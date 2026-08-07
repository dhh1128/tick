"""Integration tests for tick.store against a temporary real git repo (SPEC §10)."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tick import core
from tick import store as S

SRC = str(Path(__file__).resolve().parent.parent / "src")


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), text=True, capture_output=True, check=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _git(["init", "-b", "main"], root)
    _git(["config", "user.email", "tester@example.com"], root)
    _git(["config", "user.name", "Tester"], root)
    (root / "file.py").write_text("print('hi')\n")
    _git(["add", "-A"], root)
    _git(["commit", "-m", "init"], root)
    return root


def _count(repo, ref="HEAD"):
    return int(_git(["rev-list", "--count", ref], repo).stdout.strip())


def _exclude(repo):
    """The repo-local untracked ignore file tick now writes to (git common dir)."""
    p = repo / ".git" / "info" / "exclude"
    return p.read_text() if p.exists() else ""


def test_init_creates_branch_worktree_config(repo):
    st = S.init(cwd=repo)
    assert st.worktree == repo / ".tick"
    assert st.worktree.is_dir()
    assert "tick" in _git(["branch", "--list", "tick"], repo).stdout
    # Stored relative to the repo root (not absolute) so a move/rename can't strand it.
    assert _git(["config", "--get", "tick.worktree"], repo).stdout.strip() == ".tick"
    # Ignored via the untracked .git/info/exclude, NOT a committed .gitignore line.
    assert "/.tick" in _exclude(repo) and S.LOCK_NAME in _exclude(repo)
    assert not (repo / ".gitignore").exists()
    # idempotent
    assert S.init(cwd=repo).worktree == st.worktree


def test_store_relocatable_after_repo_move(repo, tmp_path):
    """Renaming/moving the repo dir must not strand the ledger: config is stored
    relative and the linked-worktree pointer self-heals on resolve — no manual
    `git worktree repair` or config reset needed."""
    st = S.init(cwd=repo)
    id = S.add(st, "survives a move")

    dest = tmp_path / "proj-renamed"
    shutil.move(str(repo), str(dest))

    # Resolve + read + mutate all work from the new location with zero fixup.
    st2 = S.resolve(cwd=dest)
    assert st2.worktree == dest / ".tick"
    assert S.read_tick(st2, id).title == "survives a move"
    id2 = S.add(st2, "added after the move")
    assert {id, id2} <= S.existing_ids(st2)


def test_legacy_absolute_config_migrates_on_resolve(repo, tmp_path):
    """A ledger initialized before this fix stored an absolute `tick.worktree`. After a
    move, resolve recovers it by basename under the new root and rewrites it relative."""
    st = S.init(cwd=repo)
    id = S.add(st, "legacy")
    # Simulate the pre-fix on-disk state: an absolute config value.
    _git(["config", "tick.worktree", str(repo / ".tick")], repo)

    dest = tmp_path / "proj-moved"
    shutil.move(str(repo), str(dest))

    st2 = S.resolve(cwd=dest)
    assert st2.worktree == dest / ".tick"
    assert S.read_tick(st2, id).title == "legacy"
    # Config has been migrated to a relative value.
    assert _git(["config", "--get", "tick.worktree"], dest).stdout.strip() == ".tick"


def test_plain_init_makes_no_host_branch_commit(repo):
    """The custos-incident fix: a plain `tick init` must not commit anything to the
    host's code branch. Ignoring is done via the untracked .git/info/exclude, and the
    AGENTS.md stanza is opt-in — so the code branch's tip is exactly where it started."""
    before = _count(repo)
    head_before = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    S.init(cwd=repo)
    assert _count(repo) - before == 0
    assert _git(["rev-parse", "HEAD"], repo).stdout.strip() == head_before
    assert not (repo / "AGENTS.md").exists()      # stanza is opt-in
    assert "/.tick" in _exclude(repo)


def test_init_with_agents_injects_stanza_in_one_commit(repo):
    before = _count(repo)
    S.init(cwd=repo, inject_agents=True)
    assert _count(repo) - before == 1            # only the AGENTS.md docs commit
    agents = repo / "AGENTS.md"
    assert agents.exists()
    text = agents.read_text()
    assert S.STANZA_BEGIN in text and S.STANZA_END in text
    assert "Task tracking: `tick`" in text
    # the corrected, whole-word grep pattern is what we tell agents to run
    assert r"~[2-7][a-z2-7]{3}\b" in text


def test_init_agents_stanza_is_idempotent(repo):
    S.init(cwd=repo, inject_agents=True)
    first = (repo / "AGENTS.md").read_text()
    count_after_first = _count(repo)
    S.init(cwd=repo, inject_agents=True)  # idempotent re-init
    assert (repo / "AGENTS.md").read_text() == first  # no duplication
    assert _count(repo) == count_after_first  # no extra commit


def test_init_without_agents_recommends_it_when_agents_md_exists(repo, capsys):
    """If the repo already keeps an AGENTS.md but --agents wasn't passed, nudge toward
    it (on stderr) — without touching the file or committing anything."""
    (repo / "AGENTS.md").write_text("# AGENTS\n\nexisting guidance\n")
    _git(["add", "AGENTS.md"], repo)
    _git(["commit", "-m", "add AGENTS.md"], repo)
    before = _count(repo)
    S.init(cwd=repo)
    assert "tick init --agents" in capsys.readouterr().err
    assert S.STANZA_BEGIN not in (repo / "AGENTS.md").read_text()  # untouched
    assert _count(repo) == before                                  # no commit


