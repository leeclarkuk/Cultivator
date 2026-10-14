import os
import subprocess
import time

import pytest

from cultivator.engine import Engine
from cultivator.errors import CultivatorError, StateError
from cultivator.executor import Status
from cultivator.planner import Action


def plan_of(ws, env=None, **kw):
    engine = Engine(ws.root, env)
    return engine, engine.plan(**kw)


def apply(ws, env=None, jobs=None, keep_going=False, **kw):
    engine, plan = plan_of(ws, env, **kw)
    with engine.lock():
        outcomes = engine.apply(plan, jobs=jobs, keep_going=keep_going)
    return {o.name: o for o in outcomes}


def actions(plan):
    return {n: i.action for n, i in plan.items.items()}


def changed(plan):
    return {n for n, i in plan.items.items() if i.action is not Action.NOOP}


def test_first_plan_creates_everything_in_dependency_order(chain):
    _, plan = plan_of(chain)
    assert set(actions(plan).values()) == {Action.CREATE}
    assert plan.stages() == [["network"], ["database"], ["api"], ["web"]]


def test_apply_then_replan_is_a_noop(chain):
    outcomes = apply(chain)
    assert all(o.status is Status.OK for o in outcomes.values())
    assert chain.applied() == ["network", "database", "api", "web"]
    _, plan = plan_of(chain)
    assert not plan.has_changes
    chain.clear_log()
    assert apply(chain) == {}
    assert chain.applied() == []


def test_leaf_change_only_redeploys_the_leaf(chain):
    apply(chain)
    chain.edit("web")
    _, plan = plan_of(chain)
    assert changed(plan) == {"web"}
    assert "1 file changed" in plan.items["web"].reasons[0]


def test_change_propagates_to_dependents_only(chain):
    apply(chain)
    chain.edit("database")
    _, plan = plan_of(chain)
    assert changed(plan) == {"database", "api", "web"}
    assert plan.items["api"].reasons == ["dependency changed: database"]
    assert plan.items["network"].action is Action.NOOP


def test_propagate_false_stops_the_cascade(ws):
    ws.module("db")
    ws.module("api", depends_on=["db"])
    ws.module("worker", depends_on=["db", {"name": "api", "propagate": False}])
    apply(ws)
    ws.edit("api")
    _, plan = plan_of(ws)
    assert changed(plan) == {"api"}
    ws.clear_log()
    apply(ws)
    assert ws.applied() == ["api"]


def test_ordering_still_holds_without_propagation(ws):
    ws.module("a", apply='sleep 0.3; echo a >> "$CULTIVATOR_ROOT/applied.log"')
    ws.module("b", depends_on=[{"name": "a", "propagate": False}])
    apply(ws)
    assert ws.applied() == ["a", "b"]


def test_whitespace_only_manifest_edit_is_not_a_change(chain):
    apply(chain)
    path = chain.root / "modules" / "api" / "module.yaml"
    path.write_text("# a comment\n" + path.read_text())
    _, plan = plan_of(chain)
    assert not plan.has_changes


def test_command_change_is_detected(chain):
    apply(chain)
    path = chain.root / "modules" / "api" / "module.yaml"
    path.write_text(path.read_text().replace("echo api", "echo api-v2"))
    _, plan = plan_of(chain)
    assert "api" in changed(plan)


def test_input_change_reports_which_keys(ws):
    ws.write_config(environments={"dev": {"vars": {"n": 1}}})
    ws.module("app", inputs={"replicas": "${vars.n}", "region": "x"})
    apply(ws)
    ws.write_config(environments={"dev": {"vars": {"n": 2}}})
    _, plan = plan_of(ws)
    assert plan.items["app"].reasons == ["inputs changed: replicas"]


def test_environments_have_independent_state(ws):
    ws.write_config(environments={"dev": {}, "prod": {}})
    ws.module("app")
    apply(ws, "dev")
    assert not plan_of(ws, "dev")[1].has_changes
    assert actions(plan_of(ws, "prod")[1]) == {"app": Action.CREATE}


def test_files_outside_watch_patterns_are_ignored(ws):
    ws.module("app", watch=["src/**"], files={"src/a.txt": "1", "docs/readme.md": "1"})
    apply(ws)
    ws.edit("app", "docs/readme.md", "2")
    assert not plan_of(ws)[1].has_changes
    ws.edit("app", "src/a.txt", "2")
    assert changed(plan_of(ws)[1]) == {"app"}


def test_shared_directory_outside_module_is_watched(ws):
    (ws.root / "shared").mkdir()
    (ws.root / "shared" / "lib.txt").write_text("1")
    ws.module("app", watch=["src/**", "../../shared/**"])
    apply(ws)
    (ws.root / "shared" / "lib.txt").write_text("2")
    assert changed(plan_of(ws)[1]) == {"app"}


