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
   from your machine (`garmin-mcp-auth --export`) into the `GARMINTOKENS_SEED`
   env var, backed by a persistent volume (`GARMINTOKENS` points at a path on
   it). Garmin rotates refresh tokens continuously; the volume is what makes
   that survive restarts — see "Why a volume" below.
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

# Print the token bundle as a single line (goes to GARMINTOKENS_SEED on Railway)
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
| `GARMINTOKENS` | `/data/garminconnect` | A **path on the persistent volume** (step 3a), NOT the token data. |
| `GARMINTOKENS_SEED` | output of `--export` | The whole line, no quotes. Seeds the volume once; see "Why a volume" below. |
| `MCP_HTTP_PATH` | `/mcp-<random>` from step 2 | This is your access credential. |
| `GARMIN_ENABLED_TOOLS` | e.g. `get_activities_by_date,get_last_activity,get_sleep_data` | Optional but recommended: a read-only allowlist limits blast radius if the URL leaks, and keeps claude.ai context small. |
| `MCP_HEALTH_PATH` | `/health-<random>` | Optional: enables a GET health endpoint for uptime monitoring (see below). |
| `GARMIN_IS_CN` | `true` | Only for Garmin Connect China. |

   `PORT` is injected by Railway; the server reads it automatically and binds
   `0.0.0.0`.

3a. **Add a persistent volume** (this is what keeps you logged in — see below).
   Service → Settings → **Volumes** → add a volume mounted at `/data`. Set
   `GARMINTOKENS=/data/garminconnect` as above.

3b. Deploy. The logs should show:

   ```
   Seeded token store at '/data/garminconnect' from GARMINTOKENS_SEED.
   Garmin Connect client initialized successfully.
   Serving MCP over streamable-http on 0.0.0.0:<port>/mcp-<random>
   ```

4. Settings → Networking → **Generate Domain** to get
   `https://<name>.up.railway.app`.

### Why a volume (read this — it's the #1 cause of "logged out after a day")

Garmin's refresh tokens **rotate**: every ~hour the server exchanges its
refresh token for a new one and Garmin invalidates the old one. If tokens live
only in an env var (inline `GARMINTOKENS` data), the rotated token is held in
memory and lost when the container restarts — the next boot reloads the now-dead
seed token and every call 401s. Railway recycles containers roughly daily, so
the deployment "mysteriously" stops working after about a day.

The volume fixes this: `GARMINTOKENS` points at a path on the volume, so the
server writes each rotated token back to durable disk and reloads the latest one
after any restart. `GARMINTOKENS_SEED` is used **only to populate the volume the
first time** (and whenever you deliberately change it — see the refresh section);
a normal restart never overwrites the rotated token.

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

## Token expiry: alerting and refresh

With the volume in place, tokens refresh themselves indefinitely while the
service sees regular use. You only need to intervene if the refresh token
itself expires — e.g. after a long stretch of downtime, or a Garmin-side
password change / forced logout. Renewing requires an MFA code, which can never
run unattended, but everything around it is automated so you don't have to
remember anything.

**1. Alerting.** Set `MCP_HEALTH_PATH` to an unguessable path (e.g.
`/health-$(openssl rand -hex 16)`). The server then serves a GET endpoint
that performs a real (cheap) Garmin API call and returns `200 {"status":"ok"}`
while tokens work and `503` once they stop. Results are cached for 15 minutes,
so monitors can ping every 5 minutes without hitting Garmin rate limits, and
the endpoint never returns account data.

Point any free uptime monitor (e.g. UptimeRobot) at
`https://<your-domain><MCP_HEALTH_PATH>` with an HTTP(S) check, alert on
non-200. When the tokens die — or the service breaks for any other reason —
you get an email.

**2. Refresh.** When the alert fires, the whole procedure is: re-auth locally,
then update the `GARMINTOKENS_SEED` variable. Because the seed value changed,
the next deploy overwrites the volume's token with the fresh one (an unchanged
seed is always left alone, so this only happens when you intend it).

Easiest — one command (needs the [Railway CLI](https://docs.railway.com/guides/cli)
logged in and linked, a one-time `railway login && railway link`):

```bash
GARMIN_MCP_REPO=git+https://github.com/<you>/garmin_mcp \
  bash scripts/refresh-tokens.sh
```

It re-authenticates (the one MFA prompt), exports the fresh tokens, updates
`GARMINTOKENS_SEED` on Railway, and redeploys. Set `RAILWAY_SERVICE=<name>` if
your project has multiple services.

Or by hand: run `garmin-mcp-auth --force-reauth` then `garmin-mcp-auth --export`
locally, and paste the new value into `GARMINTOKENS_SEED` in the Railway
dashboard (that save auto-redeploys).

## Operational notes

- **SSE buffering:** the MCP SDK already sends
  `Cache-Control: no-cache, no-transform` and `X-Accel-Buffering: no` on its
  event streams, so no proxy-buffering workarounds are needed.
- **Rotating the secret:** change `MCP_HTTP_PATH` and redeploy, then update
  the connector URL in claude.ai.
- **Token expiry:** if the volume's token dies (long downtime, password
  change), API calls 401, the health endpoint goes 503, and on restart the
  logs show `ERROR: OAuth tokens not found...` after a failed load. Run
  `scripts/refresh-tokens.sh` (see above).
- **Also using Claude Desktop locally?** Don't share one exported session
  between desktop and server: whichever refreshes first invalidates the
  other's refresh token. After exporting the seed for the server, run
  `garmin-mcp-auth --force-reauth` once more so your local machine gets its
  own session chain.
- **Token safety:** the server never prints the token value to logs.
- **Persistence:** with the volume mounted and `GARMINTOKENS` set to a path on
  it, rotated refresh tokens survive restarts. Without the volume, expect to be
  logged out within about a day (see "Why a volume" above).
- **Local stdio use is unchanged:** without `MCP_TRANSPORT` set, the server
  behaves exactly as before for Claude Desktop.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| **Works, then 401s after ~a day** | No persistent volume — the rotated refresh token is lost on restart. Add a volume and set `GARMINTOKENS` to a path on it (see "Why a volume"). |
| 404 from the connector URL | Path mismatch — the URL must include `MCP_HTTP_PATH` exactly. |
| `GARMINTOKENS token data is invalid or expired` in logs | You're using inline `GARMINTOKENS` data. Switch to the volume + `GARMINTOKENS_SEED` approach, or re-export and update the value. |
| Refreshed the seed but still 401 | The volume kept its old token because the seed value didn't actually change. Confirm `GARMINTOKENS_SEED` holds the *new* export; the log line `Seeded token store ...` confirms a re-seed happened. |
| `Unknown MCP_TRANSPORT ...` in logs | Typo in `MCP_TRANSPORT`; valid values: `stdio`, `sse`, `streamable-http`. |
| Connector connects but tools fail | Check deploy logs — Garmin API errors (rate limiting, expired tokens) appear there. |
