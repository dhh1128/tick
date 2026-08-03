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
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from tick import __version__
from tick import core

BRANCH = "tick"
LOCK_NAME = ".tick.lock"

# The ledger worktree and its lock are ignored via the repo-local, UNTRACKED
# `.git/info/exclude` (see `_ensure_code_exclude`) rather than a tracked `.gitignore`
# line, so `tick init` makes no commit on the host's code branch.
IGNORE_ENTRIES = ("/.tick", LOCK_NAME)

# First tick release that ignores the ledger via `.git/info/exclude` instead of a
# committed `.gitignore` line. On the first run of a tick at or past this version in
# a repo still using the old mechanism, tick offers a one-time `migrate-ignore` nudge.
MIGRATION_VERSION = "1.2.1"

# How long a backlog may sit unpushed before it counts as a real failure rather
# than a push still in flight. Auto-push is detached and a mutation returns in
# ~0.1s, but the push itself takes seconds over the network — so the tracking ref
# legitimately lags every write. Anything younger than this is assumed in flight.
GRACE_SECONDS = 30

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
# The blank lines AROUND the HTML comment markers are load-bearing: without them
# Prettier's markdown formatter flags the stanza (and a list item hugging the closing
# comment gets its continuation de-indented), so `prettier --check` in the host repo's
# lint-staged/CI would reject AGENTS.md. Keep them — the stanza is prettier-clean as-is.
_TICK_STANZA = f"""\
{STANZA_BEGIN}

## Task tracking: `tick`

This repo tracks tasks, tech debt, and ideas in a local [`tick`](https://github.com/dhh1128/tick)
ledger (an orphan `tick` branch; the `tick` CLI is the interface). Reads are plain
files — do **not** use an external API for task tracking.

- **First, if a `tick` command says the repo isn't initialized**, run `tick init`
  once to connect this clone to the ledger — it adopts the existing remote ledger
  if a colleague already set one up, or creates a new one otherwise.
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


def _commit(cwd, message) -> str:
    """Make one of tick's own managed commits. `--no-verify` so the host repo's
    commit hooks never gate tick's bookkeeping: tick commits run in worktrees that
    share the repo's `.git/hooks`, so a husky/lint-staged `prettier --check`,
    `eslint`, `commitlint`, gitleaks scan, or pre-commit test suite would otherwise
    reject tick's AGENTS.md stanza (`--agents`), a `migrate-ignore` `.gitignore`
    edit, and every ledger commit.
    Same rationale as the pre-push guard that skips the code repo's test tax on
    tick-branch pushes (SPEC §3.5). `-s` keeps the DCO sign-off."""
    return git(["commit", "--no-verify", "-s", "-m", message], cwd)


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


def _warn(msg: str) -> None:
    """Surface a recovery/self-heal notice on stderr (stdout stays clean for
    machine-readable command output)."""
    print(f"tick: {msg}", file=sys.stderr)


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


def _local_has_branch(branch: str, cwd) -> bool:
    """True if a local branch ref exists (used to decide whether a vanished store
    can be re-checked-out from a surviving local branch before reaching for the
    remote)."""
    return bool(git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd, check=False))


def _heal_worktree(primary_root: Path, worktree: Path, branch: str, remote: str | None) -> None:
    """Detect and recover a ledger store whose worktree has gone missing — the
    `.tick/` directory deleted, or its branch removed underneath it (e.g. someone
    ran `rm -rf .tick`, or `git worktree remove` + `git branch -D tick`). Without
    this, a stale `tick.worktree` config leaves `tick ls` silently reporting an
    empty ledger and `tick add` crashing on the missing lock file — both look like
    data loss even though the commits survive on the local branch and/or the remote.

    Cheap on the hot path: a present worktree returns after one `_repair_worktree_link`
    check (the moved-repo case). Recovery only runs when the directory is gone, in
    most-local-first order so it stays offline when it can:

      1. directory present                              -> repair a moved-repo link
      2. directory gone, local `branch` still exists    -> prune + re-check-out from it
      3. directory gone, branch gone, remote has it      -> re-fetch + re-adopt
      4. nothing left anywhere                            -> raise (don't fake an empty ledger)
    """
    if worktree.exists():
        _repair_worktree_link(primary_root, worktree)
        return
    # The configured store path is gone. Clear the now-dangling worktree registration
    # so `git worktree add` won't refuse the path as still-registered.
    git(["worktree", "prune"], primary_root, check=False)
    if _local_has_branch(branch, primary_root):
        _warn(f"ledger worktree at {worktree} was missing — re-checking out the local `{branch}` branch")
        git(["worktree", "add", str(worktree), branch], primary_root)
        return
    if remote and _remote_has_branch(remote, branch, primary_root):
        _warn(f"ledger worktree at {worktree} was missing — re-fetching the `{branch}` ledger from `{remote}`")
        git(["fetch", remote, f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}"], primary_root)
        git(["worktree", "add", "--track", "-b", branch, str(worktree), f"{remote}/{branch}"], primary_root)
        return
    raise TickError(
        f"the tick ledger worktree at {worktree} is gone and cannot be recovered automatically: "
        f"no local `{branch}` branch, and "
        + (f"`{remote}` has no `{branch}` branch either" if remote else "no remote is configured")
        + f". If the branch still exists in your reflog (`git reflog show {branch}`), recreate it and run "
        f"`git worktree add {worktree} {branch}`; otherwise the ledger data has been lost."
    )


def resolve(cwd=".") -> Store:
    code_root = git(["rev-parse", "--show-toplevel"], cwd, check=False)
    if not code_root:
        raise TickError("not inside a git repository")
    raw = _config_get("tick.worktree", cwd)
    if not raw:
        # No local config — this clone isn't wired to the ledger. Distinguish "a
        # colleague already inited it (adopt)" from "nobody has (create)". A normal
        # clone of a repo with a `tick` branch already has refs/remotes/*/tick, so
        # this is a zero-network local check on the error path; `tick init` does the
        # authoritative remote `ls-remote` when it actually adopts.
        seen = git(["for-each-ref", "--format=%(refname)", "refs/remotes/*/tick"], cwd, check=False)
        if seen.strip():
            raise TickError(
                "this repo already has a tick ledger on the remote — "
                "run `tick init` to connect this clone to it"
            )
        raise TickError("tick is not initialized in this repo — run `tick init`")
    common = Path(git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd))
    primary_root = _primary_root(common)
    worktree, migrate = _resolve_store_path(raw, primary_root)
    if migrate:
        _config_set("tick.worktree", _rel_or_abs(worktree, primary_root), cwd)
    remote = _config_get("tick.remote", cwd)
    branch = _config_get("tick.branch", cwd, BRANCH)
    _heal_worktree(primary_root, worktree, branch, remote)
    return Store(
        code_root=Path(code_root),
        git_common_dir=common,
        worktree=worktree,
        remote=remote,
        branch=branch,
        autopush=_config_bool("tick.autopush", cwd, True),
    )


def init(cwd=".", store_path=None, remote=None, install_guard=False,
         inject_agents=False, force_host=False) -> Store:
    code_root = git(["rev-parse", "--show-toplevel"], cwd, check=False)
    if not code_root:
        raise TickError("not inside a git repository")
    code_root = Path(code_root)

    common = Path(git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd))
    primary_root = _primary_root(common)

    if remote:
        _validate_remote(remote, cwd)  # reject URLs / unknown names up front, on fresh init and re-init alike

    if _config_get("tick.worktree", cwd):
        _ensure_code_exclude(common)  # self-heal a half-ignored repo; no-op if already present
        if inject_agents:
            _ensure_agents_stanza(primary_root, force=force_host)  # self-heal a missing stanza; no-op if present
        else:
            _maybe_recommend_agents(primary_root)
        if remote:
            # Honor an explicit --remote on re-init: the original `tick init` may have
            # run before the remote existed (leaving tick.remote unset), and re-running
            # with --remote is the natural way to attach/update it. Without this the
            # argument was silently dropped.
            _config_set("tick.remote", remote, cwd)
        store = resolve(cwd)  # idempotent
        if install_guard:
            install_pre_push_guard(store)  # reachable on re-init too (e.g. flag forgotten first time); idempotent
        return store

    store_path = Path(store_path).resolve() if store_path else (primary_root / ".tick")
    remote = remote or _detect_remote(cwd)

    if remote and _remote_has_branch(remote, BRANCH, cwd):
        # A contributor already initialized this repo and pushed the ledger. Adopt
        # that branch instead of minting a fresh (divergent, unrelated-history)
        # orphan root that would later collide on push. Fetch just the tick branch
        # into its tracking ref, then check it out as a worktree tracking it.
        git(["fetch", remote, f"+refs/heads/{BRANCH}:refs/remotes/{remote}/{BRANCH}"], cwd)
        git(["worktree", "add", "--track", "-b", BRANCH, str(store_path), f"{remote}/{BRANCH}"], cwd)
    else:
        # Create an orphan branch from an empty root commit (no --orphan needed; robust
        # across git versions), then check it out as a worktree.
        empty_tree = git(["hash-object", "-t", "tree", "--stdin"], cwd, input="")
        root_commit = git(["commit-tree", empty_tree, "-m", "tick: root"], cwd)
        git(["branch", BRANCH, root_commit], cwd)
        git(["worktree", "add", str(store_path), BRANCH], cwd)

        (store_path / ".gitignore").write_text(LOCK_NAME + "\n")
        (store_path / "README.md").write_text(TICK_BRANCH_README)
        git(["add", "-A"], store_path)
        _commit(store_path, "tick: initialize ledger store")

    _config_set("tick.worktree", _rel_or_abs(store_path, primary_root), cwd)
    _config_set("tick.branch", BRANCH, cwd)
    _config_set("tick.autopush", "true", cwd)  # back up the ledger after each mutation; off via `git config tick.autopush false`
    if remote:
        _config_set("tick.remote", remote, cwd)

    _ensure_code_exclude(common)
    if inject_agents:
        _ensure_agents_stanza(primary_root, force=force_host)
    else:
        _maybe_recommend_agents(primary_root)

    store = resolve(cwd)
    if install_guard:
        install_pre_push_guard(store)
    return store


def _remote_names(cwd):
    return git(["remote"], cwd, check=False).split()


def _detect_remote(cwd):
    remotes = _remote_names(cwd)
    if "origin" in remotes:
        return "origin"
    return remotes[0] if remotes else None


def _validate_remote(remote, cwd):
    """`--remote` takes a git remote *name* (e.g. `origin`), not a URL — tick pushes
    with `git push <name> ...`. Reject anything that isn't a configured remote, with
    a hint when the value looks like a URL (the most common mistake)."""
    names = _remote_names(cwd)
    if remote in names:
        return
    looks_like_url = "://" in remote or remote.endswith(".git") or "@" in remote
    hint = (
        " — that looks like a URL, but --remote takes a git remote *name* like `origin`"
        if looks_like_url else ""
    )
    known = ", ".join(names) if names else "none configured"
    raise TickError(
        f"no git remote named '{remote}' in this repo{hint}\n"
        f"  known remotes: {known}\n"
        f"  add one first:  git remote add <name> <url>"
    )


def _remote_has_branch(remote, branch, cwd) -> bool:
    """True if `remote` already publishes `branch` (so init should adopt it rather
    than create a divergent orphan). One cheap network round-trip via ls-remote."""
    return bool(git(["ls-remote", "--heads", remote, branch], cwd, check=False).strip())


def _exclude_file(common: Path) -> Path:
    return common / "info" / "exclude"


def _ensure_code_exclude(common: Path) -> bool:
    """Ignore the ledger worktree via the repo-local, UNTRACKED `.git/info/exclude`
    instead of the tracked `.gitignore`.

    This is what keeps `tick init` from making ANY commit on the host's code branch.
    The custos incident showed the old tracked-`.gitignore` line (and, worse, an
    AGENTS.md stanza) landing on someone else's `main` unbidden and then leaking into
    PRs cut from it. `info/exclude` lives in the git *common* dir, so a single write
    covers every worktree and every branch — the same reach the tracked entry had —
    but it touches no history and no working tree, so there is nothing to commit and
    nothing to pollute. Idempotent: only missing entries are appended."""
    excl = _exclude_file(common)
    excl.parent.mkdir(parents=True, exist_ok=True)
    lines = excl.read_text().splitlines() if excl.exists() else []
    already = {ln.strip() for ln in lines}
    have_tick = "/.tick" in already or ".tick" in already
    have_lock = LOCK_NAME in already
    missing = []
    if not have_tick:
        missing.append("/.tick")
    if not have_lock:
        missing.append(LOCK_NAME)
    if not missing:
        return False
    with open(excl, "a") as f:
        if lines and lines[-1].strip() != "":
            f.write("\n")
        for e in missing:
            f.write(e + "\n")
    return True


def _assert_host_mutable(primary_root, force, action) -> None:
    """Guard every host-repo *commit* behind a clean, on-a-branch check.

    `tick init` used to commit to whatever branch happened to be checked out, no
    questions asked — so a coding agent that ran it inside someone else's repo
    silently landed tick's bookkeeping on `main`, on top of (and entangled with)
    unrelated in-flight work, and branches cut from that `main` inherited the
    pollution. This gate makes any host-branch commit conditional on the primary
    worktree being on a branch (not detached) with `git status --porcelain` clean;
    `force` (via `--force-host`) overrides it for callers who really mean to.

    Writing `.git/info/exclude` is deliberately NOT gated: it makes no commit and
    touches no tracked file, so it can never pollute history or entangle live work."""
    if force:
        return
    head = git(["symbolic-ref", "--quiet", "HEAD"], primary_root, check=False)
    if not head:
        raise TickError(
            f"refusing to {action}: the repo's primary worktree has a detached HEAD, "
            f"not a branch. Check out a branch first, or pass --force-host to override."
        )
    dirty = git(["status", "--porcelain"], primary_root, check=False)
    if dirty:
        shown = "\n".join("    " + ln for ln in dirty.splitlines()[:10])
        raise TickError(
            f"refusing to {action}: the repo's primary worktree has uncommitted "
            f"changes. tick won't commit onto an unclean branch — commit or stash "
            f"first, or pass --force-host to override.\n  outstanding:\n{shown}"
        )


def _ensure_agents_stanza(primary_root, force=False) -> bool:
    """Append the tick stanza to the primary worktree's AGENTS.md (one commit).

    Teaches coding agents to drive the local ledger instead of an external tracker.
    Opt-in only (`tick init --agents`): injecting tooling docs into a repo you may
    not own — and committing them — is exactly the surprise the custos incident was.
    Idempotent: skipped if the stanza marker is already present, so a re-init never
    duplicates it (and never touches the guarded path). Creates AGENTS.md when absent
    and preserves any existing content (the stanza is appended). The commit goes
    through `_assert_host_mutable`, so it refuses an unclean/detached primary worktree
    unless `force`. Targets the primary worktree — that's the checkout users read."""
    agents = primary_root / "AGENTS.md"
    existing = agents.read_text() if agents.exists() else ""
    if STANZA_BEGIN in existing:
        return False
    _assert_host_mutable(primary_root, force, "add the tick stanza to AGENTS.md")
    sep = "" if existing == "" or existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    agents.write_text(existing + sep + _TICK_STANZA)
    git(["add", "AGENTS.md"], primary_root)
    _commit(primary_root, "docs: add tick stanza to AGENTS.md")
    return True


def _maybe_recommend_agents(primary_root) -> None:
    """When the user didn't pass `--agents` but the repo already keeps an AGENTS.md,
    nudge (on stderr) toward `tick init --agents`. A repo that already maintains an
    AGENTS.md is exactly one where the stanza — which teaches coding agents to drive
    the ledger instead of an external tracker — pays off. Silent when there is no
    AGENTS.md or the stanza is already present."""
    agents = primary_root / "AGENTS.md"
    if not agents.exists() or STANZA_BEGIN in agents.read_text():
        return
    _warn(
        "this repo has an AGENTS.md but tick left it untouched. Re-run "
        "`tick init --agents` to add the stanza that teaches coding agents to drive "
        "the ledger (it commits one docs change to the current branch)."
    )


def _old_gitignore_present(primary_root) -> bool:
    """True if this repo still ignores the ledger the pre-1.2 way — a `/.tick` (or
    bare `.tick`) line committed into the tracked `.gitignore` by an old `tick init`."""
    gi = primary_root / ".gitignore"
    if not gi.exists():
        return False
    entries = {ln.strip() for ln in gi.read_text().splitlines()}
    return "/.tick" in entries or ".tick" in entries


def migrate_ignore(cwd=".", force_host=False) -> bool:
    """Move the ledger ignore from the tracked `.gitignore` (pre-1.2) to the untracked
    `.git/info/exclude`, dropping the committed `/.tick`/`.tick`/lock lines so the host
    branch stops carrying tick bookkeeping. Returns True if it changed anything (False
    when there was nothing to migrate). The `.gitignore` edit is a host-repo commit, so
    it goes through the clean-tree guard — override with `force_host`."""
    common = Path(git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd))
    primary_root = _primary_root(common)
    _ensure_code_exclude(common)  # put the new mechanism in place first (no commit)
    if not _old_gitignore_present(primary_root):
        _config_set("tick.ignoreMigrationNotified", __version__, cwd)  # nothing to nag about
        return False
    _assert_host_mutable(primary_root, force_host, "remove the /.tick line from .gitignore")
    gi = primary_root / ".gitignore"
    kept = [ln for ln in gi.read_text().splitlines()
            if ln.strip() not in ("/.tick", ".tick", LOCK_NAME)]
    gi.write_text("".join(ln + "\n" for ln in kept) if kept else "")
    git(["add", ".gitignore"], primary_root)
    _commit(primary_root, "chore: move tick ledger ignore to .git/info/exclude")
    _config_set("tick.ignoreMigrationNotified", __version__, cwd)
    return True


