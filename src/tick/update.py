"""tick.update — self-update from GitHub Releases + a throttled update nag.

Zero runtime deps (stdlib only). tick is offline-first, so every network path
here fails *silently* for the nag and *clearly but non-destructively* for an
explicit `tick update`. The downloadable artifact is the single-file zipapp; an
`update.json` manifest published beside it carries `latest_version` and the
`sha256` we verify before atomically replacing the running binary.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen

from tick import __version__

REPO = "dhh1128/tick"
DEFAULT_MANIFEST_URL = f"https://github.com/{REPO}/releases/latest/download/update.json"
DEFAULT_BINARY_URL = f"https://github.com/{REPO}/releases/latest/download/tick"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"

# The nag checks the network at most once per this window; within it, the cached
# answer is reused so reads stay instant and offline-safe.
CHECK_TTL_SECONDS = 24 * 60 * 60
# Only the "dashboard" read nags — never the hot write path (add/note/mark/off).
NAG_COMMANDS = {"ls"}


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class UpdateStatus:
    current_version: str
    latest_version: str
    update_available: bool
    script_url: str | None = None
    sha256: str | None = None


def parse_version(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.split(r"[.\-+]", value) if part.isdigit())


# --------------------------------------------------------------------- fetching

_GH_LATEST_RE = re.compile(
    r"https://github\.com/(?P<repo>[^/]+/[^/]+)/releases/latest/download/(?P<filename>[^/?]+)"
)


def _gh_fetch(url: str) -> bytes | None:
    """Fetch a `releases/latest/download/<file>` asset via the gh CLI, so private
    repos work without baking in a token. Returns None for a non-matching URL."""
    m = _GH_LATEST_RE.match(url)
    if not m:
        return None
    cmd = ["gh", "release", "download", "--repo", m.group("repo"),
           "--pattern", m.group("filename"), "--output", "-"]
    try:
        result = subprocess.run(cmd, capture_output=True)
    except FileNotFoundError:
        return None  # no gh — fall back to urlopen
    if result.returncode != 0:
        raise UpdateError(
            f"gh release download failed for {url}:\n"
            f"  {result.stderr.decode().strip() or 'no output'}\n"
            f"  Download manually from: {RELEASES_PAGE}"
        )
    return result.stdout


def fetch_bytes(url: str, timeout: float = 10.0) -> bytes:
    payload = _gh_fetch(url)
    if payload is not None:
        return payload
    with urlopen(url, timeout=timeout) as response:  # noqa: S310 (https only by construction)
        return response.read()


def _read_source(source: str, fetcher=None) -> bytes:
    if source.startswith("file://"):
        return Path(source[7:]).read_bytes()
    if source.startswith(("http://", "https://")):
        return (fetcher or fetch_bytes)(source)
    return Path(source).read_bytes()


def load_manifest(manifest: str | None = None, fetcher=None) -> dict:
    source = manifest or DEFAULT_MANIFEST_URL
    raw = _read_source(source, fetcher)
    return json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)


def check_update(manifest: str | None = None, fetcher=None) -> UpdateStatus:
    data = load_manifest(manifest, fetcher)
    latest = data["latest_version"]
    return UpdateStatus(
        current_version=__version__,
        latest_version=latest,
        update_available=parse_version(latest) > parse_version(__version__),
        script_url=data.get("script_url", DEFAULT_BINARY_URL),
        sha256=data.get("sha256"),
    )


# ------------------------------------------------------------------- self-replace


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def atomic_replace(target: Path, payload: bytes) -> None:
    """Write `payload` over `target` atomically, preserving the executable bit —
    a half-written tick on PATH would be a nasty failure mode."""
    target = target.resolve()
    mode = target.stat().st_mode if target.exists() else 0o755
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        tmp.chmod(mode | stat.S_IXUSR)
        os.replace(tmp, target)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def resolve_target(argv0: str) -> Path:
    """Find the on-disk binary to replace. An explicit/relative path is used as
    given; a bare name (invoked via PATH) is resolved with `which`."""
    candidate = Path(argv0)
    if candidate.is_absolute() or candidate.parent != Path("."):
        return candidate.resolve()
    found = shutil.which(argv0)
    return Path(found).resolve() if found else candidate.resolve()


def apply_update(*, target: Path, manifest: str | None = None,
                 manifest_fetcher=None, payload_fetcher=None) -> UpdateStatus:
    status = check_update(manifest, manifest_fetcher)
    if not status.update_available:
        return status
    if not status.sha256:
        raise UpdateError("update manifest is missing sha256")
    url = status.script_url or DEFAULT_BINARY_URL
    payload = payload_fetcher(url) if payload_fetcher else _read_source(url)
    if not hmac.compare_digest(sha256_hex(payload), status.sha256):
        raise UpdateError("downloaded tick sha256 does not match the manifest — refusing to install")
    atomic_replace(target, payload)
    return status


# ------------------------------------------------------------------- the nag


def default_cache_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(base) / "tick" / "update-check.json"


def _read_cache(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _write_cache(path: Path, latest_version: str, checked_at: float) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"latest_version": latest_version, "checked_at": checked_at}))
    except OSError:
        pass  # a missing cache just means we recheck next time — never fatal


def maybe_notify_update(
    command: str,
    *,
    manifest: str | None = None,
    fetcher=None,
    cache_path: Path | None = None,
    now: float | None = None,
    ttl: float = CHECK_TTL_SECONDS,
    no_check: bool = False,
    out=None,
) -> None:
    """Print a pip-style "newer version available" line on stderr, at most once
    per `ttl`. Network is hit only on a cold/expired cache; any failure (offline,
    no gh, bad JSON) is swallowed so reads never stall or error."""
    import sys

    if command not in NAG_COMMANDS:
        return
    if no_check or os.environ.get("TICK_NO_UPDATE_CHECK") == "1":
        return

    out = out if out is not None else sys.stderr
    path = cache_path if cache_path is not None else default_cache_path()
    now = now if now is not None else time.time()

    cache = _read_cache(path)
    if cache and (now - cache.get("checked_at", 0)) < ttl:
        latest = cache.get("latest_version")
    else:
        try:
            latest = check_update(manifest, fetcher).latest_version
        except Exception:
            return  # offline / unreachable / malformed — stay quiet
        _write_cache(path, latest, now)

    if latest and parse_version(latest) > parse_version(__version__):
        print(f"A newer tick is available: {__version__} -> {latest}. Run: tick update", file=out)
