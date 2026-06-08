"""tick.store — the side-effecting layer: filesystem, git, and the flock that makes
concurrent multi-worktree mutation safe (SPEC §3, §4).

The store is the orphan `tick` branch checked out as a persistent worktree
(default `<repo-root>/.tick/`). Discovery is config-based (`tick.worktree`) so the
tool works from any worktree. Every mutation runs its read-modify-write-commit
critical section behind an exclusive flock.
"""

from __future__ import annotations

import contextlib
import fcntl  # ~55ez POSIX-only locking; Windows has no fcntl
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from tick import core

BRANCH = "tick"
LOCK_NAME = ".tick.lock"

TICK_BRANCH_README = """\
# tick ledger

This orphan branch holds the [tick](https://github.com/dhh1128/tick) ledger for
this repo: one Markdown file per tick, named `<id>.md`. It is checked out as a
persistent worktree and is unrelated to the code branches' history. Mutate it with
the `tick` CLI, not by editing here directly.
"""

STANZA_BEGIN = "<!-- >>> tick stanza >>> (managed by `tick init`) -->"
STANZA_END = "<!-- <<< tick stanza <<< -->"
# Instructions injected into the target repo's AGENTS.md so coding agents drive the
# local ledger instead of an external tracker. Wrapped in the markers above so a
# re-init is idempotent and a future `tick` can refresh it in place. The grep
# pattern here MUST stay in sync with core.MARK_RE (trailing `\b` = whole-word mark).
# This text is itself scanned for marks (it lands in a tracked file), so it must NOT
# contain a literal mark — the id below is shown WITHOUT the `~` sigil on purpose, so
# neither tick nor an agent's own `rg` mistakes the example for a real pin.
_TICK_STANZA = f"""\
{STANZA_BEGIN}
## Task tracking: `tick`

This repo tracks tasks, tech debt, and ideas in a local [`tick`](https://github.com/dhh1128/tick)
ledger (an orphan `tick` branch; the `tick` CLI is the interface). Reads are plain
files — do **not** use an external API for task tracking.

- **A tick mark is the sigil `~` immediately followed by a digit-first 4-char
  base32 id** (the id part looks like `4mz3`, so the full mark is that id with a
  leading `~`). It pins a tick to a code location.
- **Before editing a file**, grep it for marks and read what they reference:
  `rg '~[2-7][a-z2-7]{{3}}\\b' <file>` then `tick show <id>`. A mark means recorded
  context exists for that spot — read it first.
- **Search** existing ticks with `tick grep <text>`; **list** with `tick ls`.
- **Capture** new work with `tick add "<title>"` and place the printed mark
  (`~` + the new id) at the relevant code spot.
- When your change **resolves** a tick, run `tick off <id>` and **delete the
  mark(s)** it reports still in the code.
{STANZA_END}
"""

GUARD_BEGIN = "# >>> tick pre-push guard >>>"
GUARD_END = "# <<< tick pre-push guard <<<"
_PRE_PUSH_GUARD = f"""\
#!/bin/sh
{GUARD_BEGIN} (managed by `tick init`)
# Skip this repo's pre-push checks when ONLY the tick ledger branch is pushed.
_tmp=$(mktemp)
cat > "$_tmp"
_tick_only=1
while read -r _l _rest; do
  [ -z "$_l" ] && continue
  [ "$_l" = "refs/heads/tick" ] || _tick_only=0
done < "$_tmp"
if [ "$_tick_only" = 1 ]; then rm -f "$_tmp"; exit 0; fi
_real="$(dirname "$0")/pre-push.tick-real"
if [ -x "$_real" ]; then "$_real" "$@" < "$_tmp"; _rc=$?; rm -f "$_tmp"; exit $_rc; fi
rm -f "$_tmp"; exit 0
{GUARD_END}
"""


class TickError(Exception):
    pass


@dataclass
class Store:
    code_root: Path
    git_common_dir: Path
    worktree: Path
    remote: str | None
    branch: str = BRANCH
    autopush: bool = True


# ------------------------------------------------------------------- git helpers


def _run(args, cwd, input=None):
    return subprocess.run(
        [str(a) for a in args],
        cwd=str(cwd),
        text=True,
        capture_output=True,
        input=input,
    )


def git(args, cwd, check=True, input=None) -> str:
    p = _run(["git", *args], cwd, input=input)
    if check and p.returncode != 0:
        raise TickError(f"git {' '.join(map(str, args))} failed: {p.stderr.strip() or p.stdout.strip()}")
    return p.stdout.strip()