def _version_tuple(v):
    parts = []
    for p in v.split("+")[0].split("-")[0].split("."):
        num = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts)


def maybe_notify_ignore_migration(cwd=".") -> None:
    """One-time nudge, on the first run of a tick >= MIGRATION_VERSION in a repo still
    ignoring the ledger the pre-1.2 way, toward `tick migrate-ignore`. Best-effort and
    silent unless there's something to say; records a marker so it never nags twice."""
    try:
        if _version_tuple(__version__) < _version_tuple(MIGRATION_VERSION):
            return
        if not _config_get("tick.worktree", cwd):
            return  # not initialized in this repo
        if _config_get("tick.ignoreMigrationNotified", cwd):
            return  # already offered once
        common = Path(git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd))
        primary_root = _primary_root(common)
        if not _old_gitignore_present(primary_root):
            return
        _config_set("tick.ignoreMigrationNotified", __version__, cwd)
        _warn(
            "this repo ignores the tick ledger via a committed `.gitignore` line (the "
            "pre-1.2 mechanism). tick now uses the untracked `.git/info/exclude`, so it "
            "makes no commit on your code branch. Run `tick migrate-ignore` to switch "
            "over (removes the /.tick line from .gitignore in one commit)."
        )
    except Exception:
        pass


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


@dataclass(frozen=True)
class BackupStatus:
    """What tick actually knows about the ledger's off-machine backup.

    Deliberately reports observations, not a diagnosis: the gauge is network-free,
    so it can see that commits aren't on the remote-tracking ref but never why.
    `state` is one of:

      ok            everything local is on the remote
      pending       a backlog younger than the grace window — a push is in flight
      stale         a backlog that outlived the window — a push really did fail
      never         a configured remote that has never received this ledger
      unconfigured  the repo has remotes but `tick.remote` is unset, so auto-push
                    is a silent no-op and nothing is backed up anywhere
      local-only    the repo has no remotes at all — nowhere to push, by design
    """

    state: str
    count: int
    age_seconds: int | None
    remote: str | None

    @property
    def should_warn(self) -> bool:
        return self.state in ("stale", "never", "unconfigured")