def test_injected_stanza_contains_no_literal_mark():
    """The stanza lands in a tracked, mark-scanned file, so it must not contain a
    literal mark — otherwise every tick-initialized repo gets a guaranteed phantom
    orphan, and an agent following the stanza's own `rg` would find a dangling mark.
    The example id is shown without its `~` sigil precisely to avoid this."""
    assert core.extract_marks(S._TICK_STANZA) == []


def test_injected_stanza_is_prettier_clean_by_construction():
    """The stanza is appended to a tracked AGENTS.md that the host repo's
    lint-staged/CI may run `prettier --check` on. Prettier requires a blank line
    on both sides of an HTML comment block, and a list item that hugs the closing
    comment with no blank line gets its continuation de-indented. Asserting the
    blank-line structure (dependency-free, no prettier binary needed) guards against
    a future edit silently reintroducing the commit-blocking non-conformance."""
    lines = S._TICK_STANZA.splitlines()
    begin, end = lines.index(S.STANZA_BEGIN), lines.index(S.STANZA_END)
    assert lines[begin + 1] == "", "need a blank line after the opening stanza marker"
    assert lines[end - 1] == "", "need a blank line before the closing stanza marker"
    # No list item may hug the closing marker (would de-indent under prettier).
    assert not lines[end - 2].startswith(("-", " ")) or lines[end - 1] == ""


def test_orphans_clean_after_init_then_detects_real_mark(repo):
    """A fresh init that injects the stanza into AGENTS.md reports no orphans (the
    example id in the stanza is deliberately sigil-less). A genuine mark the user
    later adds to AGENTS.md is detected like any other."""
    st = S.init(cwd=repo, inject_agents=True)
    marks_without_tick, _ = S.orphans(st)
    assert marks_without_tick == set()  # no phantom from the injected stanza
    with open(repo / "AGENTS.md", "a") as f:
        f.write("\nsee ~7qax for the design notes\n")
    marks_without_tick, _ = S.orphans(st)
    assert marks_without_tick == {"7qax"}


def test_init_preserves_existing_agents_md(repo):
    existing = "# AGENTS\n\nProject-specific guidance the user already wrote.\n"
    (repo / "AGENTS.md").write_text(existing)
    _git(["add", "AGENTS.md"], repo)
    _git(["commit", "-m", "add AGENTS.md"], repo)
    S.init(cwd=repo, inject_agents=True)
    text = (repo / "AGENTS.md").read_text()
    assert existing in text  # original content untouched
    assert S.STANZA_BEGIN in text  # stanza appended


def test_init_from_linked_worktree_ignores_tick_via_shared_exclude(repo, tmp_path):
    """Running `tick init` from a *linked* worktree must ignore `/.tick` via the
    shared `.git/info/exclude` (in the common dir), which covers every worktree and
    branch — where the ledger physically lives is the primary root. The exclude
    mechanism makes no commit on any branch at all, so the feature branch stays
    clean and the primary branch never stages `.tick` as an embedded gitlink."""
    wt = tmp_path / "feature-wt"
    _git(["worktree", "add", "-b", "feature", str(wt)], repo)
    before_main = _git(["rev-parse", "main"], repo).stdout.strip()

    S.init(cwd=wt)

    # The store lands at the primary root regardless of where init ran.
    assert (repo / ".tick").is_dir()
    # ...ignored via the common-dir exclude, and NO commit on either branch.
    assert "/.tick" in _exclude(repo)
    assert not (repo / ".gitignore").exists()
    assert _git(["rev-parse", "main"], repo).stdout.strip() == before_main


def test_reinit_self_heals_missing_exclude(repo):
    """A repo whose `.git/info/exclude` lost the `/.tick` entry (e.g. hand-edited) is
    repaired by re-running `tick init` — the idempotent path re-adds it instead of
    short-circuiting, and still makes no commit."""
    S.init(cwd=repo)
    excl = repo / ".git" / "info" / "exclude"
    excl.write_text(excl.read_text().replace("/.tick\n", ""))  # simulate the damaged state
    assert "/.tick" not in _exclude(repo)

    before = _count(repo)
    S.init(cwd=repo)
    assert "/.tick" in _exclude(repo)
    assert _count(repo) == before  # self-heal is commit-free


# ---------------------------------------------------- host-repo mutation guardrail


def test_agents_commit_refused_on_dirty_working_tree(repo):
    """A host-repo commit (the --agents stanza) is refused when the primary worktree
    has uncommitted work, so tick can't entangle its bookkeeping with in-flight edits."""
    (repo / "wip.txt").write_text("half-done work\n")   # untracked outstanding change
    with pytest.raises(S.TickError, match="uncommitted changes"):
        S.init(cwd=repo, inject_agents=True)
    assert not (repo / "AGENTS.md").exists()            # nothing committed


def test_agents_commit_refused_on_detached_head(repo):
    _git(["checkout", "--detach"], repo)
    with pytest.raises(S.TickError, match="detached HEAD"):
        S.init(cwd=repo, inject_agents=True)


def test_force_host_overrides_dirty_tree_guard(repo):
    (repo / "wip.txt").write_text("half-done work\n")
    S.init(cwd=repo, inject_agents=True, force_host=True)   # override the guard
    assert S.STANZA_BEGIN in (repo / "AGENTS.md").read_text()


def test_ignore_exclude_is_not_gated_by_dirty_tree(repo):
    """The exclude write makes no commit and touches no tracked file, so a dirty tree
    must NOT block a plain init (only host-branch *commits* are guarded)."""
    (repo / "wip.txt").write_text("outstanding\n")
    S.init(cwd=repo)                                    # no --agents => no commit, no guard
    assert "/.tick" in _exclude(repo)


