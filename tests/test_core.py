"""Unit tests for tick.core — pure logic, no git / fs / network (SPEC §10)."""

from datetime import datetime, timedelta, timezone

from tick import core


def test_id_format():
    for _ in range(300):
        i = core.generate_id()
        assert len(i) == core.ID_LEN
        assert i[0] in core.ID_DIGITS
        assert all(c in core.ID_ALPHABET for c in i)
        assert core.is_valid_id(i)


def test_is_valid_id():
    assert core.is_valid_id("2k3m")
    assert core.is_valid_id("7qax")
    assert not core.is_valid_id("ak3m")   # first char not a digit
    assert not core.is_valid_id("2k3")    # too short
    assert not core.is_valid_id("2k3mz")  # too long
    assert not core.is_valid_id("2K3M")   # uppercase
    assert not core.is_valid_id("289m")   # 8, 9 not in base32


class SeqRNG:
    """A scripted rng: returns the next char regardless of the alphabet passed."""

    def __init__(self, seq):
        self.seq = list(seq)
        self.i = 0

    def choice(self, alphabet):
        c = self.seq[self.i]
        self.i += 1
        return c


def test_id_generation_retries_on_collision():
    rng = SeqRNG(["2", "a", "a", "a", "3", "b", "b", "b"])
    out = core.generate_id(existing={"2aaa"}, rng=rng)
    assert out == "3bbb"


def test_mark_regex_matches_and_rejects():
    assert core.extract_marks("see !2k3m here") == ["2k3m"]
    assert core.extract_marks("!7qax") == ["7qax"]
    for s in ["!data", "!user", "!name", "!flag", "!=", "!a", "!2k3"]:
        assert core.extract_marks(s) == [], s
    # matches only the first 4 chars of a longer run
    assert core.extract_marks("!2k3mz") == ["2k3m"]


def test_extract_multiple_marks():
    text = "// fix !2k3m and !7qax\n# later !4zzz"
    assert core.extract_marks(text) == ["2k3m", "7qax", "4zzz"]


def test_parse_serialize_roundtrip():
    t = core.Tick(
        id="2k3m",
        title="Fix the parser",
        kind="debt",
        tags=["parser", "perf"],
        created="2026-06-05T14:30Z",
        closed="2026-06-09T09:05Z",
        body="some context\n\n- 2026-06-06T08:12Z a note",
    )
    back = core.parse_tick("2k3m", core.serialize_tick(t))
    assert back == t


def test_parse_minimal_defaults():
    text = "# Just a title\ncreated: 2026-06-05T14:30Z\n\nbody here"
    t = core.parse_tick("2aaa", text)
    assert t.title == "Just a title"
    assert t.kind == "todo"
    assert t.tags == []
    assert t.closed is None
    assert t.is_open
    assert t.body == "body here"


def test_format_ts_utc_minute():
    dt = datetime(2026, 6, 5, 14, 30, 45, tzinfo=timezone(timedelta(hours=2)))
    assert core.format_ts(dt) == "2026-06-05T12:30Z"


def test_append_note():
    assert core.append_note("context", "2026-06-06T08:12Z", "learned X") == (
        "context\n- 2026-06-06T08:12Z learned X"
    )
    assert core.append_note("", "2026-06-06T08:12Z", "first") == (
        "- 2026-06-06T08:12Z first"
    )


def test_filter_ticks():
    a = core.Tick(id="2aaa", title="open todo", kind="todo")
    b = core.Tick(id="2bbb", title="closed", kind="debt", closed="2026-06-01T00:00Z")
    c = core.Tick(id="2ccc", title="open debt", kind="debt", tags=["x"])
    ticks = [a, b, c]
    assert core.filter_ticks(ticks) == [a, c]
    assert core.filter_ticks(ticks, include_closed=True) == [a, b, c]
    assert core.filter_ticks(ticks, only_closed=True) == [b]
    assert core.filter_ticks(ticks, kind="debt") == [c]
    assert core.filter_ticks(ticks, tag="x") == [c]


def test_grep_ticks():
    a = core.Tick(id="2aaa", title="Fix parser", body="the lexer is slow")
    b = core.Tick(id="2bbb", title="UI tweak", body="button color")
    assert core.grep_ticks([a, b], "lexer") == [a]
    assert core.grep_ticks([a, b], "PARSER") == [a]  # case-insensitive
    assert core.grep_ticks([a, b], "color") == [b]


def test_compute_orphans():
    marks = {"2aaa", "2bbb"}    # marks found in code
    present = {"2bbb", "2ccc"}  # tick files that exist
    open_ids = {"2ccc"}         # 2ccc is open; 2bbb is closed
    marks_without_tick, open_without_mark = core.compute_orphans(marks, present, open_ids)
    assert marks_without_tick == {"2aaa"}
    assert open_without_mark == {"2ccc"}