def _tracking_ref(store: Store) -> str:
    return f"refs/remotes/{store.remote}/{store.branch}"


def _has_tracking_ref(store: Store) -> bool:
    return bool(
        git(["rev-parse", "--verify", "--quiet", _tracking_ref(store)], store.worktree, check=False)
    )


def _unpushed_range(store: Store, tracked: bool) -> str:
    """The rev range holding ledger commits the remote isn't known to have. With
    no remote-tracking ref (the branch has never been pushed) that's the whole
    branch — the case a bare `A..HEAD` can't express and used to silently drop.

    `tracked` is passed in rather than probed here so one caller can resolve the
    range ONCE. Re-probing mid-call is a live race: auto-push is detached and can
    land between two calls, so the count and the age would describe different
    ranges (observed as a real backlog reported with an unknown age)."""
    return f"{_tracking_ref(store)}..HEAD" if tracked else "HEAD"


def _count_range(store: Store, rng: str) -> int:
    out = git(["rev-list", "--count", rng], store.worktree, check=False)
    try:
        return int(out)
    except ValueError:
        return 0


def unpushed_count(store: Store) -> int:
    """Ledger commits not known to be on the remote — a cheap, network-free backlog
    gauge (git advances refs/remotes/<remote>/<branch> on a successful push). A
    never-pushed branch counts every commit: zero backup is the loudest case, not
    the quietest. Returns 0 only when no remote is configured."""
    if not store.remote:
        return 0
    return _count_range(store, _unpushed_range(store, _has_tracking_ref(store)))


