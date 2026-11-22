#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
x-readonly MCP server —— 用你自己的 X(Twitter) 登录 cookie 做【只读】读取。

为什么不直接用现成库（2026-09-11 实测结论）：
  - agent-twitter-client 0.0.18 / @the-convocation/twitter-scraper：内部写死的 web bearer
    已过期，所有 graphql 请求 401 code 32；换 bearer 后仍缺 x-client-transaction-id 校验。
  - 本文件自己实现：cookie + 当前有效 web bearer + x-client-transaction-id 事务签名
    + 从 X 当前 main bundle 实时提取 queryId，所以 X 换 id 时能自愈，不用等上游库更新。

只读：本文件不含任何发推 / 点赞 / 关注 / 删除等写操作。
凭证：从 Hermes 的 .env 读 TWITTER_COOKIES（每次调用重读，换 cookie 不用重启）。
风控：全局最小请求间隔 + 短缓存 + 单次条数上限，避免高频或批量打 X。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Literal

import bs4
from mcp.server.mcpserver import MCPServer
from x_client_transaction import ClientTransaction
from x_client_transaction.utils import get_ondemand_file_url

from x_public_fallback import fetch_tweet_public, fetch_user_tweets_public

# ---------------------------------------------------------------- 配置
HERE = os.path.dirname(os.path.abspath(__file__))
HERMES_HOME = os.environ.get("HERMES_HOME") or os.path.join(
    os.path.expanduser("~"), "AppData", "Local", "hermes")


def _resolve_env_file() -> str:
    """优先使用 X_ENV_FILE 或项目目录 .env，回退到 HERMES_HOME/.env（兼容本机既有环境）。"""
    if os.environ.get("X_ENV_FILE"):
        return os.environ["X_ENV_FILE"]
    local_env = os.path.join(HERE, ".env")
    if os.path.exists(local_env):
        return local_env
    return os.path.join(HERMES_HOME, ".env")


ENV_FILE = _resolve_env_file()
EP_FILE = os.path.join(HERE, "graphql_endpoints.json")

WEB_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8"
    "LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

MIN_INTERVAL_SEC = 1.5      # 两次真实请求最小间隔（风控）
CACHE_TTL_SEC = 60          # 相同请求短缓存
SIGNER_TTL_SEC = 1800       # 事务签名器重建周期
HTTP_TIMEOUT = 30
MAX_COUNT = 100
SERVER_NAME = "x-readonly"

mcp = MCPServer(SERVER_NAME,
                instructions=("X(Twitter) 只读读取。get_tweet 与 get_user_tweets 支持 "
                              "source='public'，免登录读取公开内容；签名或 health_check.api_ok "
                              "失败不能证明 cookie 过期，也不能据此跳过公开读取。搜索仍依赖登录接口。"),
                log_level="WARNING")


