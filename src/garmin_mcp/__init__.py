"""
Modular MCP Server for Garmin Connect Data
"""

import os
import sys
import base64

import requests
from mcp.server.fastmcp import FastMCP

from garminconnect import Garmin, GarminConnectAuthenticationError, GarminConnectConnectionError, GarminConnectTooManyRequestsError

# Import all modules
from garmin_mcp import activity_management
from garmin_mcp import health_wellness
from garmin_mcp import user_profile
from garmin_mcp import devices
from garmin_mcp import gear_management
from garmin_mcp import weight_management
from garmin_mcp import challenges
from garmin_mcp import training
from garmin_mcp import workouts
from garmin_mcp import workout_templates
from garmin_mcp import data_management
from garmin_mcp import womens_health
from garmin_mcp import nutrition
from garmin_mcp import workout_builders
from garmin_mcp import courses
from garmin_mcp import activity_analysis


def is_interactive_terminal() -> bool:
    """Detect if running in interactive terminal vs MCP subprocess.

    Returns:
        bool: True if running in an interactive terminal, False otherwise
    """
    return sys.stdin.isatty() and sys.stdout.isatty()


def get_mfa() -> str:
    """Get MFA code from user input.

    Raises:
        RuntimeError: If running in non-interactive environment
    """
    if not is_interactive_terminal():
        print(
            "\nERROR: MFA code required but no interactive terminal available.\n"
            "Please run 'garmin-mcp-auth' in your terminal first.\n"
            "See: https://github.com/Taxuspt/garmin_mcp#mfa-setup\n",
            file=sys.stderr,
        )
        raise RuntimeError("MFA required but non-interactive environment")

    print(
        "\nGarmin Connect MFA required. Please check your email/phone for the code.",
        file=sys.stderr,
    )
    return input("Enter MFA code: ")


# Get credentials from environment
email = os.environ.get("GARMIN_EMAIL")
email_file = os.environ.get("GARMIN_EMAIL_FILE")
if email and email_file:
    raise ValueError(
        "Must only provide one of GARMIN_EMAIL and GARMIN_EMAIL_FILE, got both"
    )
elif email_file:
    with open(email_file, "r") as email_file:
        email = email_file.read().rstrip()

password = os.environ.get("GARMIN_PASSWORD")
password_file = os.environ.get("GARMIN_PASSWORD_FILE")
if password and password_file:
    raise ValueError(
        "Must only provide one of GARMIN_PASSWORD and GARMIN_PASSWORD_FILE, got both"
    )
elif password_file:
    with open(password_file, "r") as password_file:
        password = password_file.read().rstrip()

tokenstore = os.getenv("GARMINTOKENS") or "~/.garminconnect"
tokenstore_base64 = os.getenv("GARMINTOKENS_BASE64") or "~/.garminconnect_base64"
is_cn = os.getenv("GARMIN_IS_CN", "false").lower() in ("true", "1", "yes")


