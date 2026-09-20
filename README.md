# Garmin Connect MCP

Remote MCP server for Garmin Connect. Gives AI assistants access to your Garmin health and activity data over the internet — no local server or app required.

Built on top of [Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp) and [cyberjunky/python-garminconnect](https://github.com/cyberjunky/python-garminconnect).

## What makes this different from Taxuspt/garmin_mcp

Taxuspt's original server runs locally on your machine and requires a locally installed AI desktop client. This version runs as a cloud service, protected by GitHub OAuth, so it works from any MCP-compatible AI assistant on any device — without running anything locally.

## What it does

Exposes 96+ Garmin Connect tools so AI assistants can query your health data directly:

- Health & Wellness: sleep, HRV, stress, body battery, heart rate, SpO2, respiration
- Activities: runs, rides, swims — with splits, weather, HR zones
- Training: readiness, status, VO2max, load
- Body composition, weight, hydration
- Workouts, gear, nutrition, challenges

## How it works

```
AI assistant → /authorize → GitHub login (+ 2FA) → /auth/callback
             → username verified → MCP access token issued → MCP connection
```

Access is protected by **GitHub OAuth** — only the GitHub account set in
`GITHUB_ALLOWED_USER` can authenticate. GitHub login with 2FA is required
once every 30 days; tokens are persisted to Redis. No credentials are stored
in the AI assistant's configuration.

1. The AI assistant detects the MCP server requires OAuth
2. A browser window opens — you log in to GitHub with 2FA
3. The server verifies your GitHub account matches `GITHUB_ALLOWED_USER_ID`
4. The AI assistant receives a 30-day access token and a 30-day refresh token

> **Cold start note:** If the hosting platform sleeps the container, the first
> request after wake-up takes a few seconds. OAuth tokens are persisted to
> a Redis-compatible store so the AI assistant does **not** need to re-authenticate.
> The `_pending` OAuth state is in-memory only — if a cold start happens
> mid-login flow, just click Connect again.

## Deploy your own

You will need:

- A container hosting platform (e.g. Render, Railway, Fly.io)
- A Redis-compatible key-value store for token persistence (e.g. Upstash, Redis Cloud)
- A GitHub OAuth App for authentication

### Step 1 — Redis store

Create a Redis database on your preferred provider. Note the **REST URL** and
**auth token** (or connection string, depending on provider).

### Step 2 — GitHub OAuth App (one-time)

Create a GitHub OAuth App at **Settings → Developer settings → OAuth Apps**:

- **Application name:** anything (e.g. `My MCP Servers`)
- **Homepage URL:** `https://your-service-url`
- **Callback URL:** `https://your-service-url/auth/callback`

Note the **Client ID** and generate a **Client Secret**.

### Step 3 — Garmin token

Run `generate_token.py` locally to obtain your `GARMINTOKENS_BASE64`.

> **Important:** You must run this from inside the project folder, and you must
> use `uv run` — not `python3`. `uv run` uses the project's own virtual
> environment. Garmin is pinned to `garminconnect==0.3.6` in both environments
> and requires Python 3.12 or newer; other dependencies can still differ.
> Using `python3` directly may use a different version on your machine, producing
> a token format the server can't read.

```bash
cd /path/to/garmin-connect-mcp   # must be in this folder
~/.local/bin/uv run --python 3.12 python generate_token.py
```

The script will prompt for your Garmin email, password, and MFA code. On
success it writes the token to **`token.txt`** in the project folder — do not
copy from the terminal (the long base64 string wraps and truncates).

1. Open `token.txt` in a text editor
2. Select all (`⌘A`) and copy
3. Paste into `GARMINTOKENS_BASE64` on your hosting platform
4. Delete `token.txt` — it contains your Garmin credentials

### Step 4 — Deploy

1. Fork this repo
2. Deploy to your container hosting platform (a `render.yaml` is included for Render)
3. Set the environment variables listed below
4. Trigger a deploy

### Step 5 — Connect to your AI assistant

In your MCP-compatible AI assistant, add this server as a remote MCP connection:

- **URL:** `https://your-service-url/mcp`
- Authentication: leave empty — the server handles OAuth automatically

**For Claude:** paste the URL into the connector dialog at [claude.ai](https://claude.ai). Claude Desktop and mobile sync automatically from the web connector.

## Configuration reference

| Env var | Required | Rotates | Description |
|---|---|---|---|
| `GARMINTOKENS_BASE64` | Initial bootstrap only | — | Garmin session from `generate_token.py`; used only when Redis has no Garmin session |
| `GITHUB_CLIENT_ID` | ✅ | Never | GitHub OAuth App client ID |
| `GITHUB_CLIENT_SECRET` | ✅ | Never | GitHub OAuth App client secret |
| `GITHUB_ALLOWED_USER_ID` | ✅ | Never | Immutable numeric GitHub user ID allowed to connect (preferred) |
| `GITHUB_ALLOWED_USER` | — | Never | GitHub username — legacy fallback, used only if `GITHUB_ALLOWED_USER_ID` is unset |
| `SERVER_URL` | ✅ | Never | Public base URL of this service |
| `UPSTASH_REDIS_REST_URL` | ✅ | Never | Redis REST endpoint |
| `UPSTASH_REDIS_REST_TOKEN` | ✅ | Never | Redis auth token |
| `GARMIN_IS_CN` | — | — | Set `true` for Garmin Connect China |

## Garmin session persistence and recovery

Redis is required and is the primary Garmin session store. The separate key
`mcp:garmin:session` contains only `di_token`, `di_refresh_token` and `di_client_id`,
never your Garmin username, password or MFA code. `TOKEN_STORE_KEY` continues to
control only GitHub/MCP OAuth storage.

When the Garmin key is absent, the server bootstraps from `GARMINTOKENS_BASE64`
and persists the session. Subsequent restarts use Redis, even if the environment
still contains an older bootstrap token. Garmin refreshes automatically on API
use; each successful refresh is saved immediately. There is no background
keepalive or guaranteed refresh-token lifetime. Startup logs describe the
access-token expiry, not the refresh-token expiry.

Redis read errors or corrupt sessions stop startup without falling back to the
bootstrap token. A failed write after refresh is reported; the renewed session
remains in memory and saving is retried before the next Garmin API request,
even if the access token does not need refreshing. If Redis still fails, that
request is not sent; a persistence failure never replays a Garmin operation. Restarting
before that write succeeds can still lose the renewed tokens. This integration
assumes one instance and does not coordinate concurrent deployments or workers.

If Garmin revokes or expires the stored session, generate a new bootstrap locally:

```bash
cd /path/to/garmin-connect-mcp   # must be in this folder
~/.local/bin/uv run --python 3.12 python generate_token.py
```

1. Open the generated `token.txt`, select all, copy
2. Update `GARMINTOKENS_BASE64` on your hosting platform
3. Stop the service and delete **only** `mcp:garmin:session` from Redis so the new
   bootstrap will be used. Do not delete the GitHub/MCP OAuth key.
4. Start/redeploy the service and confirm the session was persisted in Redis
5. Delete `token.txt`; the bootstrap environment variable can also be removed
   after successful persistence

Your AI assistant's configuration and GitHub OAuth are **not** affected.

## Architecture

- **Transport:** Streamable HTTP (MCP 1.x) via FastMCP + uvicorn
- **Auth:** GitHub OAuth 2.0 — server acts as Authorization Server, GitHub as Identity Provider
- **User restriction:** GitHub user ID verified against `GITHUB_ALLOWED_USER_ID` on every login (immutable; `GITHUB_ALLOWED_USER` is a legacy username fallback)
- **Token lifetime:** 30-day access token, 30-day refresh token (rotated on each refresh)
- **Token persistence:** Redis-compatible store — tokens survive container restarts
- **Garmin auth:** `garminconnect==0.3.6`, automatic DI token refresh, separate Redis session

## Contributing

This code was built with AI assistance ([Claude Code](https://claude.ai/code)) — vibe-coded with the best intentions. Security has been a priority throughout, but the code has not been independently audited. Use it at your own risk. If you spot a bug, a vulnerability, or an opportunity to improve anything, issues and pull requests are very welcome.

## Credits

- [Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp) — original local MCP server this remote version was adapted from (MIT)
- [cyberjunky/python-garminconnect](https://github.com/cyberjunky/python-garminconnect) — Python library powering Garmin Connect API access
- Built with [Claude Code](https://claude.ai/code)
