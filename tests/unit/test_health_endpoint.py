"""Unit tests for the uptime-monitor health endpoint (_make_health_endpoint)."""

import json
from unittest.mock import MagicMock

from garmin_mcp import _make_health_endpoint


def _body(response):
    return json.loads(response.body)


class FakeClock:
    """Controllable monotonic clock."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


async def test_healthy_garmin_returns_200():
    garmin = MagicMock()
    handler = _make_health_endpoint(garmin, cache_seconds=900, clock=FakeClock())

    response = await handler(None)
    assert response.status_code == 200
    assert _body(response) == {"status": "ok"}
    garmin.get_userprofile_settings.assert_called_once()


async def test_failing_garmin_returns_503_with_reason():
    garmin = MagicMock()
    garmin.get_userprofile_settings.side_effect = RuntimeError("expired")
    handler = _make_health_endpoint(garmin, cache_seconds=900, clock=FakeClock())

    response = await handler(None)
    assert response.status_code == 503
    body = _body(response)
    assert body["status"] == "error"
    assert body["reason"] == "RuntimeError"


async def test_result_is_cached_within_window():
    garmin = MagicMock()
    clock = FakeClock()
    handler = _make_health_endpoint(garmin, cache_seconds=900, clock=clock)

    await handler(None)
    clock.now += 300  # within cache window
    response = await handler(None)

    assert response.status_code == 200
    garmin.get_userprofile_settings.assert_called_once()


async def test_garmin_reprobed_after_cache_expires():
    garmin = MagicMock()
    clock = FakeClock()
    handler = _make_health_endpoint(garmin, cache_seconds=900, clock=clock)

    await handler(None)
    clock.now += 901
    await handler(None)

    assert garmin.get_userprofile_settings.call_count == 2


async def test_failure_is_cached_then_recovers():
    garmin = MagicMock()
    garmin.get_userprofile_settings.side_effect = RuntimeError("expired")
    clock = FakeClock()
    handler = _make_health_endpoint(garmin, cache_seconds=900, clock=clock)

    response = await handler(None)
    assert response.status_code == 503

    # Failure is cached: no re-probe within the window
    clock.now += 300
    response = await handler(None)
    assert response.status_code == 503
    garmin.get_userprofile_settings.assert_called_once()

    # After the window, a recovered Garmin flips the endpoint back to 200
    garmin.get_userprofile_settings.side_effect = None
    clock.now += 900
    response = await handler(None)
    assert response.status_code == 200