def test_targets_include_dependencies_only(chain):
    _, plan = plan_of(chain, targets=["api"])
    assert changed(plan) == {"network", "database", "api"}
    assert plan.items["web"].reasons == ["not targeted"]


def test_unknown_target_is_an_error(chain):
    with pytest.raises(CultivatorError, match="unknown module"):
        plan_of(chain, targets=["nope"])


def test_force_and_all(chain):
    apply(chain)
    assert changed(plan_of(chain, force=["api"])[1]) == {"api"}
    assert changed(plan_of(chain, everything=True)[1]) == set(chain_modules(chain))


def chain_modules(ws):
    return ["network", "database", "api", "web"]


def test_failure_skips_dependents_and_leaves_state_stale(ws):
    ws.module("db")
    ws.module("api", depends_on=["db"], apply="exit 3")
    ws.module("web", depends_on=["api"])
    ws.module("metrics")
    out = apply(ws)
    assert out["db"].status is Status.OK
    assert out["api"].status is Status.FAILED and "exit status 3" in out["api"].output
    assert out["web"].status is Status.SKIPPED
    _, plan = plan_of(ws)
    assert out["metrics"].status is Status.OK  # started alongside db, before the failure
    assert changed(plan) == {"api", "web"}


def test_fail_fast_versus_keep_going(ws):
    ws.module("bad", apply="exit 1")
    ws.module("slow", apply='sleep 0.4; echo slow >> "$CULTIVATOR_ROOT/applied.log"')
    ws.module("later", depends_on=["slow"])
    out = apply(ws, jobs=2)
    assert out["bad"].status is Status.FAILED
    assert out["slow"].status is Status.OK  # was already running
    assert out["later"].status is Status.SKIPPED

    ws.clear_log()
    out = apply(ws, jobs=2, keep_going=True)
    assert out["later"].status is Status.OK


def test_failure_is_retried_on_next_apply(ws):
    ws.module("flaky", apply='test -f "$CULTIVATOR_ROOT/ok"')
    assert apply(ws)["flaky"].status is Status.FAILED
    (ws.root / "ok").write_text("")
    assert apply(ws)["flaky"].status is Status.OK
    assert not plan_of(ws)[1].has_changes


def test_retries(ws):
    script = 'n=$(cat "$CULTIVATOR_ROOT/n" 2>/dev/null || echo 0); echo $((n+1)) > "$CULTIVATOR_ROOT/n"; test $n -ge 2'
    ws.module("flaky", apply=script, retries=2)
    out = apply(ws)["flaky"]
    assert out.status is Status.OK and out.output.count("retry") == 2


def test_verify_failure_fails_the_module(ws):
    ws.module("app", verify="exit 1")
    out = apply(ws)["app"]
    assert out.status is Status.FAILED and out.message == "verify failed"
    assert plan_of(ws)[1].has_changes


def test_timeout_kills_the_command(ws):
    ws.module("slow", apply="sleep 30", timeout=0.3)
    start = time.monotonic()
    out = apply(ws)["slow"]
    assert out.status is Status.FAILED and "timed out" in out.output
    assert time.monotonic() - start < 5


def test_independent_modules_run_in_parallel(ws):
    for name in "abc":
        ws.module(name, apply="sleep 0.5")
    start = time.monotonic()
    apply(ws, jobs=3)
    assert time.monotonic() - start < 1.3
    ws2_start = time.monotonic()
    apply(ws, jobs=1, everything=True)
    assert time.monotonic() - ws2_start >= 1.4


def test_argv_commands_run_without_a_shell(ws):
    ws.module("app", apply=[["touch", "made.txt"]])
    apply(ws)
    assert (ws.root / "modules" / "app" / "made.txt").exists()


def test_module_environment_is_exported(ws):
    ws.module("app", inputs={"replica-count": 3}, env={"MODE": "fast"},
              apply='echo "$CULTIVATOR_MODULE $CULTIVATOR_INPUT_REPLICA_COUNT $MODE $CULTIVATOR_ENVIRONMENT" > "$CULTIVATOR_ROOT/seen"')
    apply(ws)
    assert (ws.root / "seen").read_text().strip() == "app 3 fast default"


