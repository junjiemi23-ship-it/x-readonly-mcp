#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
公开（免登录）推文读取兜底链。

主路（cookie + x-client-transaction-id 签名）挂了的时候用，不需要任何登录态：
  1. fxtwitter 公开 API（官方文档 https://docs.fxembed.com/api/introduction）
     - v2: https://api.fxtwitter.com/2/status/<id>  （当前在用，返回 {"code":200,"status":{...}}）
     - v1: https://api.fxtwitter.com/<id>          （旧形态 {"tweet":{...}}，留作兼容）
  2. X 官方 oEmbed（publish.twitter.com），只给正文 + 作者名，字段少但极稳。

任何 HTTP / 解析错误都不往外抛，由 fetch_tweet_public 统一吞掉并降级；
两条路都失败才返回 None。

只依赖标准库（urllib / json / re / html）。不含任何写操作，不碰 cookie。
"""
from __future__ import annotations

import html as _html
import json
import re
import sys
import urllib.parse
import urllib.request

TIMEOUT = 15            # 秒；两条路都一样
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _default_log(msg: str) -> None:
    print(f"[x-public-fallback] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 通用请求
def _get_json(url: str, tries: int = 1) -> dict:
    """GET 并按 JSON 解析。失败抛异常，交给上层降级。"""
    last: Exception | None = None
    for _ in range(max(1, tries)):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Accept": "application/json, text/plain;q=0.5, */*;q=0.1"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
            return json.loads(raw.decode("utf-8", "replace"))
        except Exception as exc:          # URLError / HTTPError / JSONDecodeError ...
            last = exc
    raise last if last is not None else RuntimeError("GET " + url)


# ---------------------------------------------------------------- 第一步：fxtwitter
def _fxtwitter(tweet_id: str) -> dict:
    data = _get_json(f"https://api.fxtwitter.com/2/status/{tweet_id}", tries=2)
    st = (data or {}).get("status") or (data or {}).get("tweet")
    if not isinstance(st, dict) or not st:
        # v2 错误响应有 code 字段（比如 404），记一下再降级
        raise ValueError(f"fxtwitter 没返回推文节点（code={data.get('code') if isinstance(data, dict) else '?'}）")
    if str(st.get("id")) != str(tweet_id):
        raise ValueError("fxtwitter 返回的帖子 ID 与请求不符")
    return _normalize_status(st)


def _normalize_status(st: dict) -> dict:
    if not re.fullmatch(r"\d{1,25}", str(st.get("id", ""))):
        raise ValueError("fxtwitter 返回的帖子 ID 无效")
    author = st.get("author") or {}
    text = (st.get("text") or ((st.get("raw_text") or {}).get("text") or "")).strip()
    if not text:
        raise ValueError("fxtwitter 返回的正文为空")
    sn = author.get("screen_name")
    return {
        "id": st.get("id"),
        "text": text,
        "author_username": sn,
        "author_name": author.get("name"),
        "created_at": st.get("created_at"),
        "created_timestamp": st.get("created_timestamp"),
        "url": st.get("url") or (f"https://x.com/{sn}/status/{st.get('id')}" if sn and st.get("id") else None),
        "likes": st.get("likes"),
        "retweets": st.get("reposts", st.get("retweets")),
        "replies": st.get("replies"),
        "views": st.get("views"),
        **({"article": st["article"]} if st.get("article") else {}),
    }


def fetch_user_tweets_public(username: str, count: int = 10, cursor: str = "", log=None) -> dict | None:
    """FxTwitter 公开用户时间线；失败返回 None，不用账号 cookie。"""
    logger = log or _default_log
    name = str(username).lstrip("@").strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}|\d{1,25}", name):
        raise ValueError("X 用户名或用户 ID 不合法")
    handle = f"id:{name}" if name.isdigit() else name
    params = {"count": max(1, min(100, int(count)))}
    if cursor:
        params["cursor"] = cursor
    url = ("https://api.fxtwitter.com/2/profile/" + urllib.parse.quote(handle, safe="")
           + "/statuses?" + urllib.parse.urlencode(params))
    try:
        data = _get_json(url)
        if data.get("code") != 200 or not isinstance(data.get("results"), list):
            raise ValueError(f"公开时间线响应无效（code={data.get('code')}）")
        rows = data["results"]
        items = [_normalize_status(row) for row in rows if row.get("type") == "status"]
        return {"items": items[:params["count"]], "total_fetched": len(items),
                "cursor": (data.get("cursor") or {}).get("bottom"), "via": "fxtwitter"}
    except Exception as exc:
        logger(f"公开时间线 @{name} 失败：{type(exc).__name__}: {str(exc)[:160]}")
        return None


# ---------------------------------------------------------------- 第二步：oEmbed
_P_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.S)
_A_RE = re.compile(r"<a\s[^>]*>(.*?)</a>", re.S)
_TCO_RE = re.compile(r"(?:https?://t\.co/|pic\.twitter\.com/)\S+")


def _strip_tags(fragment: str) -> str:
    fragment = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    fragment = _A_RE.sub(lambda m: m.group(1), fragment)
    fragment = re.sub(r"<[^>]+>", "", fragment)
    return _html.unescape(fragment)


def _oembed(tweet_id: str) -> dict:
    url = f"https://x.com/i/status/{tweet_id}"
    api = "https://publish.twitter.com/oembed?" + urllib.parse.urlencode({"url": url})
    data = _get_json(api, tries=1)
    markup = data.get("html") or ""
    m = _P_RE.search(markup)
    if not m:
        raise ValueError("oEmbed 里找不到正文 <p>")
    text = _TCO_RE.sub("", _strip_tags(m.group(1))).strip()
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise ValueError("oEmbed 正文解析出来是空的")
    # 作者用户名从 author_url（https://x.com/<handle>）尾部取，最可靠
    au = str(data.get("author_url") or "").rstrip("/")
    sn = au.rsplit("/", 1)[-1] if "/" in au else None
    if sn and not re.fullmatch(r"[A-Za-z0-9_]{1,15}", sn):
        sn = None
    return {
        "text": text,
        "author_username": sn,
        "author_name": data.get("author_name"),
        "created_at": None,          # oEmbed 只给 "October 3, 2026" 这种日期串，不强求
        "url": data.get("url") or url,
    }


# ---------------------------------------------------------------- 对外入口
def fetch_tweet_public(tweet_id: str, log=None) -> dict | None:
    """免登录读单条推文。成功返回 dict（text/author_username/author_name/
    created_at/url/via），两条路都失败返回 None。绝不抛异常。

    log 可传一个 print 风格的回调，用来走调用方自己的日志通道。
    """
    tid = str(tweet_id or "").strip()
    if not re.fullmatch(r"\d{1,25}", tid):
        log and log(f"公开兜底跳过：推文 ID 不合法 {tid!r}")
        return None
    logger = log or _default_log
    for via, fn in (("fxtwitter", _fxtwitter), ("oembed", _oembed)):
        try:
            out = fn(tid)
            if out:
                out["via"] = via
                return out
        except Exception as exc:
            logger(f"公开兜底 {via}（{tid}）失败：{type(exc).__name__}: {str(exc)[:160]}")
    logger(f"公开兜底两条路都失败：{tid}")
    return None


if __name__ == "__main__":
    import sys
    for tid in sys.argv[1:] or ["2106251767537348942"]:
        r = fetch_tweet_public(tid)
        print(tid, "->", (r or {}).get("via"))
        if r:
            print("  author:", r.get("author_username"), "/", r.get("author_name"))
            print("  at:", r.get("created_at"))
            print("  url:", r.get("url"))
            print("  text:", (r.get("text") or "")[:160].replace("\n", " / "))