def _config_get(key, cwd, default=None):
    p = _run(["git", "config", "--get", key], cwd)
    return p.stdout.strip() if p.returncode == 0 and p.stdout.strip() else default


def _config_bool(key, cwd, default):
    v = _config_get(key, cwd)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _config_set(key, value, cwd):
    git(["config", key, value], cwd)


# ----------------------------------------------------------------- resolve / init


def _primary_root(common: Path) -> Path:
    """The primary worktree's root — where the `.tick/` store physically lives.

    `--git-common-dir` is `<primary>/.git` for the standard layout tick targets,
    so its parent is the primary checkout. Anchoring on this (rather than an
    absolute config value or the cwd worktree's `--show-toplevel`) is what makes
    the store survive a repo move/rename: git recomputes the common dir from the
    live location, so the join always lands on the current path even after a move."""
    return common.parent


def _rel_or_abs(store_path: Path, primary_root: Path) -> str:
    """Record the store relative to the repo root when it lives inside it (so a
    move/rename doesn't strand the path); absolute for an out-of-repo `--store`."""
    try:
        return str(store_path.relative_to(primary_root))
    except ValueError:
        return str(store_path)


def _resolve_store_path(raw: str, primary_root: Path) -> tuple[Path, bool]:
    """Map the stored `tick.worktree` value to a live path. Returns
    (path, migrate) where migrate signals a legacy absolute value that has been
    recovered under the moved repo and should be rewritten relative."""
    p = Path(raw)
    if not p.is_absolute():
        return primary_root / p, False
    if p.exists():
        return p, False  # legacy absolute config, still valid — honor it
    # Legacy absolute path stranded by a repo move: recover by basename under the
    # current primary root and signal the caller to migrate it to a relative value.
    candidate = primary_root / p.name
    if candidate.exists():
        return candidate, True
    return p, False  # nothing better; let the downstream "no such tick" surface


def _repair_worktree_link(primary_root: Path, worktree: Path) -> None:
    """Self-heal a linked-worktree pointer broken by a repo move/rename so git
    operations inside the store work without a manual `git worktree repair`. Cheap
    on the hot path: a valid pointer returns after one small file read."""
    dotgit = worktree / ".git"
    if not dotgit.is_file():
        return  # not a linked worktree (absent, or a plain dir) — nothing to do
    line = dotgit.read_text().strip()
    if line.startswith("gitdir:"):
        target = Path(line.split(":", 1)[1].strip())
        if target.exists():
            return  # linkage intact
    git(["worktree", "repair", str(worktree)], primary_root, check=False)


def resolve(cwd=".") -> Store:
    code_root = git(["rev-parse", "--show-toplevel"], cwd, check=False)
    if not code_root:
        raise TickError("not inside a git repository")
    raw = _config_get("tick.worktree", cwd)
    if not raw:
        raise TickError("tick is not initialized in this repo — run `tick init`")
    common = Path(git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd))
    primary_root = _primary_root(common)
    worktree, migrate = _resolve_store_path(raw, primary_root)
    if migrate:
        _config_set("tick.worktree", _rel_or_abs(worktree, primary_root), cwd)
    _repair_worktree_link(primary_root, worktree)
    return Store(
        code_root=Path(code_root),
        git_common_dir=common,
        worktree=worktree,
        remote=_config_get("tick.remote", cwd),
        branch=_config_get("tick.branch", cwd, BRANCH),
        autopush=_config_bool("tick.autopush", cwd, True),
    )


def init(cwd=".", store_path=None, remote=None, install_guard=False) -> Store:
    code_root = git(["rev-parse", "--show-toplevel"], cwd, check=False)
    if not code_root:
        raise TickError("not inside a git repository")
    code_root = Path(code_root)

    common = Path(git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd))
    primary_root = _primary_root(common)

    if _config_get("tick.worktree", cwd):
        _ensure_code_gitignore(primary_root)  # self-heal a half-ignored repo; no-op if already present
        _ensure_agents_stanza(primary_root)   # self-heal a missing AGENTS.md stanza; no-op if present
        return resolve(cwd)  # idempotent

    store_path = Path(store_path).resolve() if store_path else (primary_root / ".tick")

    # Create an orphan branch from an empty root commit (no --orphan needed; robust
    # across git versions), then check it out as a worktree.
    empty_tree = git(["hash-object", "-t", "tree", "--stdin"], cwd, input="")
    root_commit = git(["commit-tree", empty_tree, "-m", "tick: root"], cwd)
    git(["branch", BRANCH, root_commit], cwd)
    git(["worktree", "add", str(store_path), BRANCH], cwd)

    (store_path / ".gitignore").write_text(LOCK_NAME + "\n")
    (store_path / "README.md").write_text(TICK_BRANCH_README)
    git(["add", "-A"], store_path)
    git(["commit", "-s", "-m", "tick: initialize ledger store"], store_path)

    _config_set("tick.worktree", _rel_or_abs(store_path, primary_root), cwd)
    _config_set("tick.branch", BRANCH, cwd)
    _config_set("tick.autopush", "true", cwd)  # back up the ledger after each mutation; off via `git config tick.autopush false`
    remote = remote or _detect_remote(cwd)
    if remote:
        _config_set("tick.remote", remote, cwd)

    _ensure_code_gitignore(primary_root)
    _ensure_agents_stanza(primary_root)

    store = resolve(cwd)
    if install_guard:
        install_pre_push_guard(store)
    return store