def test_removed_module_is_destroyed_after_deploys(ws):
    ws.module("keep")
    ws.module("old", destroy='echo old >> "$CULTIVATOR_ROOT/destroyed.log"')
    ws.module("older", depends_on=["old"], destroy='echo older >> "$CULTIVATOR_ROOT/destroyed.log"')
    apply(ws)
    for name in ("old", "older"):
        subprocess.run(["rm", "-rf", str(ws.root / "modules" / name)], check=True)
    ws.module("fresh")
    _, plan = plan_of(ws)
    assert {i.name for i in plan.deletes} == {"old", "older"}
    out = apply(ws)
    assert out["old"].status is Status.OK
    assert (ws.root / "destroyed.log").read_text().split() == ["older", "old"]  # dependents first
    assert not plan_of(ws)[1].has_changes


def test_no_prune_and_targets_do_not_delete(ws):
    ws.module("keep")
    ws.module("old")
    apply(ws)
    subprocess.run(["rm", "-rf", str(ws.root / "modules" / "old")], check=True)
    assert plan_of(ws, prune=False)[1].deletes == []
    assert plan_of(ws, targets=["keep"])[1].deletes == []


def test_deletes_wait_for_failed_deploys(ws):
    ws.module("old", destroy='touch "$CULTIVATOR_ROOT/destroyed"')
    apply(ws)
    subprocess.run(["rm", "-rf", str(ws.root / "modules" / "old")], check=True)
    ws.module("new", apply="exit 1")
    apply(ws)
    assert not (ws.root / "destroyed").exists()


def test_drift_detection_with_check(ws):
    ws.module("app", apply='touch "$CULTIVATOR_ROOT/live"', check='test -f "$CULTIVATOR_ROOT/live"')
    ws.module("nocheck")
    apply(ws)
    assert not plan_of(ws, refresh=True)[1].has_changes
    (ws.root / "live").unlink()
    assert not plan_of(ws)[1].has_changes  # cheap plans do not look at reality
    _, plan = plan_of(ws, refresh=True)
    assert changed(plan) == {"app"} and plan.items["app"].reasons[0].startswith("drift")
    apply(ws, refresh=True)
    assert (ws.root / "live").exists()


def test_state_lock_blocks_concurrent_runs(chain):
    engine = Engine(chain.root)
    with engine.lock():
        with pytest.raises(StateError, match="locked"):
            with Engine(chain.root).lock():
                pass
    with Engine(chain.root).lock():
        pass


def test_stale_lock_from_dead_process_is_reclaimed(chain):
    engine = Engine(chain.root)
    engine.store.directory.mkdir(parents=True)
    import socket
    engine.store.lock_path.write_text(f"{socket.gethostname()}:999999\n0\n")
    with engine.lock():
        pass


def test_cycle_is_rejected(ws):
    ws.module("a", depends_on=["b"])
    ws.module("b", depends_on=["a"])
    with pytest.raises(CultivatorError, match="dependency cycle"):
        Engine(ws.root)


def test_corrupt_state_is_reported(chain):
    apply(chain)
    path = Engine(chain.root).store.path
    path.write_text("{not json")
    with pytest.raises(StateError, match="unreadable"):
        plan_of(chain)


# stateless change detection against git

@pytest.fixture
def repo(chain):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}

    def git(*args):
        subprocess.run(["git", "-C", str(chain.root), *args], check=True, env=env, capture_output=True)

    git("init", "-q", "-b", "main")
    (chain.root / ".gitignore").write_text(".cultivator/\napplied.log\n")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    chain.git = git
    return chain


def test_since_finds_changed_modules_and_their_dependents(repo):
    _, plan = plan_of(repo, since="HEAD")
    assert not plan.has_changes

    repo.edit("api")
    _, plan = plan_of(repo, since="HEAD")
    assert changed(plan) == {"api", "web"}
    assert plan.items["web"].reasons == ["dependency changed: api"]

    repo.git("add", "-A")
    repo.git("commit", "-q", "-m", "api")
    assert not plan_of(repo, since="HEAD")[1].has_changes
    assert changed(plan_of(repo, since="HEAD~1")[1]) == {"api", "web"}


def test_since_sees_new_and_deleted_files(repo):
    (repo.root / "modules" / "network" / "src" / "new.txt").write_text("x")
    assert changed(plan_of(repo, since="HEAD")[1]) >= {"network"}
    repo.git("add", "-A")
    repo.git("commit", "-q", "-m", "n")
    (repo.root / "modules" / "web" / "src" / "app.txt").unlink()
    assert changed(plan_of(repo, since="HEAD")[1]) == {"web"}


def test_since_unknown_ref_is_an_error(repo):
    with pytest.raises(CultivatorError):
        plan_of(repo, since="no-such-ref")


def test_root_config_change_marks_everything(repo):
    repo.write_config(parallelism=2)
    assert len(changed(plan_of(repo, since="HEAD")[1])) == 4
