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


def test_init_creates_branch_worktree_config(repo):
    st = S.init(cwd=repo)
    assert st.worktree == repo / ".tick"
    assert st.worktree.is_dir()
    assert "tick" in _git(["branch", "--list", "tick"], repo).stdout
    # Stored relative to the repo root (not absolute) so a move/rename can't strand it.
    assert _git(["config", "--get", "tick.worktree"], repo).stdout.strip() == ".tick"
    assert "/.tick" in (repo / ".gitignore").read_text()
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


def test_init_adds_gitignore_in_exactly_one_code_commit(repo):
    before = _count(repo)
    S.init(cwd=repo)
    assert _count(repo) - before == 1  # only the /.tick .gitignore commit on the code branch


def test_init_from_linked_worktree_ignores_tick_in_primary(repo, tmp_path):
    """Running `tick init` from a *linked* worktree must still add `/.tick` to the
    PRIMARY worktree's tracked .gitignore — that's where the ledger physically
    lives (anchored on the common dir). Regression: init used to commit the ignore
    onto the worktree it was invoked from, so when run from a feature worktree the
    primary branch (e.g. main) never got `/.tick`, and a later `git add .` on main
    staged `.tick` as an embedded-repo gitlink."""
    wt = tmp_path / "feature-wt"
    _git(["worktree", "add", "-b", "feature", str(wt)], repo)

    S.init(cwd=wt)

    # The store lands at the primary root regardless of where init ran.
    assert (repo / ".tick").is_dir()
    # ...and the ignore lands on the primary worktree, not the feature branch.
    primary_gi = repo / ".gitignore"
    assert primary_gi.exists() and "/.tick" in primary_gi.read_text()


def test_reinit_self_heals_missing_gitignore(repo):
    """A repo left half-initialized by the old linked-worktree bug (ledger present,
    but `/.tick` missing from the primary .gitignore) is repaired by re-running
    `tick init` — the idempotent path self-heals instead of short-circuiting."""
    S.init(cwd=repo)
    gi = repo / ".gitignore"
    gi.write_text(gi.read_text().replace("/.tick\n", ""))   # simulate the damaged state
    _git(["commit", "-am", "drop tick ignore"], repo)
    assert "/.tick" not in gi.read_text()

    S.init(cwd=repo)
    assert "/.tick" in gi.read_text()


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


def test_init_enables_autopush_and_detects_remote(repo, tmp_path):
    _bare_remote(repo, tmp_path)
    st = S.init(cwd=repo)
    assert st.autopush is True
    assert st.remote == "origin"
    assert _git(["config", "--get", "tick.autopush"], repo).stdout.strip() == "true"


def test_autopush_backs_up_ledger_branch_to_remote(repo, tmp_path):
    """The fire-and-forget push the verbs make lands the ledger branch on the
    remote. autopush is toggled off for the `add` so the single explicit push we
    await doesn't race the verb's own background push over creating the branch."""
    bare = _bare_remote(repo, tmp_path)
    S.init(cwd=repo)
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
    _git(["config", "tick.autopush", "false"], repo)
    st2 = S.resolve(cwd=repo)
    assert st2.autopush is False
    assert S._autopush(st2) is None   # opted out


def test_unpushed_count_tracks_backlog(repo, tmp_path):
    _bare_remote(repo, tmp_path)
    S.init(cwd=repo)
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


def test_sync_round_trips_through_a_real_remote(repo, tmp_path):
    """`tick sync` pushes the ledger and `pull --rebase`s a second machine's
    commits back, reconciling divergence without conflict (one file per tick).
    Exercises the real ls-remote -> pull --rebase -> push path."""
    bare = _bare_remote(repo, tmp_path)
    S.init(cwd=repo)
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


def test_sync_without_remote_errors(repo):
    st = S.init(cwd=repo)                                # base fixture has no remote
    with pytest.raises(S.TickError, match="no remote"):
        S.sync(st)


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