def _seed_tokenstore(tokenstore_path, seed):
    """Seed a persistent tokenstore directory from exported token data.

    Garmin's DI refresh tokens rotate on every refresh: each refresh returns a
    new refresh token and invalidates the previous one. The library only
    persists the rotated token when tokens were loaded from a *path* (which
    sets the client's tokenstore path); inline GARMINTOKENS *data* cannot be
    written back, so on the next container restart the stale seed token is
    already dead -> 401.

    For remote deployments (see RAILWAY.md) mount a persistent volume, point
    GARMINTOKENS at a path on it, and provide the exported token JSON via
    GARMINTOKENS_SEED. This seeds the volume; thereafter the volume holds the
    continuously-rotated token and survives restarts.

    A fingerprint of the seed is stored alongside the token file so that a
    normal restart (seed unchanged) never clobbers the rotated token, while
    changing GARMINTOKENS_SEED and redeploying forces a fresh token — the
    entire ~6-month refresh procedure.

    Returns True if a seed file was written, False otherwise.
    """
    import hashlib

    if not seed:
        return False
    # Only seed a path-style tokenstore, never inline token data.
    if len(tokenstore_path) > 512:
        print(
            "WARNING: GARMINTOKENS_SEED is set but GARMINTOKENS holds inline token "
            "data, so the seed is ignored and rotated tokens will NOT persist "
            "across restarts. Set GARMINTOKENS to a directory path on a "
            "persistent volume (see RAILWAY.md).",
            file=sys.stderr,
        )
        return False

    token_dir = os.path.expanduser(tokenstore_path)
    token_file = os.path.join(token_dir, "garmin_tokens.json")
    fingerprint_file = os.path.join(token_dir, ".seed_fingerprint")
    seed_fingerprint = hashlib.sha256(seed.encode()).hexdigest()

    if os.path.exists(token_file):
        existing = None
        if os.path.exists(fingerprint_file):
            with open(fingerprint_file) as f:
                existing = f.read().strip()
        if existing == seed_fingerprint:
            return False  # unchanged seed: preserve the rotated token
        if existing is None:
            # Token predates fingerprinting; adopt the current seed as the
            # baseline without clobbering a possibly-rotated token.
            print(
                "Existing token store found; GARMINTOKENS_SEED was NOT applied "
                "(adopted as baseline). To force a re-seed, change the "
                "GARMINTOKENS_SEED value and redeploy.",
                file=sys.stderr,
            )
            with open(fingerprint_file, "w") as f:
                f.write(seed_fingerprint)
            return False
        # Seed value changed: an intentional re-seed. Replace the token.

    os.makedirs(token_dir, exist_ok=True)
    with open(token_file, "w") as f:
        f.write(seed)
    with open(fingerprint_file, "w") as f:
        f.write(seed_fingerprint)
    try:
        os.chmod(token_dir, 0o700)
    except OSError:
        pass
    return True


# --- Tool filtering ---------------------------------------------------------
# Optionally expose only a subset of tools, to reduce the context an LLM must
# carry. No modules are removed; tools are simply not registered when filtered.
#   GARMIN_ENABLED_TOOLS  - comma-separated allowlist; if set, ONLY these register
#   GARMIN_DISABLED_TOOLS - comma-separated denylist; ignored if an allowlist is set
# Tool names are case-insensitive. Unset = all tools register (default behaviour).
def _parse_tool_set(value):
    if not value:
        return set()
    return {name.strip().lower() for name in value.split(",") if name.strip()}


enabled_tools = _parse_tool_set(os.getenv("GARMIN_ENABLED_TOOLS"))
disabled_tools = _parse_tool_set(os.getenv("GARMIN_DISABLED_TOOLS"))


class _ToolFilter:
    """Wraps a FastMCP app to conditionally register tools by function name.

    Modules register via ``@app.tool()``; we intercept that decorator and skip
    registration for any tool not permitted by the env-var filter. All other
    attribute access (``run``, ``resource``, ...) passes through to the app.
    """

    def __init__(self, app, enabled, disabled):
        self._app = app
        self._enabled = enabled
        self._disabled = disabled
        self._seen = set()  # tool names encountered, for typo detection

    def _allowed(self, name):
        name = name.lower()
        if self._enabled:
            return name in self._enabled
        return name not in self._disabled

    def tool(self, *args, **kwargs):
        decorator = self._app.tool(*args, **kwargs)
        # Prefer the explicit registered name if given (@app.tool(name="x")),
        # so the env-var filter matches what the user actually configures.
        explicit = kwargs.get("name") or (
            args[0] if args and isinstance(args[0], str) else None
        )

        def wrapper(fn):
            name = explicit or getattr(fn, "__name__", "")
            self._seen.add(name.lower())
            if self._allowed(name):
                return decorator(fn)
            return fn  # skip registration; tool never reaches the LLM

        return wrapper

    def unknown_filter_names(self):
        """Configured names that never matched a real tool (likely typos)."""
        configured = self._enabled or self._disabled
        return sorted(configured - self._seen)

    def __getattr__(self, item):
        return getattr(self._app, item)
# ---------------------------------------------------------------------------