def _detect_remote(cwd):
    out = git(["remote"], cwd, check=False)
    remotes = out.split()
    if "origin" in remotes:
        return "origin"
    return remotes[0] if remotes else None


def _ensure_code_gitignore(primary_root) -> bool:
    """Add `/.tick` + lock to the primary worktree's tracked .gitignore (one commit).

    Always targets the primary worktree — the ledger physically lives at
    `<primary_root>/.tick`, so that is the only checkout whose `.gitignore` needs
    the entry. Writing it to whatever worktree `tick init` happened to run from
    would put it on the wrong branch (and leave the primary branch staging `.tick`
    as an embedded-repo gitlink)."""
    gi = primary_root / ".gitignore"
    lines = gi.read_text().splitlines() if gi.exists() else []
    already = {ln.strip() for ln in lines}
    if "/.tick" in already or ".tick" in already:
        return False
    with open(gi, "a") as f:
        if lines and lines[-1].strip() != "":
            f.write("\n")
        f.write("/.tick\n" + LOCK_NAME + "\n")
    git(["add", ".gitignore"], primary_root)
    git(["commit", "-s", "-m", "chore: ignore tick ledger worktree (/.tick)"], primary_root)
    return True


def _ensure_agents_stanza(primary_root) -> bool:
    """Append the tick stanza to the primary worktree's AGENTS.md (one commit).

    Teaches coding agents to drive the local ledger instead of an external tracker.
    Idempotent: skipped if the stanza marker is already present, so a re-init never
    duplicates it. Creates AGENTS.md when absent and preserves any existing content
    (the stanza is appended). Targets the primary worktree for the same reason as
    `_ensure_code_gitignore` — that's the checkout users actually read."""
    agents = primary_root / "AGENTS.md"
    existing = agents.read_text() if agents.exists() else ""
    if STANZA_BEGIN in existing:
        return False
    sep = "" if existing == "" or existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    agents.write_text(existing + sep + _TICK_STANZA)
    git(["add", "AGENTS.md"], primary_root)
    git(["commit", "-s", "-m", "docs: add tick stanza to AGENTS.md"], primary_root)
    return True


# --------------------------------------------------------------------- locking