def _oldest_age(store: Store, rng: str, now: float) -> int | None:
    """Seconds since the OLDEST commit in `rng` was committed, or None if the range
    is empty. Anchoring on the oldest (not the newest) is what keeps a fresh
    mutation from resetting the clock and muting a genuine day-old backlog."""
    out = git(["log", "--format=%ct", rng], store.worktree, check=False)
    stamps = [int(s) for s in out.split() if s.isdigit()]
    return max(0, int(now - min(stamps))) if stamps else None


def backup_status(store: Store, clock=time.time, grace: int | None = None) -> BackupStatus:
    """Classify the ledger's backup state without touching the network (SPEC §4)."""
    grace = GRACE_SECONDS if grace is None else grace
    if not store.remote:
        # No push target. Distinguish "this repo is local by design" (nothing to
        # say) from "there IS a remote, tick just isn't wired to it" — the latter
        # means every mutation's auto-push has been a silent no-op.
        if not _remote_names(store.code_root):
            return BackupStatus("local-only", 0, None, None)
        return BackupStatus("unconfigured", _count_range(store, "HEAD"), None, None)
    tracked = _has_tracking_ref(store)
    rng = _unpushed_range(store, tracked)
    count = _count_range(store, rng)
    if count == 0:
        return BackupStatus("ok", 0, None, store.remote)
    age = _oldest_age(store, rng, clock())
    if age is not None and age < grace:
        return BackupStatus("pending", count, age, store.remote)
    return BackupStatus("stale" if tracked else "never", count, age, store.remote)


def format_age(seconds: int | None) -> str:
    """Coarse, human age for a backlog. Precision past the unit is noise here —
    what matters is telling a few-second hiccup from a week of divergence."""
    if seconds is None:
        return "unknown"
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


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
    _commit(store.worktree, msg)


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
        _commit(store.worktree, f"tick edit {id}")
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