def init_api(email, password):
    """Initialize Garmin API with your credentials."""
    import io

    # Seed a persistent tokenstore from GARMINTOKENS_SEED on first boot so that
    # rotated refresh tokens can be written back and survive restarts.
    if _seed_tokenstore(tokenstore, os.getenv("GARMINTOKENS_SEED")):
        print(
            f"Seeded token store at '{tokenstore}' from GARMINTOKENS_SEED.",
            file=sys.stderr,
        )

    # GARMINTOKENS may hold a directory path or, for headless deployments
    # (e.g. Railway), the raw token data printed by `garmin-mcp-auth --export`.
    # garminconnect treats values longer than 512 chars as token data. Never
    # echo the value itself: token data grants full access to the account.
    token_is_data = len(tokenstore) > 512
    token_source = (
        "GARMINTOKENS environment data" if token_is_data else f"directory '{tokenstore}'"
    )

    try:
        # Using Oauth1 and OAuth2 token files from directory
        print(
            f"Trying to login to Garmin Connect using token data from {token_source}...\n",
            file=sys.stderr,
        )

        # Using Oauth1 and Oauth2 tokens from base64 encoded string
        # print(
        #     f"Trying to login to Garmin Connect using token data from file '{tokenstore_base64}'...\n"
        # )
        # dir_path = os.path.expanduser(tokenstore_base64)
        # with open(dir_path, "r") as token_file:
        #     tokenstore = token_file.read()

        # Suppress stderr for token validation to avoid confusing library errors
        old_stderr = sys.stderr
        sys.stderr = io.StringIO()

        try:
            garmin = Garmin(is_cn=is_cn)
            garmin.login(tokenstore)
        finally:
            sys.stderr = old_stderr

    except (FileNotFoundError, ValueError, GarminConnectConnectionError, GarminConnectTooManyRequestsError, GarminConnectAuthenticationError):
        # Session is expired. You'll need to log in again

        if token_is_data:
            # Headless deployment with inline token data: re-authentication is
            # not possible here, and the credential fallback below would try to
            # dump tokens to the data string as if it were a path.
            print(
                "ERROR: GARMINTOKENS token data is invalid or expired.\n"
                "Re-export fresh tokens with 'garmin-mcp-auth --export' and update\n"
                "the GARMINTOKENS environment variable on your server.\n",
                file=sys.stderr,
            )
            return None

        # Check if we're in a non-interactive environment without credentials
        if not is_interactive_terminal() and (not email or not password):
            print(
                "ERROR: OAuth tokens not found and no interactive terminal available.\n"
                "Please authenticate first:\n"
                "  1. Run: garmin-mcp-auth\n"
                "  2. Enter your credentials and MFA code\n"
                "  3. Restart your MCP client\n"
                f"Tokens will be saved to: {tokenstore}\n",
                file=sys.stderr,
            )
            return None

        print(
            "Login tokens not present, login with your Garmin Connect credentials to generate them.\n"
            f"They will be stored in '{tokenstore}' for future use.\n",
            file=sys.stderr,
        )
        try:
            garmin = Garmin(
                email=email, password=password, is_cn=is_cn, prompt_mfa=get_mfa, return_on_mfa=True
            )
            result1, result2 = garmin.login()
            if result1 == "needs_mfa":
                mfa_code = get_mfa()
                garmin.resume_login(result2, mfa_code)
            # Save Oauth1 and Oauth2 token files to directory for next login
            garmin.client.dump(tokenstore)
            print(
                f"Oauth tokens stored in '{tokenstore}' directory for future use. (first method)\n",
                file=sys.stderr,
            )
            # Encode Oauth1 and Oauth2 tokens to base64 string and save to file for next login (alternative way)
            expanded_tokenstore = os.path.expanduser(tokenstore)
            token_json_path = os.path.join(expanded_tokenstore, "garmin_tokens.json")
            with open(token_json_path, "r") as f:
                token_data = f.read()
            token_base64 = base64.b64encode(token_data.encode()).decode()
            dir_path = os.path.expanduser(tokenstore_base64)
            with open(dir_path, "w") as token_file:
                token_file.write(token_base64)
            print(
                f"Oauth tokens encoded as base64 string and saved to '{dir_path}' file for future use. (second method)\n",
                file=sys.stderr,
            )
        except (
            FileNotFoundError,
            GarminConnectConnectionError,
            GarminConnectTooManyRequestsError,
            GarminConnectAuthenticationError,
            requests.exceptions.HTTPError,
        ) as err:
            error_msg = str(err)

            # Provide clean, actionable error messages
            print("\nAuthentication failed.", file=sys.stderr)

            if isinstance(err, GarminConnectAuthenticationError):
                if "MFA" in error_msg or "code" in error_msg.lower():
                    print("MFA code may be incorrect or expired.", file=sys.stderr)
                else:
                    print("Invalid email or password.", file=sys.stderr)
            elif isinstance(err, GarminConnectTooManyRequestsError):
                print(
                    "Too many requests. Please wait and try again.", file=sys.stderr
                )
            elif isinstance(err, GarminConnectConnectionError):
                if "401" in error_msg or "Unauthorized" in error_msg:
                    print(
                        "Invalid credentials. Please check your email and password.",
                        file=sys.stderr,
                    )
                elif "500" in error_msg or "503" in error_msg:
                    print(
                        "Garmin Connect service issue. Please try again later.",
                        file=sys.stderr,
                    )
                else:
                    print(f"Error: {error_msg.split(':')[0]}", file=sys.stderr)
            elif isinstance(err, requests.exceptions.HTTPError):
                print("Network error. Please check your connection.", file=sys.stderr)
            else:
                print(f"Error: {error_msg.split(':')[0]}", file=sys.stderr)

            print(
                f"\nTip: Run 'garmin-mcp-auth' to authenticate interactively.",
                file=sys.stderr,
            )
            return None

    return garmin


