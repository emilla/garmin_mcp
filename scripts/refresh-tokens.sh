#!/usr/bin/env bash
# Refresh Garmin OAuth tokens and push them to a Railway deployment.
#
# Run this when the health endpoint or your claude.ai connector reports the
# Garmin tokens as expired (roughly every 6 months). The only interactive
# step is the MFA prompt — everything else is automatic.
#
# Requirements:
#   - uv (https://docs.astral.sh/uv/)
#   - Railway CLI, authenticated (`railway login`) and linked (`railway link`)
#     in the directory you run this from, OR RAILWAY_TOKEN set.
#
# Configuration (env vars):
#   GARMIN_MCP_REPO   git source for the CLI (default: upstream GitHub repo;
#                     set to your fork, e.g. git+https://github.com/you/garmin_mcp)
#   RAILWAY_SERVICE   Railway service name, if the project has more than one

set -euo pipefail

REPO="${GARMIN_MCP_REPO:-git+https://github.com/Taxuspt/garmin_mcp}"
SERVICE_ARGS=()
if [ -n "${RAILWAY_SERVICE:-}" ]; then
  SERVICE_ARGS=(--service "$RAILWAY_SERVICE")
fi

echo "Step 1/3: re-authenticating with Garmin Connect (expect an MFA prompt)..."
uvx --python 3.12 --from "$REPO" garmin-mcp-auth --force-reauth

echo "Step 2/3: exporting tokens and updating Railway variable GARMINTOKENS_SEED..."
TOKENS="$(uvx --python 3.12 --from "$REPO" garmin-mcp-auth --export 2>/dev/null)"
# GARMINTOKENS itself stays pointed at the volume path; changing the SEED value
# forces the server to overwrite the volume's token on the next deploy.
railway variables ${SERVICE_ARGS[@]+"${SERVICE_ARGS[@]}"} --set "GARMINTOKENS_SEED=$TOKENS" --skip-deploys

echo "Step 3/3: redeploying..."
railway redeploy ${SERVICE_ARGS[@]+"${SERVICE_ARGS[@]}"} --yes

echo "Done. Verify via your health URL or a claude.ai query."
