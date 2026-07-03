"""Unit tests for the tokenstore seeding logic (_seed_tokenstore)."""

import os

from garmin_mcp import _seed_tokenstore

SEED = '{"di_token": "a.b.c", "di_refresh_token": "r0", "di_client_id": "cid"}'


def test_no_seed_is_noop(tmp_path):
    assert _seed_tokenstore(str(tmp_path), None) is False
    assert _seed_tokenstore(str(tmp_path), "") is False
    assert not list(tmp_path.iterdir())


def test_writes_seed_when_dir_empty(tmp_path):
    target = tmp_path / "garminconnect"
    assert _seed_tokenstore(str(target), SEED) is True
    token_file = target / "garmin_tokens.json"
    assert token_file.exists()
    assert token_file.read_text() == SEED


def test_does_not_clobber_existing_token_predating_fingerprint(tmp_path):
    target = tmp_path / "garminconnect"
    target.mkdir()
    existing = target / "garmin_tokens.json"
    existing.write_text('{"di_refresh_token": "ROTATED"}')  # simulates rotated token

    assert _seed_tokenstore(str(target), SEED) is False
    # The rotated token on the volume must be preserved, not overwritten
    assert existing.read_text() == '{"di_refresh_token": "ROTATED"}'
    # A baseline fingerprint is adopted so future seed changes are detectable
    assert (target / ".seed_fingerprint").exists()


def test_unchanged_seed_preserves_rotated_token(tmp_path):
    target = tmp_path / "garminconnect"
    # First boot seeds cleanly
    assert _seed_tokenstore(str(target), SEED) is True
    # Simulate the library rotating the refresh token on the volume
    (target / "garmin_tokens.json").write_text('{"di_refresh_token": "R5"}')

    # A normal restart with the same seed must NOT clobber the rotated token
    assert _seed_tokenstore(str(target), SEED) is False
    assert (target / "garmin_tokens.json").read_text() == '{"di_refresh_token": "R5"}'


def test_changed_seed_forces_reseed(tmp_path):
    target = tmp_path / "garminconnect"
    assert _seed_tokenstore(str(target), SEED) is True
    (target / "garmin_tokens.json").write_text('{"di_refresh_token": "R5"}')

    # Updating GARMINTOKENS_SEED (e.g. after re-auth) forces a fresh token
    new_seed = '{"di_token": "x.y.z", "di_refresh_token": "NEW", "di_client_id": "cid"}'
    assert _seed_tokenstore(str(target), new_seed) is True
    assert (target / "garmin_tokens.json").read_text() == new_seed


def test_inline_token_data_is_not_seeded(tmp_path):
    # A >512 char value is inline token data, not a path; nothing should be written.
    inline = "x" * 600
    assert _seed_tokenstore(inline, SEED) is False


def test_directory_created_with_restrictive_permissions(tmp_path):
    target = tmp_path / "nested" / "garminconnect"
    assert _seed_tokenstore(str(target), SEED) is True
    assert target.exists()
    mode = os.stat(target).st_mode & 0o777
    assert mode == 0o700