@contextlib.contextmanager
def _lock(store: Store):
    lockfile = store.worktree / LOCK_NAME
    lockfile.touch(exist_ok=True)
    fh = open(lockfile, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


# ----------------------------------------------------------------- auto-backup


def _autopush(store: Store) -> subprocess.Popen | None:
    """Best-effort background backup of the ledger branch after a mutation.

    Fire-and-forget: spawn a detached `git push` and return immediately, so the
    write stays instant and offline-capable (SPEC §4). It never blocks and never
    fails the mutation — the local commit is already durable, so a failed/offline
    push simply defers to the next mutation or an explicit `tick sync`. Disabled
    when `tick.autopush` is false or no remote is configured; returns None then.
    Returns the Popen handle (callers ignore it; tests await it)."""
    if not store.autopush or not store.remote:
        return None
    return subprocess.Popen(
        ["git", "push", store.remote, f"HEAD:{store.branch}"],
        cwd=str(store.worktree),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # outlive this CLI process; immune to its signals
    )


def unpushed_count(store: Store) -> int:
    """Ledger commits not yet on the remote-tracking ref — a cheap, network-free
    backlog gauge (git advances refs/remotes/<remote>/<branch> on a successful
    push, so a nonzero count means auto-push has been failing, e.g. offline).
    Returns 0 when there's no remote or no remote-tracking ref yet."""
    if not store.remote:
        return 0
    out = git(
        ["rev-list", "--count", f"refs/remotes/{store.remote}/{store.branch}..HEAD"],
        store.worktree,
        check=False,
    )
    try:
        return int(out)
    except ValueError:
        return 0  # remote-tracking ref absent (never pushed) — don't guess


# ----------------------------------------------------------------------- CRUD


def _tick_path(store: Store, id: str) -> Path:
    return store.worktree / f"{id}.md"


def read_tick(store: Store, id: str) -> core.Tick:
    p = _tick_path(store, id)
    if not p.exists():
        raise TickError(f"no such tick: {id}")
    return core.parse_tick(id, p.read_text())


def existing_ids(store: Store) -> set[str]:
    return {p.stem for p in store.worktree.glob("*.md") if core.is_valid_id(p.stem)}


def list_ticks(store: Store) -> list[core.Tick]:
    ticks = []
    for p in sorted(store.worktree.glob("*.md")):
        if core.is_valid_id(p.stem):
            ticks.append(core.parse_tick(p.stem, p.read_text()))
    return ticks


def _write_commit(store: Store, id: str, text: str, msg: str):
    p = _tick_path(store, id)
    p.write_text(text)
    git(["add", p.name], store.worktree)
    git(["commit", "-s", "-m", msg], store.worktree)


def add(store: Store, title: str, kind=core.DEFAULT_KIND, tags=None, clock=core.utc_now, rng=None) -> str:
    tags = tags or []
    with _lock(store):
        id = core.generate_id(existing_ids(store), rng)
        t = core.Tick(id=id, title=title, kind=kind, tags=list(tags), created=core.format_ts(clock()))
        _write_commit(store, id, core.serialize_tick(t), f"tick add {id}: {title}")
    _autopush(store)
    return id


def note(store: Store, id: str, text: str, clock=core.utc_now):
    with _lock(store):
        t = read_tick(store, id)
        t.body = core.append_note(t.body, core.format_ts(clock()), text)
        _write_commit(store, id, core.serialize_tick(t), f"tick note {id}")
    _autopush(store)


def off(store: Store, id: str, clock=core.utc_now):
    with _lock(store):
        t = read_tick(store, id)
        t.closed = core.format_ts(clock())
        _write_commit(store, id, core.serialize_tick(t), f"tick off {id}: {t.title}")
    _autopush(store)
    return refs(store, id)  # remaining mark sites to warn about


def reopen(store: Store, id: str):
    with _lock(store):
        t = read_tick(store, id)
        t.closed = None
        _write_commit(store, id, core.serialize_tick(t), f"tick reopen {id}: {t.title}")
    _autopush(store)


def edit(store: Store, id: str, editor=None) -> bool:
    p = _tick_path(store, id)
    if not p.exists():
        raise TickError(f"no such tick: {id}")
    editor = editor or os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    before = p.read_text()
    cp = subprocess.run([*shlex.split(editor), str(p)])
    if cp.returncode != 0:
        raise TickError(f"editor exited with status {cp.returncode}")
    if p.read_text() == before:
        return False
    with _lock(store):
        git(["add", p.name], store.worktree)
        git(["commit", "-s", "-m", f"tick edit {id}"], store.worktree)
    _autopush(store)
    return True


# ------------------------------------------------------------------- code scan


MAX_SCAN_BYTES = 1 << 20  # 1 MiB — marks live in source lines; never read big/generated blobs


def _iter_code_files(store: Store):
    """Yield code-worktree files to scan for marks, honoring `.gitignore` and
    skipping oversized blobs.

    `git ls-files --cached --others --exclude-standard` enumerates tracked plus
    untracked-but-not-ignored files, so the same ignore rules that hide build
    output and the `.tick` store keep mark-scanning bounded — no descending into
    `node_modules/`, `dist/`, etc. A per-file size cap (`MAX_SCAN_BYTES`) skips
    large/binary files we'd otherwise read whole (debt: the scan used to read
    every file under the root, unbounded)."""
    listing = git(
        ["ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        store.code_root,
    )
    for rel in listing.split("\0"):
        if not rel:
            continue
        path = store.code_root / rel
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_SCAN_BYTES:
                continue
        except OSError:
            continue
        yield path


def refs(store: Store, id: str):
    """Return [(relpath, lineno, line)] in the code worktree mentioning ~id."""
    needle = core.MARK_SIGIL + id
    out = []
    for path in _iter_code_files(store):
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if needle in line:
                out.append((str(path.relative_to(store.code_root)), n, line.strip()))
    return out


# Comment leaders for injecting marks, keyed by file extension; default "#".
_COMMENT_LEADERS = {
    **dict.fromkeys(
        [".py", ".sh", ".bash", ".zsh", ".rb", ".pl", ".r", ".yaml", ".yml",
         ".toml", ".cfg", ".ini", ".tf", ".dockerfile", ".mk", ".makefile", ".coffee"], "#"),
    **dict.fromkeys(
        [".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".c", ".h", ".cpp", ".hpp",
         ".cc", ".go", ".rs", ".java", ".kt", ".swift", ".scala", ".php", ".cs",
         ".m", ".dart", ".proto"], "//"),
    **dict.fromkeys([".sql", ".hs", ".lua", ".elm", ".adb", ".ads"], "--"),
    **dict.fromkeys([".clj", ".cljs", ".el", ".lisp", ".scm"], ";"),
}


def _comment_leader(path: Path) -> str:
    return _COMMENT_LEADERS.get(path.suffix.lower(), "#")


# Extensions whose linters/formatters require *two* spaces before an inline
# comment (PEP 8 / flake8 E261 / black). Everything else gets a single space —
# what Prettier (JS/TS/CSS), gofmt, and rustfmt normalize to; two spaces there
# makes `prettier --check` fail on every injected mark.
_TWO_SPACE_BEFORE = {".py", ".pyi"}


def _comment_gap(path: Path) -> str:
    return "  " if path.suffix.lower() in _TWO_SPACE_BEFORE else " "


def mark(store: Store, id: str, file: str, line: int) -> bool:
    """Inject the tick mark `~<id>` as a trailing comment on <file>:<line> of the
    code worktree, using a comment leader inferred from the file extension
    (default `#`). Returns True if added, False if the mark was already on that
    line. Edits the working tree only — no commit and no store lock, since the
    mark lives in code that the user stages as part of their normal flow."""
    read_tick(store, id)  # validate the tick exists (raises TickError otherwise)
    path = store.code_root / file
    if not path.is_file():
        raise TickError(f"no such file: {file}")
    try:
        lines = path.read_text().splitlines(keepends=True)
    except (UnicodeDecodeError, OSError) as e:
        raise TickError(f"cannot read {file}: {e}")
    if line < 1 or line > len(lines):
        raise TickError(f"{file} has no line {line} (file has {len(lines)})")
    idx = line - 1
    raw = lines[idx]
    text = raw.rstrip("\r\n")
    ending = raw[len(text):]  # preserve the original line ending ("", "\n", "\r\n")
    needle = core.MARK_SIGIL + id
    if needle in text:
        return False  # idempotent — already marked here
    lines[idx] = f"{text}{_comment_gap(path)}{_comment_leader(path)} {needle}{ending}"
    path.write_text("".join(lines))
    return True


def all_marks_in_code(store: Store) -> set[str]:
    found = set()
    for path in _iter_code_files(store):
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        found.update(core.extract_marks(text))
    return found


def orphans(store: Store):
    ticks = list_ticks(store)
    present = {t.id for t in ticks}
    open_ids = {t.id for t in ticks if t.is_open}
    return core.compute_orphans(all_marks_in_code(store), present, open_ids)


def grep(store: Store, query: str):
    return core.grep_ticks(list_ticks(store), query)


# -------------------------------------------------------------- sync / link / guard


def sync(store: Store):
    if not store.remote:
        raise TickError("no remote configured (set tick.remote or pass --remote to init)")
    remote_heads = git(["ls-remote", "--heads", store.remote, store.branch], store.worktree, check=False)
    if remote_heads.strip():
        git(["pull", "--rebase", store.remote, store.branch], store.worktree)
    git(["push", store.remote, f"HEAD:{store.branch}"], store.worktree)


def link(store: Store, cwd=".") -> bool:
    here = Path(git(["rev-parse", "--show-toplevel"], cwd))
    target = here / ".tick"
    if target.exists() or target.is_symlink():
        return False
    target.symlink_to(store.worktree)
    return True


def install_pre_push_guard(store: Store) -> Path:
    hooks = store.git_common_dir / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    pre_push = hooks / "pre-push"
    if pre_push.exists():
        if GUARD_BEGIN in pre_push.read_text():
            return pre_push  # already installed
        # preserve the existing hook; our wrapper chains to it
        pre_push.rename(hooks / "pre-push.tick-real")
    pre_push.write_text(_PRE_PUSH_GUARD)
    pre_push.chmod(0o755)
    return pre_push
