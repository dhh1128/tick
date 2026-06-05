"""Integration tests for tick.store against a temporary real git repo (SPEC §10)."""

import os
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
    assert _git(["config", "--get", "tick.worktree"], repo).stdout.strip() == str(repo / ".tick")
    assert "/.tick" in (repo / ".gitignore").read_text()
    # idempotent
    assert S.init(cwd=repo).worktree == st.worktree


def test_init_adds_gitignore_in_exactly_one_code_commit(repo):
    before = _count(repo)
    S.init(cwd=repo)
    assert _count(repo) - before == 1  # only the /.tick .gitignore commit on the code branch


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
    (repo / "file.py").write_text(f"# do the work here !{id}\n# stale ref !2zzz\n")
    sites = S.refs(st, id)
    assert any(f"!{id}" in line for _, _, line in sites)
    marks_without_tick, open_without_mark = S.orphans(st)
    assert "2zzz" in marks_without_tick      # mark in code, no tick file
    assert id not in open_without_mark        # open tick that DOES have a mark


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
