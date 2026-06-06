"""End-to-end smoke tests driving tick.cli.main() against a temp repo (SPEC §10)."""

import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import tick
from tick import cli

REPO_ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    return data["project"]["version"]


def test_version_is_single_source_of_truth():
    # __version__ must track pyproject.toml — they had drifted (0.1.0 vs 1.0.1).
    assert tick.__version__ == _pyproject_version()


def test_cli_version_flag(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0
    out = capsys.readouterr().out.strip()
    assert out == f"tick {tick.__version__}"
    assert re.match(r"^tick \d+\.\d+\.\d+", out)


def test_built_zipapp_reports_pyproject_version(tmp_path):
    # build.py must bake the real version into the zipapp, which has no package
    # metadata for importlib.metadata to read at runtime.
    out = tmp_path / "tick"
    subprocess.run([sys.executable, "build.py", str(out)], cwd=REPO_ROOT, check=True,
                   capture_output=True, text=True)
    res = subprocess.run([sys.executable, str(out), "--version"], capture_output=True, text=True)
    assert res.returncode == 0
    assert res.stdout.strip() == f"tick {_pyproject_version()}"


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
    m = re.search(r"~([2-7][a-z2-7]{3})", out)
    assert m, out
    tid = m.group(1)

    assert cli.main(["note", tid, "the lexer is slow"]) == 0
    capsys.readouterr()

    assert cli.main(["ls"]) == 0
    out = capsys.readouterr().out
    assert tid in out and "Fix parser" in out
    # listings print the bare id (no sigil) so it copy-pastes into commands
    assert re.search(rf"^{tid}\b", out, re.M), out
    assert f"~{tid}" not in out and f"!{tid}" not in out

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
    tid = re.search(r"~([2-7][a-z2-7]{3})", capsys.readouterr().out).group(1)

    (repo / "file.py").write_text(f"# do it here ~{tid}\n# stale ~2zzz\n")

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
    tid = re.search(r"~([2-7][a-z2-7]{3})", capsys.readouterr().out).group(1)
    (repo / "x.py").write_text("y = 2\n")

    assert cli.main(["mark", tid, "x.py:1"]) == 0
    assert f"# ~{tid}" in (repo / "x.py").read_text()
    # accepts a ~-prefixed id, and reports the no-op on a repeat
    assert cli.main(["mark", f"~{tid}", "x.py:1"]) == 0
    assert "already" in capsys.readouterr().out
    # also tolerates the legacy !-prefixed id
    assert cli.main(["mark", f"!{tid}", "x.py:1"]) == 0
    assert "already" in capsys.readouterr().out

    # malformed FILE:LINE -> usage error, exit 1
    assert cli.main(["mark", tid, "x.py"]) == 1
    assert "FILE:LINE" in capsys.readouterr().err


def test_cli_update_check_reports_status(tmp_path, capsys):
    import json
    manifest = tmp_path / "update.json"
    manifest.write_text(json.dumps({"latest_version": "999.0.0", "sha256": "x"}))
    assert cli.main(["update", "--check", "--manifest", str(manifest)]) == 0
    out = capsys.readouterr().out
    assert "999.0.0" in out and "tick update" in out

    manifest.write_text(json.dumps({"latest_version": "0.0.1", "sha256": "x"}))
    assert cli.main(["update", "--check", "--manifest", str(manifest)]) == 0
    assert "current" in capsys.readouterr().out.lower()


def test_cli_update_unreachable_manifest_is_graceful(tmp_path, capsys):
    # missing/unreachable manifest -> friendly error + exit 1, never a traceback
    assert cli.main(["update", "--check", "--manifest", str(tmp_path / "nope.json")]) == 1
    assert "update server" in capsys.readouterr().err


def test_cli_uninitialized_errors(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    assert cli.main(["ls"]) == 1
    assert "not initialized" in capsys.readouterr().err
