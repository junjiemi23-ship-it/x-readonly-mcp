# x-readonly MCP Server

一个高可靠、**纯只读**的 X (Twitter) [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) 服务器。

支持 **免登录公开读取**（通过 `FxTwitter API -> X oEmbed` 双路降级，无需任何 Cookie 即可读取单帖、长文 X Article 与用户公开时间线）与 **Cookie 登录态读取**（自带 `x-client-transaction-id` 实时事务签名与 GraphQL `queryId` 动态自愈，解锁全网搜索、粉丝/关注列表与热门趋势）。

---

## 核心特性

1. **双路容灾（免登录可用）**
   - `get_tweet` 和 `get_user_tweets` 支持 `source="public"`（或 `source="auto"` 自动降级）。
   - 免登录链路走 `FxTwitter v2 API -> X 官方 oEmbed`，不碰本地 Cookie，**完整保留超过 500 字的长推文（Note Tweet）以及上万字的 X Article 专栏文章结构**。
2. **GraphQL 端点自愈 + 实时事务签名**
   - 解决了常见的第三方库因写死 Bearer Token 或缺少 `x-client-transaction-id` 导致 `401 / 403 / 404` 的顽疾。
   - 当 X 前端更新 GraphQL `queryId` 时，自动从 `x.com` 最新 `main.*.js` bundle 实时提取 `queryId`、`featureSwitches` 与 `fieldToggles` 并落盘更新 `graphql_endpoints.json`。
3. **严格只读 & 账号风控保护**
   - **零写操作**：不含发推、点赞、转发、关注、私信或删除等任何写接口，物理隔离账号误操作风险。
   - **内置限流与短缓存**：全局请求最小间隔 `1.5s` + `60s` 内存缓存 + 单次条数上限，避免高频请求触发 X 风控。
   - **凭证热加载**：每次调用实时读取 `.env` 或环境变量，更换 Cookie 无需重启 MCP 服务器。

---

## 工具列表（13 个）

| 工具名 | 是否需 Cookie | 说明 |
| :--- | :---: | :--- |
| `get_tweet` | **否** (`source="public"`) | 按帖子 ID 读全文及互动数据（支持 Note 长推与 X Article，主路失败自动降级公开路线） |
| `get_user_tweets` | **否** (`source="public"`) | 读指定用户的最近推文时间线与翻页游标（支持 `public` 免登录读取或 `auto` 自动降级） |
| `health_check` | 部分可选 | 分离检查登录态 GraphQL 接口与公开单帖（可传 `public_tweet_id` 实测公开读取） |
| `search_tweets` | 是 | 按关键词搜索推文（支持 `from:`、`to:`、`#话题`、`lang:zh`、`Latest` / `Top`） |
| `search_users` | 是 | 按关键词搜索 X 用户账号 |
| `get_user_profile` | 是 | 查看指定用户的详细资料（简介、粉丝数、关注数、认证状态等） |
| `get_my_profile` | 是 | 查看当前 Cookie 所属账号的资料（需 Cookie 含 `twid`） |
| `get_mentions` | 是 | 查看提及（`@`）当前账号的推文 |
| `get_followers` | 是 | 查看指定账号的粉丝列表 |
| `get_following` | 是 | 查看指定账号正在关注的列表 |
| `get_user_lists` | 是 | 查看指定账号创建的列表（List） |
| `get_list_tweets` | 是 | 读取指定列表（List ID）内的最新推文 |
| `get_trends` | 是 | 查看全球或指定地区热门趋势（`woeid=1` 全球，`23424977` 美国，`23424856` 日本） |

---

## 快速开始

### 1. 安装依赖

建议使用 Python 3.10+ 虚拟环境：

```bash
git clone https://github.com/junjiemi23-ship-it/x-readonly-mcp.git
cd x-readonly-mcp

python -m venv .venv
# Windows:
.venv\Scripts\pip install -r requirements.txt
# macOS / Linux:
.venv/bin/pip install -r requirements.txt
```

### 2. （可选）配置登录 Cookie

如果只需要读公开推文链接或公开用户时间线，**可以跳过此步**，直接调用 `get_tweet(..., source="public")` 或 `get_user_tweets(..., source="public")`。

如果需要使用 `search_tweets` 搜索等功能，请配置 `.env`：

- **方式一：交互式隐藏输入（推荐，防止终端留痕）**
  ```bash
  python set_cookie.py
  ```
- **方式二：手动复制 `.env.example` 为 `.env`**
  从已登录 `https://x.com` 的浏览器开发者工具（`F12 -> Application -> Cookies`）获取 `auth_token`、`ct0`（必填）和 `twid`（选填），填入 `.env`：
  ```ini
  TWITTER_COOKIES='auth_token=xxx; ct0=yyy; twid=u%3D123456'
  ```

### 3. 接入 MCP 客户端（Claude Desktop / Cursor / Codex / Antigravity）

在客户端的 MCP 配置文件中加入：

```json
{
  "mcpServers": {
    "x-readonly": {
      "command": "/path/to/x-readonly-mcp/.venv/Scripts/python.exe",
      "args": ["-u", "/path/to/x-readonly-mcp/server.py"]
    }
  }
}
```
*(macOS / Linux 请将解释器路径换为 `/path/to/x-readonly-mcp/.venv/bin/python`)*

---

## 自检与排障

运行内置自检脚本验证全部工具：

```bash
# 免登录测试公开单帖兜底链
python x_public_fallback.py 2088993353111159216

# 完整 MCP 客户端协议自检（需配置 Cookie）
python selftest.py
```

### 常见排障笔记
- **`transaction_signer` 初始化报错不等于 Cookie 过期**：未登录或被重定向时 X 首页只下发 `entry-client-logged-out` 脚本，不含 `ondemand.s` 签名映射。此时即便 `health_check.api_ok` 为 `false`，**也不要跳过公开读取**，直接传 `source="public"` 即可正常读帖。
- **公开翻页与登录翻页游标隔离**：公开时间线返回的游标带有 `fxtwitter:` 前缀，翻页时原样传回；切勿将登录态 GraphQL 的 cursor 混入公开接口。

---

## License

MIT