def log(msg: str) -> None:
    """日志只走 stderr —— stdout 是 MCP 协议通道。"""
    print(f"[{SERVER_NAME}] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 凭证
def load_cookies() -> dict:
    """每次调用重读环境变量或 .env（换 cookie 后无需重启服务器）。
    支持 JSON 数组格式或标准 'auth_token=xxx; ct0=yyy; twid=u%3D123' 字符串。
    """
    raw = os.environ.get("TWITTER_COOKIES", "").strip()
    env_path = _resolve_env_file()
    if not raw:
        if not os.path.exists(env_path):
            raise RuntimeError(f"未配置 TWITTER_COOKIES 环境变量，且找不到 .env：{env_path}")
        with open(env_path, "r", encoding="utf-8", errors="surrogateescape") as fh:
            for line in fh:
                if line.strip().startswith("TWITTER_COOKIES="):
                    raw = line.strip().split("=", 1)[1].strip()
                    break
    if not raw:
        raise RuntimeError("TWITTER_COOKIES 未配置；请在 .env 中写入或运行 python set_cookie.py")
    if len(raw) >= 2 and raw[0] in "'\"" and raw[-1] == raw[0]:
        raw = raw[1:-1].strip()
    jar: dict[str, str] = {}
    if raw.startswith("["):
        for item in json.loads(raw):
            part = item.split(";")[0].strip()
            if "=" in part:
                name, value = part.split("=", 1)
                jar[name.strip()] = value.strip()
    else:
        for part in raw.split(";"):
            part = part.strip()
            if "=" in part:
                name, value = part.split("=", 1)
                if name.strip().lower() not in ("domain", "path", "expires", "max-age", "samesite"):
                    jar[name.strip()] = value.strip()
    if "auth_token" not in jar or "ct0" not in jar:
        raise RuntimeError("cookie 里缺少 auth_token 或 ct0")
    return jar


def my_user_id() -> str | None:
    """从 twid(u%3D123456) 里解出我自己的数字 user id。"""
    twid = load_cookies().get("twid", "")
    if not twid:
        return None
    digits = re.search(r"(\d{5,})", urllib.parse.unquote(twid))
    return digits.group(1) if digits else None


def headers(jar: dict, extra: dict | None = None) -> dict:
    h = {
        "authorization": f"Bearer {WEB_BEARER}",
        "x-csrf-token": jar.get("ct0", ""),
        "cookie": "; ".join(f"{k}={v}" for k, v in jar.items()),
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-active-user": "yes",
        "x-twitter-client-language": "en",
        "user-agent": UA,
        "accept": "*/*",
        "referer": "https://x.com/",
    }
    if extra:
        h.update(extra)
    return h


# ---------------------------------------------------------------- 限速 + 缓存
_last_call = [0.0]
_cache: dict[str, tuple[float, object]] = {}


def _throttle() -> None:
    gap = time.time() - _last_call[0]
    if gap < MIN_INTERVAL_SEC:
        time.sleep(MIN_INTERVAL_SEC - gap)
    _last_call[0] = time.time()


class XError(RuntimeError):
    pass


def http_text(url: str, jar: dict | None = None) -> str:
    h = {"user-agent": UA, "accept": "*/*"}
    if jar:
        h["cookie"] = "; ".join(f"{k}={v}" for k, v in jar.items())
        h["referer"] = "https://x.com/"
    _throttle()
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read().decode("utf-8", "replace")


# ---------------------------------------------------------------- 事务签名
_sign = {"tx": None, "at": 0.0}


def signer(force: bool = False):
    """x-client-transaction-id 生成器；X 对部分接口（搜索/粉丝）强制要求它。"""
    if not force and _sign["tx"] is not None and time.time() - _sign["at"] < SIGNER_TTL_SEC:
        return _sign["tx"]
    jar = load_cookies()
    soup = bs4.BeautifulSoup(http_text("https://x.com/", jar), "html.parser")
    try:
        ondemand_url = get_ondemand_file_url(response=soup)
    except AttributeError as exc:
        raise XError("X 返回页缺少签名脚本，无法初始化；cookie 有效性尚未验证。"
                     "公开帖可用 source='public' 读取。") from exc
    ondemand = http_text(ondemand_url)
    _sign["tx"] = ClientTransaction(home_page_response=soup, ondemand_file_response=ondemand)
    _sign["at"] = time.time()
    log("事务签名器已重建")
    return _sign["tx"]


# ---------------------------------------------------------------- 端点表（自愈）
_OPS_RE = re.compile(r'queryId:"([A-Za-z0-9_-]{10,})",operationName:"([A-Za-z0-9_]+)"')


def load_endpoints() -> dict:
    with open(EP_FILE, "r", encoding="utf-8") as fh:
        return json.load(fh)


def refresh_endpoints() -> dict:
    """从 X 当前 main bundle 重新提取 queryId + featureSwitches（X 换 id 后自愈）。"""
    jar = load_cookies()
    html = http_text("https://x.com/home", jar)
    main_js = sorted(set(re.findall(
        r'https://abs\.twimg\.com/responsive-web/client-web/main\.[^"\']+\.js', html)))
    if not main_js:
        raise XError("拿不到 X 的 main bundle（可能被登出/风控）")
    js = http_text(main_js[0], jar)
    ops: dict[str, dict] = {}
    for m in _OPS_RE.finditer(js):
        qid, op = m.group(1), m.group(2)
        if op in ops:
            continue
        win = js[m.start():m.start() + 4000]
        fs = re.search(r"featureSwitches:\[(.*?)\]", win, re.S)
        ft = re.search(r"fieldToggles:\[(.*?)\]", win, re.S)
        ops[op] = {
            "query_id": qid,
            "feature_switches": re.findall(r'"([^"]+)"', fs.group(1)) if fs else [],
            "field_toggles": re.findall(r'"([^"]+)"', ft.group(1)) if ft else [],
        }
    if not ops:
        raise XError("bundle 里没解析出任何操作")
    with open(EP_FILE, "w", encoding="utf-8") as fh:
        json.dump(ops, fh, ensure_ascii=False, indent=1)
    log(f"端点表已刷新：{len(ops)} 个操作（main bundle {main_js[0].rsplit('/', 1)[-1]}）")
    return ops


def _endpoint(op: str) -> dict:
    eps = load_endpoints()
    if op not in eps:
        eps = refresh_endpoints()
    meta = eps.get(op)
    if not meta or not meta.get("query_id"):
        raise XError(f"端点表里没有 {op}，且刷新后仍缺")
    return meta


# ---------------------------------------------------------------- graphql 调用
def gql(op: str, variables: dict, retry: bool = True) -> object:
    meta = _endpoint(op)
    path = f"/graphql/{meta['query_id']}/{op}"
    key = path + "?" + urllib.parse.urlencode(sorted(variables.items()))
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL_SEC:
        return hit[1]

    jar = load_cookies()
    tid = signer().generate_transaction_id(method="GET", path=path)
    feats = {k: True for k in meta.get("feature_switches") or []}
    togs = {k: True for k in meta.get("field_toggles") or []}
    url = (f"https://api.x.com{path}"
           f"?variables={urllib.parse.quote(json.dumps(variables, separators=(',', ':')))}")
    if feats:
        url += f"&features={urllib.parse.quote(json.dumps(feats, separators=(',', ':')))}"
    if togs:
        url += f"&fieldToggles={urllib.parse.quote(json.dumps(togs, separators=(',', ':')))}"
    _throttle()
    req = urllib.request.Request(url, headers=headers(jar, {"x-client-transaction-id": tid}))
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read(300).decode("utf-8", "replace")
        if retry and exc.code in (404, 400):
            log(f"{op} HTTP {exc.code}，刷新端点表后重试")
            refresh_endpoints()
            _sign["tx"] = None
            return gql(op, variables, retry=False)
        if retry and exc.code in (401, 403):
            log(f"{op} HTTP {exc.code}，重建签名器后重试")
            _sign["tx"] = None
            signer(force=True)
            return gql(op, variables, retry=False)
        hint = "（cookie 可能过期：重跑 set-twitter-cookie.py）" if exc.code in (401, 403) else ""
        raise XError(f"HTTP {exc.code} {body[:180]} {hint}") from exc
    except Exception as exc:
        raise XError(f"请求失败：{type(exc).__name__}: {exc}") from exc

    if isinstance(data, dict) and data.get("errors") and not data.get("data"):
        raise XError(f"X 返回错误：{json.dumps(data['errors'], ensure_ascii=False)[:300]}")
    _cache[key] = (time.time(), data)
    return data


def v11_get(path: str, params: dict | None = None) -> object:
    """个别桌面端 v1.1 接口仍然可用（趋势 / 列表），作为补充。"""
    params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    key = "11:" + path + "?" + urllib.parse.urlencode(sorted(params.items()))
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL_SEC:
        return hit[1]
    jar = load_cookies()
    url = ("https://api.x.com/1.1/" + path + "?" + urllib.parse.urlencode(params))
    _throttle()
    req = urllib.request.Request(url, headers=headers(jar))
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise XError(f"HTTP {exc.code} {exc.read(200).decode('utf-8', 'replace')[:180]}") from exc
    _cache[key] = (time.time(), data)
    return data


# ---------------------------------------------------------------- 解析
def _dig(node, kind: str) -> list:
    """在 graphql 时间线里递归找 tweet_results / user_results。"""
    out: list = []

    def rec(o):
        if isinstance(o, dict):
            ic = o.get("itemContent")
            if isinstance(ic, dict):
                if kind == "tweet" and isinstance(ic.get("tweet_results"), dict):
                    res = ic["tweet_results"].get("result")
                    if isinstance(res, dict):
                        out.append(res)
                if kind == "user" and isinstance(ic.get("user_results"), dict):
                    res = ic["user_results"].get("result")
                    if isinstance(res, dict):
                        out.append(res)
            for v in o.values():
                rec(v)
        elif isinstance(o, list):
            for v in o:
                rec(v)

    rec(node)
    return out


def _bottom_cursor(node) -> str | None:
    found: list = []

    def rec(o):
        if isinstance(o, dict):
            if o.get("cursorType") == "Bottom" and o.get("value"):
                found.append(o["value"])
            if str(o.get("entryId", "")).startswith("cursor-bottom"):
                _, _, val = str(o.get("entryId")).partition("cursor-bottom-")
                if val:
                    found.append(val)
            for v in o.values():
                rec(v)
        elif isinstance(o, list):
            for v in o:
                rec(v)

    rec(node)
    return found[-1] if found else None


def clean_text(s: str, limit: int = 500) -> str:
    s = (s or "").replace("\r", " ").replace("\n", " ⏎ ")
    return s if len(s) <= limit else s[:limit] + "…"


def fmt_tweet(res: dict) -> dict:
    if res.get("__typename") == "TweetWithVisibilityResults":
        res = res.get("tweet") or {}
    legacy = res.get("legacy") or {}
    core = res.get("core") or {}
    ure = ((core.get("user_results") or {}).get("result") or {})
    ucore = ure.get("core") or {}
    ulegacy = ure.get("legacy") or {}
    sn = ucore.get("screen_name") or ulegacy.get("screen_name")
    rid = res.get("rest_id") or legacy.get("id_str")
    note = (((res.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {})
    text = note.get("text") or legacy.get("full_text") or legacy.get("text") or ""
    rt = legacy.get("retweeted_status_result")
    return {
        "id": rid,
        "author": sn,
        "name": ucore.get("name") or ulegacy.get("name"),
        "created_at": legacy.get("created_at"),
        "text": text,
        "likes": legacy.get("favorite_count"),
        "retweets": legacy.get("retweet_count"),
        "replies": legacy.get("reply_count"),
        "views": ((res.get("views") or {}).get("count")),
        "is_retweet": bool(rt),
        "url": f"https://x.com/{sn}/status/{rid}" if sn and rid else None,
    }


def _from_public(pub: dict, tweet_id: str) -> dict:
    """把公开兜底（fxtwitter / oEmbed）的结果整理成 fmt_tweet 一样的结构。"""
    sn = pub.get("author_username")
    return {
        "id": tweet_id,
        "author": sn,
        "name": pub.get("author_name"),
        "created_at": pub.get("created_at"),
        "text": pub.get("text") or "",
        "likes": pub.get("likes"),
        "retweets": pub.get("retweets"),
        "replies": pub.get("replies"),
        "views": pub.get("views"),
        "is_retweet": None,
        "url": pub.get("url") or (f"https://x.com/{sn}/status/{tweet_id}" if sn else None),
        "served_via": pub.get("via"),
        **({"created_timestamp": pub["created_timestamp"]} if pub.get("created_timestamp") is not None else {}),
        **({"article": pub["article"]} if pub.get("article") else {}),
    }


def fmt_user(res: dict) -> dict:
    if res.get("__typename") == "UserWithVisibilityResults":
        res = res.get("user") or {}
    legacy = res.get("legacy") or {}
    core = res.get("core") or {}
    rel = res.get("relationship_counts") or {}
    twc = res.get("tweet_counts") or {}
    bio = res.get("profile_bio") or {}
    sn = core.get("screen_name") or legacy.get("screen_name")
    return {
        "id": res.get("rest_id") or legacy.get("id_str"),
        "screen_name": sn,
        "name": core.get("name") or legacy.get("name"),
        "bio": clean_text(bio.get("description") or legacy.get("description") or "", 200),
        "followers": rel.get("followers", legacy.get("followers_count")),
        "following": rel.get("following", legacy.get("friends_count")),
        "tweets": twc.get("tweets", legacy.get("statuses_count")),
        "location": (res.get("location") or {}).get("location"),
        "verified": bool((res.get("verification") or {}).get("verified")
                         or res.get("is_blue_verified")),
        "created_at": core.get("created_at") or legacy.get("created_at"),
        "url": f"https://x.com/{sn}" if sn else None,
    }


def clamp(n, default: int = 10) -> int:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return default
    return max(1, min(MAX_COUNT, n))


def get_user_id(username: str) -> str:
    if username.isdigit():
        return username
    data = gql("UserByScreenName", {"screen_name": username.lstrip("@"),
                                    "withGrokTranslatedBio": True})
    uid = (((data.get("data") or {}).get("user") or {}).get("result") or {}).get("rest_id")
    if not uid:
        raise XError(f"找不到用户 @{username.lstrip('@')}")
    return str(uid)


def _timeline_tool(op: str, variables: dict, kind: str, count: int) -> dict:
    data = gql(op, variables)
    items = _dig(data, kind)
    seen, uniq = set(), []
    for it in items:
        key = it.get("rest_id") or (it.get("legacy") or {}).get("id_str")
        if key in seen:
            continue
        seen.add(key)
        uniq.append(it)
    fmt = fmt_tweet if kind == "tweet" else fmt_user
    picked = uniq[:count]
    return {"count": len(picked), "total_fetched": len(uniq),
            "items": [fmt(i) for i in picked], "cursor": _bottom_cursor(data)}


# ---------------------------------------------------------------- 工具
@mcp.tool(name="get_user_profile", description="看某个 X 用户的资料（粉丝数、简介等）")
def get_user_profile(username: str) -> dict:
    """看某个 X 用户的资料。username 可带或不带 @。"""
    name = username.lstrip("@").strip()
    data = gql("UserByScreenName", {"screen_name": name, "withGrokTranslatedBio": True})
    res = (((data.get("data") or {}).get("user") or {}).get("result") or {})
    if not res:
        raise XError(f"找不到用户 @{name}")
    return fmt_user(res)


@mcp.tool(name="get_my_profile", description="看我自己（cookie 所属账号）的资料")
def get_my_profile() -> dict:
    """看我自己账号的资料。"""
    uid = my_user_id()
    if not uid:
        raise XError("cookie 里没有 twid，无法识别我自己；请重新用脚本写入 cookie")
    data = gql("UsersByRestIds", {"userIds": [uid]})
    for u in ((data.get("data") or {}).get("users") or []):
        res = u.get("result") or {}
        if res:
            return fmt_user(res)
    raise XError("取不到我的资料")


@mcp.tool(name="get_user_tweets", description="读用户最近帖子全文；source=public 免登录读取，签名失败仍可用")
def get_user_tweets(username: str, count: int = 10, cursor: str = "",
                    source: Literal["auto", "public"] = "auto") -> dict:
    """读用户最近帖子。auto 主路失败后走公开读取；public 不使用 cookie。"""
    if source not in ("auto", "public"):
        raise ValueError("source 必须是 auto 或 public")
    if source == "public" or cursor.startswith("fxtwitter:"):
        if cursor and not cursor.startswith("fxtwitter:"):
            raise ValueError("公开时间线不能使用登录接口的 cursor；请留空重新开始。")
        _throttle()
        pub = fetch_user_tweets_public(username, clamp(count, 10), cursor.removeprefix("fxtwitter:"), log=log)
        if pub is None:
            raise XError(f"公开时间线 @{username} 读取失败")
        items = [_from_public({**item, "via": pub["via"]}, str(item["id"])) for item in pub["items"]]
        return {"count": len(items), "total_fetched": pub["total_fetched"], "items": items,
                "cursor": f"fxtwitter:{pub['cursor']}" if pub.get("cursor") else None,
                "served_via": pub["via"]}
    try:
        return _get_user_tweets_primary(username, count, cursor)
    except Exception as exc:
        if cursor:
            raise XError("登录接口翻页失败；改用 source='public'、空 cursor 重新开始公开时间线。") from exc
        log(f"用户时间线主路失败（{type(exc).__name__}）→ 转公开读取")
        out = get_user_tweets(username, count, source="public")
        out["primary_error"] = str(exc)[:160]
        return out


def _get_user_tweets_primary(username: str, count: int, cursor: str) -> dict:
    uid = get_user_id(username.lstrip("@").strip())
    variables = {"userId": uid, "count": clamp(count, 10), "includePromotedContent": False,
                 "withVoice": True}
    if cursor:
        variables["cursor"] = cursor
    return _timeline_tool("UserTweets", variables, "tweet", clamp(count, 10))


@mcp.tool(name="get_tweet", description="按帖子ID读全文；支持免登录备用读取，source=public 跳过失效签名，不应因health失败跳过")
def get_tweet(tweet_id: str, source: Literal["auto", "public"] = "auto") -> dict:
    """按推文 ID（URL 里 status/ 后面那串数字）读单条推文。

    主路（cookie + 事务签名）挂了会自动降级到公开兜底（fxtwitter → oEmbed，
    不用登录）；返回里的 served_via 标明这条推文是谁提供的
    （primary / fxtwitter / oembed）。
    """
    tid = str(tweet_id).strip()
    if not re.fullmatch(r"\d{1,25}", tid):
        raise ValueError("帖子 ID 必须是 1 至 25 位数字")
    if source not in ("auto", "public"):
        raise ValueError("source 必须是 auto 或 public")
    if source == "public":
        _throttle()
        pub = fetch_tweet_public(tid, log=log)
        if pub is None:
            raise XError(f"公开帖 {tid} 读取失败")
        return _from_public(pub, tid)
    try:
        data = gql("TweetResultByRestId", {"tweetId": tid,
                                           "withCommunity": False,
                                           "includePromotedContent": False, "withVoice": False})
        res = (((data.get("data") or {}).get("tweetResult") or {}).get("result") or {})
        if not res:
            raise XError(f"读不到推文 {tid}")
        out = fmt_tweet(res)
        if str(out.get("id")) != tid:
            raise XError(f"推文 {tid} 不可读或返回 ID 不符")
        out["served_via"] = "primary"
        log(f"get_tweet {tid}：主路提供")
        return out
    except Exception as exc:
        log(f"get_tweet {tid}：主路失败（{type(exc).__name__}: {str(exc)[:140]}）→ 转公开兜底")
        pub = fetch_tweet_public(tid, log=log)
        if pub:
            log(f"get_tweet {tid}：{pub['via']} 提供")
            return _from_public(pub, tid)
        raise XError(f"读不到推文 {tid}（主路与公开兜底都失败）") from exc


@mcp.tool(name="search_tweets", description="按关键词搜索推文（支持 from:、to:、#话题、lang: 等语法）")
def search_tweets(query: str, count: int = 15, product: str = "Latest", cursor: str = "") -> dict:
    """搜索推文。product=Latest（最新）/ Top（热门）。query 支持 from:user、to:user、#tag、lang:zh。

    说明：本服务器未实现「我关注的时间线」读取接口，想看我关心的人，可以用
    from:a OR from:b 这类组合查询，或直接对具体账号用 get_user_tweets。
    """
    variables = {"rawQuery": query, "count": clamp(count, 15), "querySource": "typed_query",
                 "product": product if product in ("Latest", "Top") else "Latest"}
    if cursor:
        variables["cursor"] = cursor
    return _timeline_tool("SearchTimeline", variables, "tweet", clamp(count, 15))


@mcp.tool(name="search_users", description="按关键词搜索用户账号")
def search_users(query: str, count: int = 10, cursor: str = "") -> dict:
    """搜索用户账号。"""
    variables = {"rawQuery": query, "count": clamp(count, 10), "querySource": "typed_query",
                 "product": "People"}
    if cursor:
        variables["cursor"] = cursor
    return _timeline_tool("SearchTimeline", variables, "user", clamp(count, 10))


@mcp.tool(name="get_mentions", description="看谁在推文里 @ 了我")
def get_mentions(count: int = 15, cursor: str = "") -> dict:
    """看 @ 到我账号的推文（用搜索 to:我的用户名 实现）。"""
    me = get_my_profile()
    variables = {"rawQuery": f"to:{me['screen_name']}", "count": clamp(count, 15),
                 "querySource": "typed_query", "product": "Latest"}
    if cursor:
        variables["cursor"] = cursor
    out = _timeline_tool("SearchTimeline", variables, "tweet", clamp(count, 15))
    out["query"] = f"to:{me['screen_name']}"
    return out


@mcp.tool(name="get_followers", description="看某个账号的粉丝")
def get_followers(username: str, count: int = 20, cursor: str = "") -> dict:
    """看某个账号的粉丝列表。"""
    uid = get_user_id(username.lstrip("@").strip())
    variables = {"userId": uid, "count": clamp(count, 20), "includePromotedContent": False}
    if cursor:
        variables["cursor"] = cursor
    return _timeline_tool("Followers", variables, "user", clamp(count, 20))


@mcp.tool(name="get_following", description="看某个账号正在关注谁")
def get_following(username: str, count: int = 20, cursor: str = "") -> dict:
    """看某个账号正在关注哪些人。"""
    uid = get_user_id(username.lstrip("@").strip())
    variables = {"userId": uid, "count": clamp(count, 20), "includePromotedContent": False}
    if cursor:
        variables["cursor"] = cursor
    return _timeline_tool("Following", variables, "user", clamp(count, 20))


@mcp.tool(name="get_user_lists", description="看某个账号创建的列表（list）")
def get_user_lists(username: str) -> dict:
    """看某个账号创建的列表。"""
    name = username.lstrip("@").strip()
    lists = v11_get("lists/list.json", {"screen_name": name})
    return {"username": name, "count": len(lists or []),
            "lists": [{"id": l.get("id_str"), "name": l.get("name"),
                       "members": l.get("member_count"),
                       "url": f"https://x.com/i/lists/{l.get('id_str')}"}
                      for l in (lists or [])]}


@mcp.tool(name="get_list_tweets", description="读某个列表（list）里的最新推文")
def get_list_tweets(list_id: str, count: int = 20, cursor: str = "") -> dict:
    """按列表 ID 读该列表的最新推文（列表 ID 可由 get_user_lists 得到）。"""
    variables = {"listId": str(list_id).strip(), "count": clamp(count, 20)}
    if cursor:
        variables["cursor"] = cursor
    return _timeline_tool("ListLatestTweetsTimeline", variables, "tweet", clamp(count, 20))


@mcp.tool(name="get_trends", description="看某地区热门趋势（woeid：1=全球，23424977=美国，23424856=日本）")
def get_trends(woeid: int = 1) -> dict:
    """看某地区的热门趋势，默认全球。"""
    try:
        woeid = int(woeid)
    except (TypeError, ValueError):
        woeid = 1
    res = v11_get("trends/place.json", {"id": woeid})
    trends = (res or [{}])[0].get("trends", []) if res else []
    return {"woeid": woeid, "count": len(trends),
            "trends": [{"name": t.get("name"), "tweet_volume": t.get("tweet_volume")}
                       for t in trends[:30]]}


@mcp.tool(name="health_check", description="分开检查登录接口与公开单帖；api_ok仅指登录接口，失败不能证明cookie过期；public_tweet_id可实测公开读取")
def health_check(recheck_endpoints: bool = False, public_tweet_id: str = "") -> dict:
    """检查 cookie、事务签名、各接口可用性。recheck_endpoints=True 时会重新抓一次端点表。"""
    out: dict = {"env_file": ENV_FILE, "min_interval_sec": MIN_INTERVAL_SEC,
                 "api_ok": False, "api_ok_scope": "authenticated_checks",
                 "cookie_validity": "not_verified", "public_single_post": "not_checked",
                 "public_user_tweets": "not_checked",
                 "guidance": "登录/签名失败仍可调用 get_tweet 或 get_user_tweets(source='public')；搜索需要登录接口。"}
    if public_tweet_id:
        try:
            tweet = get_tweet(public_tweet_id, source="public")
            out["public_single_post"] = {"ok": True, "id": tweet["id"], "served_via": tweet["served_via"]}
        except Exception as exc:
            out["public_single_post"] = {"ok": False, "error": str(exc)[:160]}
    try:
        jar = load_cookies()
        out["cookies"] = {k: len(v) for k, v in jar.items()}
        out["my_user_id"] = my_user_id()
    except Exception as exc:
        out["cookie_error"] = str(exc)
        return out
    if recheck_endpoints:
        try:
            out["refreshed_ops"] = len(refresh_endpoints())
        except Exception as exc:
            out["refresh_error"] = str(exc)
    try:
        signer()
        out["transaction_signer"] = "ok"
    except Exception as exc:
        out["transaction_signer"] = f"失败：{exc}"
        return out
    checks = {}
    for label, fn in (
        ("me", lambda: get_my_profile()),
        ("user_tweets", lambda: _get_user_tweets_primary("elonmusk", 1, "")),
        ("search", lambda: search_tweets("AI", 1)),
        ("followers", lambda: get_followers("elonmusk", 1)),
        ("trends", lambda: get_trends(1)),
    ):
        try:
            fn()
            checks[label] = "ok"
        except Exception as exc:
            checks[label] = f"失败：{str(exc)[:120]}"
    out["checks"] = checks
    out["api_ok"] = all(v == "ok" for v in checks.values())
    return out


if __name__ == "__main__":
    log(f"启动，env={ENV_FILE}，端点表={EP_FILE}")
    mcp.run("stdio")
