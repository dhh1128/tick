"""Tests for tick.update — self-update + the throttled, offline-safe nag.

Network is never touched: manifests are passed as local file paths and payloads
via injected fetchers, so these stay fast and deterministic.
"""

import json
import os
import stat
from pathlib import Path

import pytest

import tick
from tick import update


def _write_manifest(tmp_path, *, latest, sha256="00", script_url="file:///x") -> Path:
    p = tmp_path / "update.json"
    p.write_text(json.dumps({"latest_version": latest, "sha256": sha256, "script_url": script_url}))
    return p


# ----------------------------------------------------------------- version math


def test_parse_and_compare_versions():
    assert update.parse_version("1.0.1") < update.parse_version("1.2.0")
    assert update.parse_version("2.0.0") > update.parse_version("1.9.9")
    assert update.parse_version("1.0.0") == update.parse_version("1.0.0")


def test_check_update_available_and_current(tmp_path):
    newer = _write_manifest(tmp_path, latest="999.0.0")
    st = update.check_update(manifest=str(newer))
    assert st.current_version == tick.__version__
    assert st.latest_version == "999.0.0"
    assert st.update_available is True

    older = _write_manifest(tmp_path, latest="0.0.1")
    assert update.check_update(manifest=str(older)).update_available is False


def test_check_update_via_injected_fetcher():
    fetched = update.check_update(
        manifest="https://example/update.json",
        fetcher=lambda url: b'{"latest_version": "999.0.0"}',
    )
    assert fetched.update_available is True


# --------------------------------------------------------------- atomic replace


def test_atomic_replace_swaps_content_and_keeps_exec_bit(tmp_path):
    target = tmp_path / "tick"
    target.write_text("old")
    target.chmod(0o755)
    update.atomic_replace(target, b"new-bytes")
    assert target.read_bytes() == b"new-bytes"
    assert target.stat().st_mode & stat.S_IXUSR


# ------------------------------------------------------------------ apply_update


def test_apply_update_verifies_sha256_and_replaces(tmp_path):
    payload = b"#!/usr/bin/env python3\n# new tick\n"
    manifest = _write_manifest(tmp_path, latest="999.0.0", sha256=update.sha256_hex(payload))
    target = tmp_path / "tick"
    target.write_text("old")
    target.chmod(0o755)

    st = update.apply_update(target=target, manifest=str(manifest), payload_fetcher=lambda url: payload)
    assert st.update_available is True
    assert target.read_bytes() == payload


def test_apply_update_rejects_sha256_mismatch_and_leaves_target(tmp_path):
    manifest = _write_manifest(tmp_path, latest="999.0.0", sha256="deadbeef")
    target = tmp_path / "tick"
    target.write_text("old")
    with pytest.raises(update.UpdateError, match="sha256"):
        update.apply_update(target=target, manifest=str(manifest), payload_fetcher=lambda url: b"tampered")
    assert target.read_text() == "old"  # untouched


def test_apply_update_noop_when_current(tmp_path):
    manifest = _write_manifest(tmp_path, latest="0.0.1", sha256="x")
    target = tmp_path / "tick"
    target.write_text("old")
    st = update.apply_update(target=target, manifest=str(manifest),
                             payload_fetcher=lambda url: (_ for _ in ()).throw(AssertionError("should not fetch")))
    assert st.update_available is False
    assert target.read_text() == "old"


# ------------------------------------------------------------------- target res


def test_resolve_target_prefers_explicit_path(tmp_path):
    p = tmp_path / "bin" / "tick"
    p.parent.mkdir()
    p.write_text("x")
    assert update.resolve_target(str(p)) == p.resolve()


# -------------------------------------------------------------------- the nag


def _nag(capsys, **kw):
    update.maybe_notify_update(kw.pop("command", "ls"), **kw)
    return capsys.readouterr().err


def test_nag_only_on_listed_commands(tmp_path, capsys):
    manifest = _write_manifest(tmp_path, latest="999.0.0")
    # add is on the hot write path -> never nags, never touches the network
    err = _nag(capsys, command="add", manifest=str(manifest),
               cache_path=tmp_path / "c.json", now=1000.0)
    assert err == ""


def test_nag_fetches_on_cold_cache_and_writes_it(tmp_path, capsys):
    cache = tmp_path / "c.json"
    manifest = _write_manifest(tmp_path, latest="999.0.0")
    err = _nag(capsys, manifest=str(manifest), cache_path=cache, now=1000.0)
    assert "999.0.0" in err and "tick update" in err
    saved = json.loads(cache.read_text())
    assert saved["latest_version"] == "999.0.0" and saved["checked_at"] == 1000.0


def test_nag_uses_cache_within_ttl_without_network(tmp_path, capsys):
    cache = tmp_path / "c.json"
    cache.write_text(json.dumps({"latest_version": "999.0.0", "checked_at": 1000.0}))

    def boom(url):
        raise AssertionError("network must not be hit within TTL")

    err = _nag(capsys, manifest="https://example/update.json", fetcher=boom,
               cache_path=cache, now=1000.0 + 3600)  # 1h later, ttl is a day
    assert "999.0.0" in err


def test_nag_refreshes_after_ttl(tmp_path, capsys):
    cache = tmp_path / "c.json"
    cache.write_text(json.dumps({"latest_version": "0.0.1", "checked_at": 0.0}))
    err = _nag(capsys, manifest="https://example/update.json",
               fetcher=lambda url: b'{"latest_version": "999.0.0"}',
               cache_path=cache, now=10 * 86400.0)  # well past ttl
    assert "999.0.0" in err
    assert json.loads(cache.read_text())["latest_version"] == "999.0.0"


def test_nag_silent_when_offline(tmp_path, capsys):
    def boom(url):
        raise OSError("offline")

    err = _nag(capsys, manifest="https://example/update.json", fetcher=boom,
               cache_path=tmp_path / "c.json", now=1000.0)
    assert err == ""


def test_nag_silent_when_current(tmp_path, capsys):
    manifest = _write_manifest(tmp_path, latest="0.0.1")
    err = _nag(capsys, manifest=str(manifest), cache_path=tmp_path / "c.json", now=1000.0)
    assert err == ""


def test_nag_opt_out_via_flag_and_env(tmp_path, capsys, monkeypatch):
    manifest = _write_manifest(tmp_path, latest="999.0.0")
    assert _nag(capsys, manifest=str(manifest), cache_path=tmp_path / "c.json",
                now=1000.0, no_check=True) == ""
    monkeypatch.setenv("TICK_NO_UPDATE_CHECK", "1")
    assert _nag(capsys, manifest=str(manifest), cache_path=tmp_path / "c.json", now=1000.0) == ""