# ------------------------------------------------------- migrate-ignore (pre-1.2)


def _seed_legacy_gitignore(repo):
    """Reproduce a pre-1.2 ledger ignore: `/.tick` + lock committed to .gitignore."""
    (repo / ".gitignore").write_text("/.tick\n" + S.LOCK_NAME + "\n")
    _git(["add", ".gitignore"], repo)
    _git(["commit", "-m", "chore: ignore tick ledger worktree (/.tick)"], repo)


def test_migrate_ignore_moves_gitignore_line_to_exclude(repo):
    S.init(cwd=repo)
    _seed_legacy_gitignore(repo)
    before = _count(repo)

    changed = S.migrate_ignore(cwd=repo)
    assert changed is True
    # entry moved: gone from tracked .gitignore, present in the untracked exclude
    assert "/.tick" not in (repo / ".gitignore").read_text()
    assert "/.tick" in _exclude(repo) and S.LOCK_NAME in _exclude(repo)
    assert _count(repo) - before == 1                  # exactly one .gitignore commit


def test_migrate_ignore_preserves_other_gitignore_lines(repo):
    S.init(cwd=repo)
    (repo / ".gitignore").write_text("node_modules/\n/.tick\n" + S.LOCK_NAME + "\ndist/\n")
    _git(["add", ".gitignore"], repo)
    _git(["commit", "-m", "ignores"], repo)

    S.migrate_ignore(cwd=repo)
    kept = (repo / ".gitignore").read_text()
    assert "node_modules/" in kept and "dist/" in kept
    assert "/.tick" not in kept and S.LOCK_NAME not in kept


def test_migrate_ignore_is_noop_when_already_on_exclude(repo):
    S.init(cwd=repo)                                    # already the new mechanism
    before = _count(repo)
    assert S.migrate_ignore(cwd=repo) is False
    assert _count(repo) == before


def test_migrate_ignore_guarded_by_dirty_tree(repo):
    S.init(cwd=repo)
    _seed_legacy_gitignore(repo)
    (repo / "wip.txt").write_text("outstanding\n")
    with pytest.raises(S.TickError, match="uncommitted changes"):
        S.migrate_ignore(cwd=repo)
    assert "/.tick" in (repo / ".gitignore").read_text()    # untouched


def test_migration_notice_fires_once_then_goes_quiet(repo, capsys):
    S.init(cwd=repo)
    _seed_legacy_gitignore(repo)

    S.maybe_notify_ignore_migration(cwd=repo)
    assert "tick migrate-ignore" in capsys.readouterr().err   # first run: nudged
    S.maybe_notify_ignore_migration(cwd=repo)
    assert capsys.readouterr().err == ""                      # second run: silent


def test_migration_notice_silent_when_already_migrated(repo, capsys):
    S.init(cwd=repo)                                    # fresh init is already on exclude
    S.maybe_notify_ignore_migration(cwd=repo)
    assert capsys.readouterr().err == ""


def test_add_creates_file_and_one_tick_commit(repo):
    st = S.init(cwd=repo)
    before = _count(repo, "tick")
    id = S.add(st, "Fix the parser", kind="debt", tags=["parser"])
    assert core.is_valid_id(id)
    assert (st.worktree / f"{id}.md").exists()
    assert _count(repo, "tick") - before == 1
    t = S.read_tick(st, id)
    assert t.title == "Fix the parser"
    assert t.kind == "debt"
    assert t.tags == ["parser"]
    assert t.created  # timestamp populated


def test_note_off_reopen(repo):
    st = S.init(cwd=repo)
    id = S.add(st, "thing")
    S.note(st, id, "learned X")
    assert "learned X" in S.read_tick(st, id).body
    S.off(st, id)
    assert not S.read_tick(st, id).is_open
    S.reopen(st, id)
    assert S.read_tick(st, id).is_open


def test_edit_commits_changes(repo, tmp_path):
    st = S.init(cwd=repo)
    id = S.add(st, "thing")
    ed = tmp_path / "fakeeditor.py"
    ed.write_text("import sys\nopen(sys.argv[1], 'a').write('\\n- edited via editor\\n')\n")
    before = _count(repo, "tick")
    assert S.edit(st, id, editor=f"python3 {ed}") is True
    assert "edited via editor" in S.read_tick(st, id).body
    assert _count(repo, "tick") - before == 1


def test_refs_and_orphans(repo):
    st = S.init(cwd=repo)
    id = S.add(st, "real work")
    (repo / "file.py").write_text(f"# do the work here ~{id}\n# stale ref ~2zzz\n")
    sites = S.refs(st, id)
    assert any(f"~{id}" in line for _, _, line in sites)
    marks_without_tick, open_without_mark = S.orphans(st)
    assert "2zzz" in marks_without_tick      # mark in code, no tick file
    assert id not in open_without_mark        # open tick that DOES have a mark


def test_mark_injects_trailing_comment_and_is_idempotent(repo):
    st = S.init(cwd=repo)
    id = S.add(st, "speed up")
    (repo / "mod.py").write_text("def f():\n    return 1\n")

    assert S.mark(st, id, "mod.py", 1) is True
    assert (repo / "mod.py").read_text() == f"def f():  # ~{id}\n    return 1\n"
    assert S.mark(st, id, "mod.py", 1) is False                     # already there -> no-op
    assert (repo / "mod.py").read_text().count(f"~{id}") == 1
    assert any(p == "mod.py" for p, _, _ in S.refs(st, id))         # now discoverable


