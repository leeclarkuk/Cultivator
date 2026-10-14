import json

import pytest

from cultivator.cli import main


def run(capsys, *argv):
    code = main(["--no-color", *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_init_creates_a_working_workspace(tmp_path, capsys):
    assert run(capsys, "init", str(tmp_path))[0] == 0
    assert ".cultivator/" in (tmp_path / ".gitignore").read_text()
    code, out, _ = run(capsys, "-C", str(tmp_path), "apply", "--yes", "-v")
    assert code == 0 and "hello from hello in dev" in out and "Applied 1" in out
    code, out, _ = run(capsys, "-C", str(tmp_path), "apply", "--yes", "-v")
    assert code == 0 and "Nothing to do." in out


def test_init_refuses_to_overwrite(tmp_path, capsys):
    run(capsys, "init", str(tmp_path))
    code, _, err = run(capsys, "init", str(tmp_path))
    assert code == 1 and "already exists" in err


def test_validate_and_graph(chain, capsys):
    assert "OK: 4 modules" in run(capsys, "-C", str(chain.root), "validate")[1]
    out = run(capsys, "-C", str(chain.root), "graph")[1]
    assert "Longest chain: network -> database -> api -> web" in out
    dot = run(capsys, "-C", str(chain.root), "graph", "--format", "dot")[1]
    assert '"api" -> "web";' in dot
    edges = json.loads(run(capsys, "-C", str(chain.root), "graph", "--format", "json")[1])
    assert edges["web"] == ["api"]


def test_plan_exit_codes_and_json(chain, capsys):
    root = str(chain.root)
    code, out, _ = run(capsys, "-C", root, "plan", "--detailed-exitcode")
    assert code == 2 and "4 to create" in out
    run(capsys, "-C", root, "apply", "-y")
    assert run(capsys, "-C", root, "plan", "--detailed-exitcode")[0] == 0

    chain.edit("api")
    code, out, _ = run(capsys, "-C", root, "plan", "--format", "json")
    data = json.loads(out)
    assert code == 0
    assert data["summary"]["update"] == 2
    assert [m["name"] for m in data["modules"] if m["action"] == "update"] == ["api", "web"]


def test_global_flags_work_after_the_subcommand(chain, capsys):
    code, out, _ = run(capsys, "plan", "-C", str(chain.root), "--no-color")
    assert code == 0 and "4 to create" in out


def test_apply_requires_confirmation_when_not_interactive(chain, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    code, _, err = run(capsys, "-C", str(chain.root), "apply")
    assert code == 1 and "--yes" in err
    assert chain.applied() == []


def test_apply_prompt_can_be_declined(chain, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    code, out, _ = run(capsys, "-C", str(chain.root), "apply")
    assert code == 0 and "Cancelled." in out and chain.applied() == []


def test_dry_run_changes_nothing(chain, capsys):
    code, out, _ = run(capsys, "-C", str(chain.root), "apply", "--dry-run")
    assert code == 0 and "4 to create" in out and chain.applied() == []


def test_failed_apply_exits_non_zero_and_shows_output(ws, capsys):
    ws.module("bad", apply="echo boom; exit 4")
    code, out, _ = run(capsys, "-C", str(ws.root), "apply", "-y")
    assert code == 1 and "FAILED" in out and "boom" in out and "failed 1" in out


def test_errors_are_reported_without_a_traceback(tmp_path, capsys):
    code, _, err = run(capsys, "-C", str(tmp_path), "plan")
    assert code == 1 and err.startswith("error: no cultivator.yaml")


def test_watch_once_reconciles(chain, capsys):
    code, out, _ = run(capsys, "-C", str(chain.root), "watch", "--once")
    assert code == 0 and chain.applied() == ["network", "database", "api", "web"]


def test_watch_once_reports_failure(ws, capsys):
    ws.module("bad", apply="exit 1")
    assert run(capsys, "-C", str(ws.root), "watch", "--once")[0] == 1


def test_target_flag(chain, capsys):
    code, out, _ = run(capsys, "-C", str(chain.root), "apply", "-y", "-t", "database")
    assert code == 0 and chain.applied() == ["network", "database"]


@pytest.mark.parametrize("flag", ["--version"])
def test_version(capsys, flag):
    with pytest.raises(SystemExit) as exc:
        main([flag])
    assert exc.value.code == 0
    assert "cultivator 0.1.0" in capsys.readouterr().out
