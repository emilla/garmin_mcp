# Deploying to Railway (remote MCP for claude.ai)

This runbook deploys the Garmin MCP server as a **remote MCP server** on
[Railway](https://railway.app), so claude.ai (web and mobile) can connect to it
as a [custom connector](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)
— no desktop app bridge required.

```
claude.ai (web / mobile / desktop)
        │  streamable HTTP + TLS
        ▼
Railway service  ──►  Garmin Connect
  garmin-mcp           (saved OAuth tokens)
  Dockerfile build
```

Cost: a single hobby-tier Railway service (~$5/mo). No database, no other
services needed.

## How authentication works

There are two separate concerns:

1. **Server → Garmin:** the server logs in with OAuth tokens you export once
   from your machine (`garmin-mcp-auth --export`) and store in the
   `GARMINTOKENS` env var. Tokens last ~6 months; re-export when they expire.
2. **claude.ai → server:** claude.ai custom connectors support OAuth or
   **no auth** — but *not* custom headers/bearer tokens. This deployment uses
   the authless mode with an **unguessable URL path** (`MCP_HTTP_PATH`) as the
   credential. Anyone with the full URL has access to your Garmin data, so
   treat the URL like a password and prefer a read-only tool allowlist (see
   below).

## Step 1 — Export your Garmin tokens (local machine)

```bash
# Authenticate once if you haven't already (handles MFA interactively)
uvx --python 3.12 --from git+https://github.com/<you>/garmin_mcp garmin-mcp-auth

# Print the token bundle as a single line (goes to GARMINTOKENS on Railway)
uvx --python 3.12 --from git+https://github.com/<you>/garmin_mcp garmin-mcp-auth --export
```

Copy the long output line. Treat it like a password.

## Step 2 — Generate a secret path

```bash
echo "/mcp-$(openssl rand -hex 24)"
```

## Step 3 — Create the Railway service

1. Railway dashboard → New Project → **Deploy from GitHub repo** → pick this
   repo. Railway detects the `Dockerfile` and uses it automatically.
2. Set the service variables:

| Variable | Value | Notes |
|---|---|---|
| `MCP_TRANSPORT` | `streamable-http` | Required — default is stdio. |
| `GARMINTOKENS` | output of `--export` | The whole line, no quotes. |
| `MCP_HTTP_PATH` | `/mcp-<random>` from step 2 | This is your access credential. |
| `GARMIN_ENABLED_TOOLS` | e.g. `get_activities_by_date,get_last_activity,get_sleep_data` | Optional but recommended: a read-only allowlist limits blast radius if the URL leaks, and keeps claude.ai context small. |
| `GARMIN_IS_CN` | `true` | Only for Garmin Connect China. |

   `PORT` is injected by Railway; the server reads it automatically and binds
   `0.0.0.0`.

3. Deploy. The logs should end with:

   ```
   Garmin Connect client initialized successfully.
   Serving MCP over streamable-http on 0.0.0.0:<port>/mcp-<random>
   ```

4. Settings → Networking → **Generate Domain** to get
   `https://<name>.up.railway.app`.

## Step 4 — (Optional) custom domain via Cloudflare

Same flow as any Railway service: attach `mcp.yourdomain.com` as a custom
domain in the Railway dashboard (the CLI's `railway domain` may return
Unauthorized — use the UI), then add the CNAME in Cloudflare with **gray cloud
(DNS only)**. Proxying (orange cloud) breaks Railway's TLS and buffers SSE
streams. Railway auto-provisions the certificate in a few minutes.

Not required — the `*.up.railway.app` domain works fine and is slightly more
obscure.

## Step 5 — Connect claude.ai

1. claude.ai → Settings → Connectors → **Add custom connector**.
2. URL: `https://<your-domain><MCP_HTTP_PATH>` — e.g.
   `https://garmin-mcp.up.railway.app/mcp-a1b2c3...`
3. Leave the OAuth fields empty (advanced settings).
4. Enable the connector in a new chat and ask for your last activity.

Custom connectors require a paid claude.ai plan (Pro/Max/Team).

## Operational notes

- **SSE buffering:** the MCP SDK already sends
  `Cache-Control: no-cache, no-transform` and `X-Accel-Buffering: no` on its
  event streams, so no proxy-buffering workarounds are needed.
- **Rotating the secret:** change `MCP_HTTP_PATH` and redeploy, then update
  the connector URL in claude.ai.
- **Token expiry:** when Garmin tokens expire (~6 months), the deploy logs show
  `GARMINTOKENS token data is invalid or expired`. Re-run step 1 and update
  the variable.
- **Token safety:** the server never prints the `GARMINTOKENS` value to logs.
- **Local stdio use is unchanged:** without `MCP_TRANSPORT` set, the server
  behaves exactly as before for Claude Desktop.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| 404 from the connector URL | Path mismatch — the URL must include `MCP_HTTP_PATH` exactly. |
| `GARMINTOKENS token data is invalid or expired` in logs | Re-export tokens (step 1) and update the variable. |
| `Unknown MCP_TRANSPORT ...` in logs | Typo in `MCP_TRANSPORT`; valid values: `stdio`, `sse`, `streamable-http`. |
| Connector connects but tools fail | Check deploy logs — Garmin API errors (rate limiting, expired tokens) appear there. |