def test_mark_comment_leader_by_extension_and_eof_line(repo):
    st = S.init(cwd=repo)
    id = S.add(st, "x")
    (repo / "a.js").write_text("const x = 1;\n")
    S.mark(st, id, "a.js", 1)
    assert (repo / "a.js").read_text() == f"const x = 1; // ~{id}\n"       # // for JS, single space (prettier-clean)

    (repo / "b.weird").write_text("hello\n")
    S.mark(st, id, "b.weird", 1)
    assert (repo / "b.weird").read_text() == f"hello # ~{id}\n"            # default #, single space

    (repo / "c.py").write_text("x = 1")                                    # no trailing newline
    S.mark(st, id, "c.py", 1)
    assert (repo / "c.py").read_text() == f"x = 1  # ~{id}"                # python keeps TWO spaces (flake8 E261 / black)


def test_mark_spacing_is_prettier_clean_for_js_and_black_clean_for_py(repo):
    """Mark injection must not fight downstream formatters: a single space before
    `//` (what Prettier/gofmt/rustfmt normalize to — two there fails `prettier
    --check`), but two spaces before `#` in Python (what flake8 E261 / black
    require for an inline comment)."""
    st = S.init(cwd=repo)
    id = S.add(st, "x")

    (repo / "app.ts").write_text("foo();\n")
    S.mark(st, id, "app.ts", 1)
    assert (repo / "app.ts").read_text() == f"foo(); // ~{id}\n"           # exactly one space before //

    (repo / "mod.py").write_text("x = 1\n")
    S.mark(st, id, "mod.py", 1)
    assert (repo / "mod.py").read_text() == f"x = 1  # ~{id}\n"            # exactly two spaces before #


def test_mark_errors(repo):
    st = S.init(cwd=repo)
    id = S.add(st, "x")
    (repo / "d.py").write_text("a\nb\n")
    with pytest.raises(S.TickError, match="no such tick"):
        S.mark(st, "2zzz", "d.py", 1)        # nonexistent tick
    with pytest.raises(S.TickError, match="no such file"):
        S.mark(st, id, "missing.py", 1)
    with pytest.raises(S.TickError, match="no line"):
        S.mark(st, id, "d.py", 99)


def test_code_scan_honors_gitignore_and_size_cap(repo):
    """The mark scan skips gitignored paths and oversized files instead of
    reading every file under the root unbounded."""
    st = S.init(cwd=repo)
    id = S.add(st, "real work")

    (repo / "file.py").write_text(f"# do the work here ~{id}\n")          # tracked -> scanned
    with open(repo / ".gitignore", "a") as f:
        f.write("build/\n")
    (repo / "build").mkdir()
    (repo / "build" / "gen.py").write_text(f"# generated, ignored ~{id}\n")  # gitignored -> skipped
    (repo / "big.bin").write_text("x" * (S.MAX_SCAN_BYTES + 1) + f"\n~{id}\n")  # too big -> skipped

    paths = {p for p, _, _ in S.refs(st, id)}
    assert "file.py" in paths
    assert not any(p.startswith("build") for p in paths)
    assert "big.bin" not in paths
    # the only mark the scan should surface is the one in the tracked, in-size file
    assert id in S.all_marks_in_code(st)


def test_grep(repo):
    st = S.init(cwd=repo)
    a = S.add(st, "Fix parser")
    S.note(st, a, "the lexer is slow")
    S.add(st, "UI tweak")
    hits = S.grep(st, "lexer")
    assert [t.id for t in hits] == [a]


