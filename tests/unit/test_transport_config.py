"""Unit tests for MCP transport selection (_transport_from_env, _http_settings_from_env)."""

from garmin_mcp import _transport_from_env, _http_settings_from_env


class TestTransportFromEnv:
    """Tests for _transport_from_env."""

    def test_default_is_stdio(self):
        assert _transport_from_env({}) == "stdio"

    def test_streamable_http(self):
        assert _transport_from_env({"MCP_TRANSPORT": "streamable-http"}) == "streamable-http"

    def test_sse(self):
        assert _transport_from_env({"MCP_TRANSPORT": "sse"}) == "sse"

    def test_underscores_normalized(self):
        assert _transport_from_env({"MCP_TRANSPORT": "streamable_http"}) == "streamable-http"

    def test_case_and_whitespace_normalized(self):
        assert _transport_from_env({"MCP_TRANSPORT": "  Streamable-HTTP "}) == "streamable-http"

    def test_unknown_falls_back_to_stdio(self, capsys):
        assert _transport_from_env({"MCP_TRANSPORT": "websocket"}) == "stdio"
        assert "Unknown MCP_TRANSPORT 'websocket'" in capsys.readouterr().err


class TestHttpSettingsFromEnv:
    """Tests for _http_settings_from_env."""

    def test_defaults(self):
        settings = _http_settings_from_env({})
        assert settings == {
            "host": "0.0.0.0",
            "port": 8000,
            "streamable_http_path": "/mcp",
        }

    def test_port_env_wins_over_mcp_http_port(self):
        settings = _http_settings_from_env({"PORT": "9100", "MCP_HTTP_PORT": "9200"})
        assert settings["port"] == 9100

    def test_mcp_http_port_used_without_port(self):
        settings = _http_settings_from_env({"MCP_HTTP_PORT": "9200"})
        assert settings["port"] == 9200

    def test_empty_port_falls_back(self):
        settings = _http_settings_from_env({"PORT": ""})
        assert settings["port"] == 8000

    def test_custom_host_and_path(self):
        settings = _http_settings_from_env(
            {"MCP_HTTP_HOST": "127.0.0.1", "MCP_HTTP_PATH": "/mcp-s3cret"}
        )
        assert settings["host"] == "127.0.0.1"
        assert settings["streamable_http_path"] == "/mcp-s3cret"

    def test_path_gets_leading_slash(self):
        settings = _http_settings_from_env({"MCP_HTTP_PATH": "mcp-s3cret"})
        assert settings["streamable_http_path"] == "/mcp-s3cret"
