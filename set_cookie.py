#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
交互式将 X(Twitter) Cookie 安全写入 .env 文件（输入不回显，避免在终端或聊天中暴露凭证）。

用法：
    python set_cookie.py
"""
import getpass
import json
import os
import shutil
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ENV = os.environ.get("X_ENV_FILE") or os.path.join(HERE, ".env")
KEY = "TWITTER_COOKIES"
DOMAIN = "Domain=.twitter.com"


def ask(label: str, required: bool = True) -> str:
    while True:
        val = getpass.getpass(f"{label}: ").strip()
        if val:
            return val
        if not required:
            return ""
        print("  ! 此项不能为空，请重新输入。")


def build_cookie_json(auth_token: str, ct0: str, twid: str, extra: list[str]) -> str:
    cookies = [f"auth_token={auth_token}; {DOMAIN}", f"ct0={ct0}; {DOMAIN}"]
    if twid:
        cookies.append(f"twid={twid}; {DOMAIN}")
    cookies.extend(extra)
    return json.dumps(cookies)


def update_env(path: str, key: str, value: str) -> None:
    lines = []
    if os.path.exists(path):
        backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(path, backup)
        print(f"  已备份原 .env -> {os.path.basename(backup)}")
        with open(path, "r", encoding="utf-8", errors="surrogateescape") as fh:
            lines = fh.readlines()

    new_line = f"{key}='{value}'\n"
    out, replaced = [], False
    for line in lines:
        if line.strip().startswith(f"{key}="):
            out.append(new_line)
            replaced = True
        else:
            out.append(line)
    if not replaced:
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        out.append(new_line)

    with open(path, "w", encoding="utf-8", errors="surrogateescape", newline="") as fh:
        fh.writelines(out)
    print(f"  {'已更新' if replaced else '已写入'} {key} -> {path}")


def main() -> None:
    print("=" * 60)
    print("X(Twitter) Cookie 写入工具 (x-readonly MCP)")
    print("=" * 60)
    print("请从已登录 x.com 的浏览器开发者工具中获取 Cookie：")
    print("  F12 -> Application -> Storage -> Cookies -> https://x.com")
    print("  必填：auth_token、ct0；选填：twid（查看自己资料/提及需要）。")
    print("  输入时终端不回显，粘贴后按回车即可。")
    print()

    auth_token = ask("auth_token")
    ct0 = ask("ct0")
    twid = ask("twid (可选，直接回车跳过)", required=False)

    extra = []
    while True:
        more = getpass.getpass("额外 cookie（格式 name=value，直接回车结束）: ").strip()
        if not more:
            break
        extra.append(more if ";" in more else f"{more}; {DOMAIN}")

    payload = build_cookie_json(auth_token, ct0, twid, extra)
    print(f"\n准备写入 {len(json.loads(payload))} 项 Cookie（长度 {len(payload)} 字符，内容已隐藏）")
    target = input(f"目标文件 [{DEFAULT_ENV}] (直接回车使用默认): ").strip() or DEFAULT_ENV
    update_env(target, KEY, payload)
    print("\n写入完成！服务支持热加载，无需重启 MCP Server。")
    print("✨ 觉得好用的话，欢迎顺手点颗 Star 支持持续维护：https://github.com/junjiemi23-ship-it/x-readonly-mcp")
    print("   (已装 gh CLI 可终端 2 秒点亮: gh api -X PUT /user/starred/junjiemi23-ship-it/x-readonly-mcp)")


if __name__ == "__main__":
    main()