def test_concurrent_adds_are_serialized(repo):
    """Two concurrent `add`s must both land with distinct ids (flock prevents the
    git index race that would otherwise fail one of them)."""
    st = S.init(cwd=repo)
    snippet = (
        "from tick import store as S;"
        f"st=S.resolve(cwd=r'{repo}');"
        "print(S.add(st, 'concurrent'))"
    )
    env = {**os.environ, "PYTHONPATH": SRC}
    procs = [
        subprocess.Popen(["python3", "-c", snippet], env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for _ in range(2)
    ]
    results = [p.communicate() for p in procs]
    for (out, err), p in zip(results, procs):
        assert p.returncode == 0, err
    ids = [out.strip() for out, _ in results]
    assert len(set(ids)) == 2, ids               # distinct ids, nothing clobbered
    assert set(ids) <= S.existing_ids(st)         # both files present
    assert all(core.is_valid_id(i) for i in ids)


def _bare_remote(repo, tmp_path):
    bare = tmp_path / "remote.git"
    _git(["init", "--bare", "-b", "main", str(bare)], tmp_path)
    _git(["remote", "add", "origin", str(bare)], repo)
    return bare


def test_init_leaves_a_fresh_ledger_detached(repo, tmp_path):
    """Backup is opt-in. A repo with an `origin` no longer gets its ledger silently
    wired to it — the fresh ledger records the `none` sentinel, so it is local *by
    design* rather than indistinguishable from one somebody forgot to configure."""
    _bare_remote(repo, tmp_path)
    st = S.init(cwd=repo)
    assert st.autopush is True
    assert st.remote is None
    assert st.local_only is True
    assert _git(["config", "--get", "tick.remote"], repo).stdout.strip() == "none"
    assert _git(["config", "--get", "tick.autopush"], repo).stdout.strip() == "true"


def test_init_attaches_a_remote_when_asked_for_one(repo, tmp_path):
    _bare_remote(repo, tmp_path)
    st = S.init(cwd=repo, remote="origin")
    assert st.remote == "origin"
    assert st.local_only is False
    assert _git(["config", "--get", "tick.remote"], repo).stdout.strip() == "origin"


def test_init_accepts_the_none_sentinel_without_validating_it_as_a_remote(repo):
    """`none` is a policy statement, not a remote name, so the remote-name validator
    must not reject it in a repo that has no remotes at all."""
    st = S.init(cwd=repo, remote="none")
    assert st.remote is None
    assert st.local_only is True
    assert _git(["config", "--get", "tick.remote"], repo).stdout.strip() == "none"


def test_reinit_with_none_detaches_a_ledger_that_had_a_remote(repo, tmp_path):
    """The reverse of attaching: `tick init --remote none` on a ledger already
    pushing to `origin` stops the pushing and the warnings, without touching data."""
    _bare_remote(repo, tmp_path)
    S.init(cwd=repo, remote="origin")
    st = S.init(cwd=repo, remote="none")
    assert st.remote is None
    assert st.local_only is True
    assert _git(["config", "--get", "tick.remote"], repo).stdout.strip() == "none"


def test_reinit_with_remote_attaches_it(repo, tmp_path):
    """`tick init` run before the remote existed leaves the ledger detached;
    re-running `tick init --remote origin` once the remote is configured must attach
    it rather than silently drop the argument."""
    st = S.init(cwd=repo)                                 # no remote yet
    assert st.remote is None
    _bare_remote(repo, tmp_path)                          # remote added afterwards
    st2 = S.init(cwd=repo, remote="origin")
    assert st2.remote == "origin"
    assert st2.local_only is False
    assert _git(["config", "--get", "tick.remote"], repo).stdout.strip() == "origin"


def test_init_rejects_remote_url(repo, tmp_path):
    """--remote takes a git remote NAME, not a URL. A URL (the common mistake) is
    rejected with a hint instead of being stored as an unusable remote."""
    _bare_remote(repo, tmp_path)
    with pytest.raises(S.TickError, match="looks like a URL"):
        S.init(cwd=repo, remote="git@github.com:acme/widget.git")


def test_init_rejects_unknown_remote_name(repo, tmp_path):
    with pytest.raises(S.TickError, match="no git remote named 'upstream'"):
        S.init(cwd=repo, remote="upstream")


def test_autopush_backs_up_ledger_branch_to_remote(repo, tmp_path):
    """The fire-and-forget push the verbs make lands the ledger branch on the
    remote. autopush is toggled off for the `add` so the single explicit push we
    await doesn't race the verb's own background push over creating the branch."""
    bare = _bare_remote(repo, tmp_path)
    S.init(cwd=repo, remote="origin")
    _git(["config", "tick.autopush", "false"], repo)
    st = S.resolve(cwd=repo)
    S.add(st, "back me up")                         # local commit only
    _git(["config", "tick.autopush", "true"], repo)
    st = S.resolve(cwd=repo)
    proc = S._autopush(st)                          # the same call a verb makes
    assert proc is not None
    assert proc.wait(timeout=30) == 0
    assert "refs/heads/tick" in _git(["ls-remote", "--heads", str(bare), "tick"], repo).stdout


def test_autopush_is_noop_without_remote_or_when_disabled(repo, tmp_path):
    st = S.init(cwd=repo)              # the base fixture has no remote
    assert st.remote is None
    assert S._autopush(st) is None    # nothing to push to

    _bare_remote(repo, tmp_path)
    S.init(cwd=repo, remote="origin")  # attach it, so only autopush is under test
    _git(["config", "tick.autopush", "false"], repo)
    st2 = S.resolve(cwd=repo)
    assert st2.remote == "origin"
    assert st2.autopush is False
    assert S._autopush(st2) is None   # opted out


def test_autopush_is_noop_for_a_detached_ledger(repo, tmp_path):
    """A repo that *has* a remote but a ledger declared local by design: autopush
    must stay a no-op, not fall back to the repo's remote."""
    _bare_remote(repo, tmp_path)
    st = S.init(cwd=repo, remote="none")
    assert st.autopush is True and st.local_only is True
    S.add(st, "stays here")
    assert S._autopush(S.resolve(cwd=repo)) is None


def test_unpushed_count_tracks_backlog(repo, tmp_path):
    _bare_remote(repo, tmp_path)
    S.init(cwd=repo, remote="origin")
    _git(["config", "tick.autopush", "false"], repo)   # control pushes by hand here
    st = S.resolve(cwd=repo)
    S.add(st, "one")                                   # local commit only
    _git(["config", "tick.autopush", "true"], repo)
    st = S.resolve(cwd=repo)
    assert S._autopush(st).wait(timeout=30) == 0       # single push -> sets the tracking ref
    assert S.unpushed_count(st) == 0

    _git(["config", "tick.autopush", "false"], repo)
    st2 = S.resolve(cwd=repo)
    S.add(st2, "two")                                  # local commit, no push
    assert S.unpushed_count(st2) == 1


def test_unpushed_count_counts_everything_when_never_pushed(repo, tmp_path):
    """A ledger whose branch has never reached the remote has no remote-tracking
    ref to diff against. That used to swallow the failed int() and report 0 — a
    clean bill of health for a ledger with zero backup. Count the whole branch."""
    _bare_remote(repo, tmp_path)
    S.init(cwd=repo, remote="origin")
    _git(["config", "tick.autopush", "false"], repo)
    st = S.resolve(cwd=repo)
    S.add(st, "never leaves this machine")
    assert not _git(["ls-remote", "--heads", "origin", "tick"], repo).stdout.strip()
    assert S.unpushed_count(st) == _count(repo, "refs/heads/tick") > 0


# ------------------------------------------------------- backup status (SPEC §4)


def _backdate(clock_skew):
    """A clock reading `clock_skew` seconds in the future — ages the real commit
    timestamps without sleeping or rewriting history."""
    import time
    return lambda: time.time() + clock_skew


def test_backup_status_ok_when_remote_has_everything(repo, tmp_path):
    _bare_remote(repo, tmp_path)
    S.init(cwd=repo, remote="origin")
    _git(["config", "tick.autopush", "false"], repo)   # drive the push through sync alone
    st = S.resolve(cwd=repo)
    S.add(st, "one")
    S.sync(st)
    status = S.backup_status(st)
    assert status.state == "ok"
    assert status.count == 0


def test_backup_status_is_pending_inside_the_grace_window(repo, tmp_path):
    """The bug that started this: a mutation returns in ~0.1s but its detached
    push takes seconds, so the tracking ref lags. A backlog whose OLDEST commit is
    younger than the grace window is a push in flight, not a failure — stay quiet."""
    _bare_remote(repo, tmp_path)
    S.init(cwd=repo, remote="origin")
    _git(["config", "tick.autopush", "false"], repo)   # stand in for a push still in flight
    st = S.resolve(cwd=repo)
    S.add(st, "just written")
    status = S.backup_status(st)
    assert status.state == "pending"
    assert status.count > 0
    assert status.should_warn is False


def test_backup_status_goes_stale_past_the_grace_window(repo, tmp_path):
    """Once the backlog outlives the window, a push really has failed — say so,
    with the age, so the user can tell a hiccup from a week of silent divergence."""
    _bare_remote(repo, tmp_path)
    S.init(cwd=repo, remote="origin")
    _git(["config", "tick.autopush", "false"], repo)
    st = S.resolve(cwd=repo)
    S.add(st, "already safe")
    S.sync(st)                                        # establishes the tracking ref
    S.add(st, "stranded")
    status = S.backup_status(st, clock=_backdate(4 * 3600))
    assert status.state == "stale"
    assert status.should_warn is True
    assert status.age_seconds >= 4 * 3600
    assert status.remote == "origin"


def test_backup_status_grace_window_uses_the_oldest_commit(repo, tmp_path):
    """Anchor on the OLDEST unpushed commit, not the newest: a fresh mutation on
    top of a real backlog must not reset the clock and mute a genuine warning."""
    _bare_remote(repo, tmp_path)
    S.init(cwd=repo, remote="origin")
    _git(["config", "tick.autopush", "false"], repo)
    st = S.resolve(cwd=repo)
    S.add(st, "already safe")
    S.sync(st)                                        # establishes the tracking ref
    S.add(st, "old and stranded")
    S.add(st, "written just now")
    status = S.backup_status(st, clock=_backdate(4 * 3600))
    assert status.state == "stale"          # newest is "now"; oldest is 4h old
    assert status.count == 2


def test_backup_status_flags_a_ledger_that_has_never_been_pushed(repo, tmp_path):
    _bare_remote(repo, tmp_path)
    S.init(cwd=repo, remote="origin")
    _git(["config", "tick.autopush", "false"], repo)
    st = S.resolve(cwd=repo)
    S.add(st, "no backup anywhere")
    status = S.backup_status(st, clock=_backdate(4 * 3600))
    assert status.state == "never"
    assert status.should_warn is True
    assert status.count == _count(repo, "refs/heads/tick")


def test_backup_status_flags_a_legacy_ledger_with_tick_remote_unset(repo, tmp_path):
    """witness-qualifier in the wild: 22 ticks, 48 ledger commits, an `origin` on
    the repo — but `tick.remote` never set, so autopush was a silent no-op and the
    backlog gauge returned 0. Since backup went opt-in, a *fresh* init writes the
    `none` sentinel, so an unset value can only come from a pre-sentinel ledger:
    genuinely undecided, and still the case where a warning matters most."""
    S.init(cwd=repo)                       # inited before the remote existed
    _git(["config", "--unset", "tick.remote"], repo)   # the pre-sentinel state
    _bare_remote(repo, tmp_path)           # remote added to the repo afterwards
    st = S.resolve(cwd=repo)
    assert st.remote is None
    assert st.local_only is False          # undecided, NOT declared local
    S.add(st, "believed to be backed up")
    status = S.backup_status(st)
    assert status.state == "unconfigured"
    assert status.should_warn is True


def test_backup_status_stays_quiet_for_a_repo_with_no_remotes_at_all(repo):
    """A genuinely local-only repo has nowhere to push. Nagging about it forever
    would be noise, not information."""
    S.init(cwd=repo)
    st = S.resolve(cwd=repo)
    S.add(st, "local by design")
    status = S.backup_status(st)
    assert status.state == "local-only"
    assert status.should_warn is False


def test_backup_status_stays_quiet_for_a_declared_local_ledger(repo, tmp_path):
    """R-7K4M, the point of the sentinel: a repo that HAS a remote, with a ledger
    deliberately kept off it. The old gauge could not tell this from a
    misconfiguration, so it warned on every `tick ls`, forever, with no way out."""
    _bare_remote(repo, tmp_path)
    S.init(cwd=repo)                       # detached by default
    st = S.resolve(cwd=repo)
    assert st.local_only is True
    S.add(st, "local by design")
    status = S.backup_status(st, clock=_backdate(4 * 3600))
    assert status.state == "local-only"
    assert status.should_warn is False
    assert status.count == 0


def test_sync_refuses_a_detached_ledger(repo, tmp_path):
    """`tick sync` on a ledger declared local names the state and the way out of
    it, rather than the generic 'no remote configured' of an undecided ledger."""
    _bare_remote(repo, tmp_path)
    st = S.init(cwd=repo, remote="none")
    with pytest.raises(S.TickError, match="local by design"):
        S.sync(st)


def test_format_age_is_human_and_coarse():
    assert S.format_age(5) == "just now"
    assert S.format_age(90) == "1m ago"
    assert S.format_age(4 * 3600) == "4h ago"
    assert S.format_age(3 * 86400) == "3d ago"


def test_sync_round_trips_through_a_real_remote(repo, tmp_path):
    """`tick sync` pushes the ledger and `pull --rebase`s a second machine's
    commits back, reconciling divergence without conflict (one file per tick).
    Exercises the real ls-remote -> pull --rebase -> push path."""
    bare = _bare_remote(repo, tmp_path)
    S.init(cwd=repo, remote="origin")
    _git(["config", "tick.autopush", "false"], repo)   # drive pushes through sync only
    st = S.resolve(cwd=repo)

    S.add(st, "a")
    S.sync(st)                                          # remote empty -> just pushes the branch

    # A second machine adds a tick straight to the remote's ledger branch.
    other = tmp_path / "other"
    _git(["clone", "--branch", "tick", str(bare), str(other)], tmp_path)
    _git(["config", "user.email", "o@example.com"], other)
    _git(["config", "user.name", "Other"], other)
    (other / "2aaa.md").write_text("# from another machine\nkind: todo\ncreated: 2026-01-01T00:00Z\n")
    _git(["add", "-A"], other)
    _git(["commit", "-m", "other: add 2aaa"], other)
    _git(["push", "origin", "tick"], other)

    bid = S.add(st, "b")                                # local commit while the remote is ahead
    S.sync(st)                                          # pull --rebase reconciles, then pushes

    local = {p.name for p in st.worktree.glob("*.md")}
    assert f"{bid}.md" in local and "2aaa.md" in local  # both ours (rebased) and theirs

    check = tmp_path / "check"
    _git(["clone", "--branch", "tick", str(bare), str(check)], tmp_path)
    remote = {p.name for p in check.glob("*.md")}
    assert {f"{bid}.md", "2aaa.md"} <= remote            # remote received our push too


def test_init_adopts_existing_remote_ledger(repo, tmp_path):
    """A second contributor running `tick init` on a repo whose ledger a colleague
    already pushed must pick up that branch (shared history + their ticks), not mint
    a fresh divergent orphan that would later collide on push.

    This is the one exception to the opt-in default: a bare `tick init` attaches the
    remote here without being asked, because the ledger is *already* shared — a
    detached orphan alongside it is nobody's intent."""
    bare = _bare_remote(repo, tmp_path)
    # First contributor initializes (detached), then pushes the code and the ledger.
    st = S.init(cwd=repo)
    _git(["config", "tick.autopush", "false"], repo)
    st = S.resolve(cwd=repo)
    aid = S.add(st, "first contributor's task")
    _git(["push", "origin", "main"], repo)
    _git(["push", "origin", "tick"], repo)
    remote_tip = _git(["ls-remote", "--heads", str(bare), "tick"], repo).stdout.split()[0]

    # Second contributor: a fresh clone (no local tick.* config, no local tick branch).
    second = tmp_path / "second"
    _git(["clone", str(bare), str(second)], tmp_path)
    _git(["config", "user.email", "two@example.com"], second)
    _git(["config", "user.name", "Two"], second)

    st2 = S.init(cwd=second)
    assert st2.remote == "origin"
    # Adopted the colleague's branch: their tick is present locally...
    assert (st2.worktree / f"{aid}.md").is_file()
    # ...and the local tick branch shares the remote's history (a fast-forward, not
    # an unrelated-history orphan).
    local_tip = _git(["rev-parse", "tick"], second).stdout.strip()
    assert local_tip == remote_tip
    # Tracking is wired so sync's pull/push reconcile cleanly.
    assert _git(["rev-parse", "--abbrev-ref", "tick@{upstream}"], second).stdout.strip() == "origin/tick"


def test_resolve_points_unconnected_clone_at_init(repo, tmp_path):
    """A colleague who clones a tick-using repo but never ran `tick init` has no
    local tick.* config, yet the clone carries refs/remotes/origin/tick. resolve()
    must tell them to *connect* (run init to adopt), distinct from the create-from-
    scratch message when no ledger exists anywhere — and decide this offline, from
    the remote-tracking ref the clone already has (no network call)."""
    bare = _bare_remote(repo, tmp_path)
    S.init(cwd=repo)
    _git(["push", "origin", "main"], repo)
    _git(["push", "origin", "tick"], repo)

    second = tmp_path / "second"
    _git(["clone", str(bare), str(second)], tmp_path)
    # Fresh clone: no local tick.* config, but it carries refs/remotes/origin/tick.
    assert "refs/remotes/origin/tick" in _git(["for-each-ref", "--format=%(refname)"], second).stdout
    with pytest.raises(S.TickError, match="already has a tick ledger on the remote"):
        S.resolve(cwd=second)


def test_resolve_uninitialized_with_no_remote_ledger(repo):
    """No ledger anywhere (no remote tick branch) keeps the plain create-it message,
    so we don't misdirect a genuinely first-time user to 'connect'."""
    with pytest.raises(S.TickError, match="tick is not initialized in this repo"):
        S.resolve(cwd=repo)


def test_heal_recovers_vanished_worktree_from_local_branch(repo):
    """`rm -rf .tick` deletes the store directory but leaves the local `tick` branch
    (and all its commits) intact. resolve() must re-check-out the worktree from that
    branch instead of silently behaving as if the ledger were empty — no remote needed."""
    st = S.init(cwd=repo)
    a = S.add(st, "alpha")
    b = S.add(st, "beta")

    shutil.rmtree(st.worktree)                       # store directory gone; branch survives
    assert not st.worktree.exists()

    st2 = S.resolve(cwd=repo)                         # heals on resolve, offline
    assert st2.worktree.is_dir()
    ids = S.existing_ids(st2)
    assert {a, b} <= ids
    assert S.read_tick(st2, a).title == "alpha"
    # And the store is fully usable again — a mutation succeeds rather than crashing.
    c = S.add(st2, "gamma")
    assert (st2.worktree / f"{c}.md").is_file()


def test_heal_recovers_from_remote_when_local_branch_is_gone(repo, tmp_path):
    """The worktree directory AND the local `tick` branch are both gone (e.g. `git
    worktree remove` + `git branch -D tick`), but the ledger was pushed. resolve()
    must re-fetch the branch from the remote, re-check-out the worktree, and rewire
    upstream tracking so sync keeps working."""
    bare = _bare_remote(repo, tmp_path)
    S.init(cwd=repo, remote="origin")
    st = S.resolve(cwd=repo)
    a = S.add(st, "pushed task")
    S._autopush(st).wait(timeout=30)                 # ensure the ledger is on the remote
    assert "refs/heads/tick" in _git(["ls-remote", "--heads", str(bare), "tick"], repo).stdout

    _git(["worktree", "remove", "--force", str(st.worktree)], repo)
    _git(["branch", "-D", "tick"], repo)
    assert not st.worktree.exists()
    assert "tick" not in _git(["branch", "--list", "tick"], repo).stdout

    st2 = S.resolve(cwd=repo)                         # heals by re-fetching from origin
    assert st2.worktree.is_dir()
    assert (st2.worktree / f"{a}.md").is_file()
    # Upstream tracking is rewired so a later sync reconciles cleanly.
    assert _git(["rev-parse", "--abbrev-ref", "tick@{upstream}"], repo).stdout.strip() == "origin/tick"


def test_heal_raises_clearly_when_ledger_is_unrecoverable(repo):
    """No worktree, no local branch, no remote at all: the data is genuinely gone.
    resolve() must surface that loudly (not report an empty ledger), and the message
    must point at the reflog as the last-resort recovery."""
    st = S.init(cwd=repo)                            # base fixture has no remote
    S.add(st, "doomed")
    _git(["worktree", "remove", "--force", str(st.worktree)], repo)
    _git(["branch", "-D", "tick"], repo)

    with pytest.raises(S.TickError, match="gone and cannot be recovered"):
        S.resolve(cwd=repo)


def test_commits_bypass_host_repo_commit_hooks(repo):
    """The host repo's pre-commit hook (husky/lint-staged running `prettier --check`
    in the wild) must not gate tick's managed commits — neither the opt-in AGENTS.md
    stanza commit nor any later ledger mutation (which commits in a worktree that
    shares the repo's .git/hooks). tick commits with --no-verify."""
    pc = repo / ".git" / "hooks" / "pre-commit"
    pc.write_text("#!/bin/sh\necho 'prettier --check: AGENTS.md' >&2\nexit 1\n")
    pc.chmod(0o755)

    st = S.init(cwd=repo, inject_agents=True)   # dies on the AGENTS.md commit without --no-verify
    assert (repo / "AGENTS.md").read_text().count(S.STANZA_BEGIN) == 1
    assert "/.tick" in _exclude(repo)
    aid = S.add(st, "still works")              # ledger commit also bypasses the hook
    assert (st.worktree / f"{aid}.md").is_file()


def test_sync_without_remote_errors(repo):
    """A pre-sentinel ledger with `tick.remote` unset keeps the generic message —
    it is undecided rather than declared local, and `tick sync` still can't run."""
    S.init(cwd=repo)                                     # base fixture has no remote
    _git(["config", "--unset", "tick.remote"], repo)     # the pre-sentinel state
    st = S.resolve(cwd=repo)
    assert st.local_only is False
    with pytest.raises(S.TickError, match="no remote"):
        S.sync(st)


def test_reinit_with_install_guard_installs_guard(repo):
    """`tick init --install-guard` must install the guard even when the repo is
    already initialized — the early-return idempotency path used to skip it, so a
    user who forgot the flag on first init had no way to add it via re-init."""
    st = S.init(cwd=repo)  # first init, no guard
    guard = st.git_common_dir / "hooks" / "pre-push"
    assert not guard.exists()  # not installed yet
    S.init(cwd=repo, install_guard=True)  # re-init with the flag
    assert guard.exists() and S.GUARD_BEGIN in guard.read_text()


def test_pre_push_guard_chains_real_hook(repo, tmp_path):
    st = S.init(cwd=repo)
    # Pretend the repo already had a heavy pre-push hook.
    hooks = st.git_common_dir / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    sentinel = tmp_path / "real_ran"
    real = hooks / "pre-push"
    real.write_text(f"#!/bin/sh\ntouch {sentinel}\n")
    real.chmod(0o755)

    S.install_pre_push_guard(st)
    guard = hooks / "pre-push"
    assert S.GUARD_BEGIN in guard.read_text()
    assert (hooks / "pre-push.tick-real").exists()

    # Pushing only the tick branch -> guard exits 0, real hook does NOT run.
    subprocess.run([str(guard)], input="refs/heads/tick abc refs/heads/tick def\n", text=True, check=True)
    assert not sentinel.exists()

    # Pushing a code branch -> guard chains to the real hook, which runs.
    subprocess.run([str(guard)], input="refs/heads/main abc refs/heads/main def\n", text=True, check=True)
    assert sentinel.exists()
