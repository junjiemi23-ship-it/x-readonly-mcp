#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
x-readonly 自检脚本：把服务器当 MCP 客户端连一次，逐个调用所有工具。

用法（用服务器自己的 venv 跑）：
    python selftest.py

换过 cookie、X 改了接口、或怀疑坏了的时候，先跑这个。
"""
import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
SRV = os.path.join(HERE, "server.py")

CALLS = [
    ("health_check", {}),
    ("get_my_profile", {}),
    ("get_user_profile", {"username": "elonmusk"}),
    ("get_user_tweets", {"username": "elonmusk", "count": 2}),
    ("get_tweet", {"tweet_id": "1237110546383724547"}),
    ("search_tweets", {"query": "AI", "count": 2}),
    ("search_users", {"query": "AI researcher", "count": 2}),
    ("get_mentions", {"count": 2}),
    ("get_followers", {"username": "elonmusk", "count": 2}),
    ("get_following", {"username": "elonmusk", "count": 2}),
    ("get_trends", {}),
    ("get_user_lists", {"username": "elonmusk"}),
]


def brief(name, txt):
    try:
        data = json.loads(txt)
    except Exception:
        return txt[:200]
    if isinstance(data, dict) and "items" in data:
        first = data["items"][0] if data["items"] else {}
        tag = first.get("text") or first.get("screen_name") or ""
        return f"count={data.get('count')} | {str(tag)[:90]}"
    return json.dumps(data, ensure_ascii=False)[:280]


async def main():
    params = StdioServerParameters(command=PY, args=["-u", SRV])
    ok = fail = 0
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await asyncio.wait_for(s.initialize(), 60)
            tools = (await asyncio.wait_for(s.list_tools(), 60)).tools
            print(f"服务器工具数：{len(tools)}")
            for name, args in CALLS:
                if name not in [t.name for t in tools]:
                    print(f"  {name:18s} 不存在")
                    continue
                try:
                    res = await asyncio.wait_for(s.call_tool(name, args), 180)
                    txt = getattr(res.content[0], "text", "") if res.content else ""
                    if getattr(res, "is_error", False):
                        print(f"  {name:18s} ✗ {txt[:160]}")
                        fail += 1
                    else:
                        print(f"  {name:18s} ✓ {brief(name, txt)}")
                        ok += 1
                except Exception as exc:
                    print(f"  {name:18s} ✗ {type(exc).__name__}: {str(exc)[:140]}")
                    fail += 1
    print(f"\n结果：{ok} 项正常 / {fail} 项异常")
    if fail:
        print("排障顺序：1) .env 里的 TWITTER_COOKIES 是否过期（重跑 set-twitter-cookie.py）"
              "2) 浏览器登录 x.com 看账号是否被限制 "
              "3) 跑 health_check(recheck_endpoints=True) 刷新端点表")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
