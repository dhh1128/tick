"""tick.core — pure logic, no I/O.

Everything in this module is a pure function or a plain dataclass, so it is fully
unit-testable with no git, no filesystem, and no network. The side-effecting layer
lives in tick.store.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

# RFC-4648 base32, lowercased: a-z then 2-7 (32 symbols). The "digits" are 2-7.
ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
ID_DIGITS = "234567"
ID_LEN = 4

# A tick mark in source code: the sigil + one digit + three base32 chars, e.g. ~2k3m.
# Constraining the first char to a digit is what makes the mark uniquely greppable:
# an identifier (or numeric literal) can never start with a digit, so `~2k3m` can
# never be a unary-operator application to anything — unlike `~data`, `!user`, ...
# The sigil is `~` (not `!`): `!` triggers shell history expansion, so a copy-pasted
# `tick show !2k3m` dies in interactive bash before tick ever runs; `~2k3m` survives
# because tilde expansion only fires for a real login name, which a digit-first id
# never is. `~` also never collides with a comment leader (unlike `//`/`#`).
# The trailing `\b` makes a mark a whole-word token: without it `~25min` matches
# `~25mi` (every id char is a word char) and gets flagged as an orphaned tick. `\b`
# (not a lookahead) so the pattern is byte-identical to the `rg` string agents run —
# ripgrep's default engine supports `\b` but has no lookaround.
MARK_SIGIL = "~"
MARK_RE = re.compile(rf"{re.escape(MARK_SIGIL)}[2-7][a-z2-7]{{3}}\b")
_ID_RE = re.compile(r"^[2-7][a-z2-7]{3}$")

VALID_KINDS = ("todo", "debt", "idea")
DEFAULT_KIND = "todo"

_HEADER_RE = re.compile(r"^([A-Za-z][\w-]*):\s?(.*)$")


# --------------------------------------------------------------------------- ids


def is_valid_id(s: str) -> bool:
    return bool(_ID_RE.match(s))


def generate_id(existing=(), rng=None) -> str:
    """Mint a fresh id not present in `existing`. `rng` needs a `.choice(seq)`."""
    rng = rng or random.SystemRandom()
    existing = set(existing)
    while True:
        first = rng.choice(ID_DIGITS)
        rest = "".join(rng.choice(ID_ALPHABET) for _ in range(ID_LEN - 1))
        candidate = first + rest
        if candidate not in existing:
            return candidate


def extract_marks(text: str) -> list[str]:
    """Return the ids (without the leading sigil) of every tick mark in `text`."""
    return [m.group(0)[len(MARK_SIGIL):] for m in MARK_RE.finditer(text)]


# -------------------------------------------------------------------- timestamps


def format_ts(dt: datetime) -> str:
    """ISO-8601, UTC, minute precision: YYYY-MM-DDThh:mmZ."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# -------------------------------------------------------------------- tick model


@dataclass
class Tick:
    id: str
    title: str
    kind: str = DEFAULT_KIND
    tags: list[str] = field(default_factory=list)
    created: str = ""
    closed: str | None = None
    body: str = ""

    @property
    def is_open(self) -> bool:
        return self.closed is None


def parse_tick(id: str, text: str) -> Tick:
    lines = text.splitlines()
    title = ""
    idx = 0
    if lines and lines[0].startswith("#"):
        title = lines[0].lstrip("#").strip()
        idx = 1
    headers: dict[str, str] = {}
    while idx < len(lines):
        line = lines[idx]
        if line.strip() == "":
            idx += 1
            break
        m = _HEADER_RE.match(line)
        if not m:
            break
        headers[m.group(1).lower()] = m.group(2).strip()
        idx += 1
    body = "\n".join(lines[idx:]).strip("\n")
    tags = [t.strip() for t in headers.get("tags", "").split(",") if t.strip()]
    return Tick(
        id=id,
        title=title,
        kind=headers.get("kind", DEFAULT_KIND),
        tags=tags,
        created=headers.get("created", ""),
        closed=headers.get("closed") or None,
        body=body,
    )


def serialize_tick(tick: Tick) -> str:
    out = [f"# {tick.title}", f"kind: {tick.kind}"]
    if tick.tags:
        out.append(f"tags: {', '.join(tick.tags)}")
    if tick.created:
        out.append(f"created: {tick.created}")
    if tick.closed:
        out.append(f"closed: {tick.closed}")
    out.append("")  # blank line separating header from body
    body = tick.body.strip("\n")
    if body:
        out.append(body)
    return "\n".join(out) + "\n"


def append_note(body: str, ts: str, text: str) -> str:
    bullet = f"- {ts} {text}"
    body = body.rstrip("\n")
    return f"{body}\n{bullet}" if body else bullet


# ------------------------------------------------------------------- queries


def filter_ticks(
    ticks,
    *,
    include_closed: bool = False,
    only_closed: bool = False,
    kind: str | None = None,
    tag: str | None = None,
):
    out = []
    for t in ticks:
        if only_closed and t.is_open:
            continue
        if not only_closed and not include_closed and not t.is_open:
            continue
        if kind and t.kind != kind:
            continue
        if tag and tag not in t.tags:
            continue
        out.append(t)
    return out


def grep_ticks(ticks, query: str):
    q = query.lower()
    return [t for t in ticks if q in t.title.lower() or q in t.body.lower()]


def compute_orphans(marks_in_code, ids_present, open_ids):
    """Return (marks_without_tick, open_ticks_without_mark)."""
    marks_in_code = set(marks_in_code)
    ids_present = set(ids_present)
    open_ids = set(open_ids)
    return marks_in_code - ids_present, open_ids - marks_in_code