# --- Transport selection -----------------------------------------------------
# MCP_TRANSPORT selects how the server is exposed:
#   stdio (default)   - spawned by a local MCP client such as Claude Desktop
#   streamable-http   - remote deployments (Railway etc.); claude.ai connects here
#   sse               - legacy HTTP transport
VALID_TRANSPORTS = ("stdio", "sse", "streamable-http")


def _transport_from_env(environ=None):
    """Resolve the MCP transport from MCP_TRANSPORT, defaulting to stdio."""
    environ = os.environ if environ is None else environ
    transport = environ.get("MCP_TRANSPORT", "stdio").strip().lower().replace("_", "-")
    if transport not in VALID_TRANSPORTS:
        print(
            f"Unknown MCP_TRANSPORT '{transport}'; expected one of: "
            f"{', '.join(VALID_TRANSPORTS)}. Falling back to stdio.",
            file=sys.stderr,
        )
        return "stdio"
    return transport


def _http_settings_from_env(environ=None):
    """FastMCP host/port/path settings for the HTTP transports.

    PORT (injected by PaaS hosts like Railway) takes precedence over
    MCP_HTTP_PORT. MCP_HTTP_PATH may carry a secret suffix (e.g.
    /mcp-<random>) so an otherwise unauthenticated endpoint is unguessable.
    """
    environ = os.environ if environ is None else environ
    path = environ.get("MCP_HTTP_PATH", "/mcp")
    if not path.startswith("/"):
        path = "/" + path
    return {
        "host": environ.get("MCP_HTTP_HOST", "0.0.0.0"),
        "port": int(environ.get("PORT") or environ.get("MCP_HTTP_PORT") or "8000"),
        "streamable_http_path": path,
    }
# ------------------------------------------------------------------------------


# --- Health endpoint ----------------------------------------------------------
# Garmin tokens expire after ~6 months and can only be renewed interactively
# (MFA). The health endpoint lets an uptime monitor detect expiry: it performs
# a real (cheap) Garmin API call and returns 200/503, caching the result so
# frequent pings don't hammer Garmin. It never returns account data.
HEALTH_CACHE_SECONDS = 900


