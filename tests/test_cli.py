"""End-to-end smoke tests driving tick.cli.main() against a temp repo (SPEC §10)."""

import re
import subprocess
from pathlib import Path

import pytest

from tick import cli


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


def test_cli_full_flow(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)

    assert cli.main(["init"]) == 0
    capsys.readouterr()

    assert cli.main(["add", "Fix parser", "--kind", "debt", "--tag", "x"]) == 0
    out = capsys.readouterr().out
    m = re.search(r"!([2-7][a-z2-7]{3})", out)
    assert m, out
    tid = m.group(1)

    assert cli.main(["note", tid, "the lexer is slow"]) == 0
    capsys.readouterr()

    assert cli.main(["ls"]) == 0
    out = capsys.readouterr().out
    assert tid in out and "Fix parser" in out

    assert cli.main(["show", tid]) == 0
    out = capsys.readouterr().out
    assert "Fix parser" in out and "the lexer is slow" in out

    assert cli.main(["grep", "lexer"]) == 0
    assert tid in capsys.readouterr().out

    # close it; ls hides it, --all shows it
    assert cli.main(["off", tid]) == 0
    capsys.readouterr()
    cli.main(["ls"])
    assert tid not in capsys.readouterr().out
    cli.main(["ls", "--all"])
    assert tid in capsys.readouterr().out

    # error path returns 1 (not an exception)
    assert cli.main(["show", "2zzz"]) == 1


def test_cli_refs_and_orphans(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    cli.main(["init"])
    capsys.readouterr()
    cli.main(["add", "real work"])
    tid = re.search(r"!([2-7][a-z2-7]{3})", capsys.readouterr().out).group(1)

    (repo / "file.py").write_text(f"# do it here !{tid}\n# stale !2zzz\n")

    cli.main(["refs", tid])
    assert "file.py" in capsys.readouterr().out

    cli.main(["orphans"])
    out = capsys.readouterr().out
    assert "2zzz" in out  # mark with no tick


def test_cli_mark(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    cli.main(["init"])
    capsys.readouterr()
    cli.main(["add", "speed up"])
    tid = re.search(r"!([2-7][a-z2-7]{3})", capsys.readouterr().out).group(1)
    (repo / "x.py").write_text("y = 2\n")

    assert cli.main(["mark", tid, "x.py:1"]) == 0
    assert f"# !{tid}" in (repo / "x.py").read_text()
    # accepts the !-prefixed id too, and reports the no-op on a repeat
    assert cli.main(["mark", f"!{tid}", "x.py:1"]) == 0
    assert "already" in capsys.readouterr().out

    # malformed FILE:LINE -> usage error, exit 1
    assert cli.main(["mark", tid, "x.py"]) == 1
    assert "FILE:LINE" in capsys.readouterr().err


def test_cli_uninitialized_errors(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    assert cli.main(["ls"]) == 1
    assert "not initialized" in capsys.readouterr().err
