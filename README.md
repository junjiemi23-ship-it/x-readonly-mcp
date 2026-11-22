# x-readonly MCP Server

> [中文版 → README.zh-CN.md](README.zh-CN.md)

A resilient, **strictly read-only** X (Twitter) [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server.

It combines **passwordless public reading** (via a `FxTwitter v2 API -> X oEmbed` fallback chain that requires zero cookies to read tweets, Note Tweets, full X Articles, and public user timelines) with **authenticated GraphQL reading** (featuring live `x-client-transaction-id` transaction signing and self-healing GraphQL `queryId` extraction from X's web bundle for search, followers, lists, and trends).

---

## Key Features

1. **Dual-Path Resilience (Works Without Login)**
   - `get_tweet` and `get_user_tweets` support `source="public"` (or `source="auto"` for automatic fallback).
   - The public path (`FxTwitter API -> X Official oEmbed`) never touches your cookies and **preserves long-form Note Tweets (>500 chars) as well as multi-thousand-word X Articles (`article.content.blocks`)**.
2. **Self-Healing GraphQL Endpoints & Live Transaction Signing**
   - Solves the common `401 / 403 / 404` breakage in third-party scrapers caused by stale bearer tokens or missing `x-client-transaction-id` headers.
   - When X rotates GraphQL `queryId`s, the server automatically extracts the latest `queryId`, `featureSwitches`, and `fieldToggles` from `x.com`'s live `main.*.js` bundle and updates `graphql_endpoints.json`.
3. **Strictly Read-Only & Rate-Limit Safe**
   - **Zero Write Operations**: Contains no endpoints for posting, liking, retweeting, following, or DMing — eliminating account action risk.
   - **Built-in Throttling & Short Caching**: Enforces a `1.5s` global minimum interval between requests + `60s` in-memory TTL cache + per-call item caps.
   - **Hot-Reloaded Credentials**: Re-reads `.env` or environment variables on each call so updating cookies never requires restarting the MCP server.

---

## Available Tools (13)

| Tool | Cookie Required? | Description |
| :--- | :---: | :--- |
| `get_tweet` | **No** (`source="public"`) | Read full tweet text, metrics, Note Tweets, and X Articles by tweet ID (auto-falls back to public path if primary fails) |
| `get_user_tweets` | **No** (`source="public"`) | Read a user's recent timeline and pagination cursor (`public` passwordless mode or `auto` fallback) |
| `health_check` | Optional | Separately checks authenticated GraphQL endpoints and public single-post reading (`public_tweet_id`) |
| `search_tweets` | Yes | Search tweets by query (supports `from:`, `to:`, `#hashtag`, `lang:`, `Latest` / `Top`) |
| `search_users` | Yes | Search X user accounts by keyword |
| `get_user_profile` | Yes | Get user profile details (bio, followers, following, verification status, etc.) |
| `get_my_profile` | Yes | Get the profile of the account owning the configured cookie (requires `twid`) |
| `get_mentions` | Yes | Get recent tweets mentioning (`@`) your account |
| `get_followers` | Yes | List followers of a given user |
| `get_following` | Yes | List accounts followed by a given user |
| `get_user_lists` | Yes | List public X Lists created by a user |
| `get_list_tweets` | Yes | Read the latest tweets from a specific List ID |
| `get_trends` | Yes | Get trending topics by WOEID (`1` Worldwide, `23424977` US, `23424856` Japan) |

---

## Quick Start

### 1. Install Dependencies

Python 3.10+ in a virtual environment is recommended:

```bash
git clone https://github.com/junjiemi23-ship-it/x-readonly-mcp.git
cd x-readonly-mcp

python -m venv .venv
# Windows:
.venv\Scripts\pip install -r requirements.txt
# macOS / Linux:
.venv/bin/pip install -r requirements.txt
```

### 2. (Optional) Configure Cookies for Search & GraphQL

If you only need to read shared X links or public user timelines, **you can skip this step** and call `get_tweet(..., source="public")` or `get_user_tweets(..., source="public")`.

To enable `search_tweets` and other authenticated tools, configure `.env`:

- **Option A: Interactive Hidden Input (Recommended)**
  ```bash
  python set_cookie.py
  ```
- **Option B: Copy `.env.example` to `.env`**
  Grab `auth_token`, `ct0` (required), and `twid` (optional) from an authenticated `https://x.com` browser session (`F12 -> Application -> Cookies`) and set:
  ```ini
  TWITTER_COOKIES='auth_token=xxx; ct0=yyy; twid=u%3D123456'
  ```

### 3. Wire into Your MCP Client (Claude Desktop / Cursor / Codex / Antigravity)

Add to your MCP configuration file:

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
*(On macOS / Linux, use `/path/to/x-readonly-mcp/.venv/bin/python`)*

---

## Self-Test & Troubleshooting

```bash
# Test passwordless public fallback chain directly
python x_public_fallback.py 2088993353111159216

# Full MCP stdio client self-test (requires cookie for search/profile checks)
python selftest.py
```

### Operational Notes
- **`transaction_signer` failure does NOT automatically mean expired cookies**: When logged out or challenged, X's homepage serves `entry-client-logged-out` bundles without the `ondemand.s` mapping. Even if `health_check.api_ok` is `false`, **do not skip public reading** — `source="public"` works independently.
- **Cursor Isolation**: Public timeline pagination returns cursors prefixed with `fxtwitter:`. Pass them back as-is; never mix authenticated GraphQL cursors with public timeline cursors.

---

## License

[MIT](LICENSE)