def _make_health_endpoint(garmin_client, cache_seconds=HEALTH_CACHE_SECONDS, clock=None):
    import time as _time

    from starlette.responses import JSONResponse

    clock = clock or _time.monotonic
    cache = {"checked_at": None, "ok": False, "reason": None}

    async def health(request):
        import anyio

        now = clock()
        if cache["checked_at"] is None or now - cache["checked_at"] >= cache_seconds:
            try:
                await anyio.to_thread.run_sync(garmin_client.get_userprofile_settings)
                cache.update(checked_at=now, ok=True, reason=None)
            except Exception as err:
                cache.update(checked_at=now, ok=False, reason=type(err).__name__)
        if cache["ok"]:
            return JSONResponse({"status": "ok"})
        return JSONResponse(
            {
                "status": "error",
                "reason": cache["reason"],
                "hint": "Garmin call failed; if this persists, refresh tokens "
                "with scripts/refresh-tokens.sh",
            },
            status_code=503,
        )

    return health
# ------------------------------------------------------------------------------


def main():
    """Initialize the MCP server and register all tools"""

    # Initialize Garmin client
    garmin_client = init_api(email, password)
    if not garmin_client:
        print("Failed to initialize Garmin Connect client. Exiting.", file=sys.stderr)
        return

    print("Garmin Connect client initialized successfully.", file=sys.stderr)

    # Configure all modules with the Garmin client
    activity_management.configure(garmin_client)
    health_wellness.configure(garmin_client)
    user_profile.configure(garmin_client)
    devices.configure(garmin_client)
    gear_management.configure(garmin_client)
    weight_management.configure(garmin_client)
    challenges.configure(garmin_client)
    training.configure(garmin_client)
    workouts.configure(garmin_client)
    data_management.configure(garmin_client)
    womens_health.configure(garmin_client)
    nutrition.configure(garmin_client)
    workout_builders.configure(garmin_client)
    courses.configure(garmin_client)
    activity_analysis.configure(garmin_client)

    transport = _transport_from_env()
    http_settings = _http_settings_from_env() if transport != "stdio" else {}

    # Create the MCP app, wrapped so the env-var filter can drop tools
    app = _ToolFilter(
        FastMCP("Garmin Connect v1.0", **http_settings), enabled_tools, disabled_tools
    )
    if enabled_tools:
        print(f"Tool filter: allowlist of {len(enabled_tools)} tool(s).", file=sys.stderr)
    elif disabled_tools:
        print(f"Tool filter: denylist of {len(disabled_tools)} tool(s).", file=sys.stderr)

    # Register tools from all modules
    app = activity_management.register_tools(app)
    app = health_wellness.register_tools(app)
    app = user_profile.register_tools(app)
    app = devices.register_tools(app)
    app = gear_management.register_tools(app)
    app = weight_management.register_tools(app)
    app = challenges.register_tools(app)
    app = training.register_tools(app)
    app = workouts.register_tools(app)
    app = data_management.register_tools(app)
    app = womens_health.register_tools(app)
    app = nutrition.register_tools(app)
    app = workout_builders.register_tools(app)
    app = courses.register_tools(app)
    app = activity_analysis.register_tools(app)

    # Register resources (workout templates)
    app = workout_templates.register_resources(app)

    # Warn about filter entries that matched no tool (most likely typos)
    unknown = app.unknown_filter_names()
    if unknown:
        print(
            f"Tool filter: warning — name(s) not found and ignored: {', '.join(unknown)}",
            file=sys.stderr,
        )

    # Optional uptime-monitor endpoint, HTTP transports only
    health_path = os.getenv("MCP_HEALTH_PATH", "").strip()
    if transport != "stdio" and health_path:
        if not health_path.startswith("/"):
            health_path = "/" + health_path
        app.custom_route(health_path, methods=["GET"])(
            _make_health_endpoint(garmin_client)
        )
        print(f"Health endpoint enabled at {health_path}", file=sys.stderr)

    # Run the MCP server
    if transport != "stdio":
        print(
            f"Serving MCP over {transport} on "
            f"{http_settings['host']}:{http_settings['port']}"
            f"{http_settings['streamable_http_path']}",
            file=sys.stderr,
        )
    app.run(transport)


if __name__ == "__main__":
    main()
