"""Running module commands and scheduling them across the dependency graph."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from .graph import Graph
from .manifest import Module, Workspace
from .planner import Plan, PlanItem, Snapshot
from .state import Record, State, StateStore, utcnow


class Status(str, Enum):
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class Outcome:
    name: str
    action: str
    status: Status
    seconds: float = 0.0
    message: str = ""
    output: str = ""


def _argv(command):
    return command if isinstance(command, str) else list(command)


def _display(command) -> str:
    return command if isinstance(command, str) else " ".join(map(str, command))


def command_env(root: Path, environment: str, name: str, inputs: dict, extra: dict) -> dict:
    env = dict(os.environ)
    env.update({
        "CULTIVATOR_ROOT": str(root),
        "CULTIVATOR_ENVIRONMENT": environment,
        "CULTIVATOR_MODULE": name,
    })
    for key, value in inputs.items():
        safe = "".join(c if c.isalnum() else "_" for c in str(key)).upper()
        env[f"CULTIVATOR_INPUT_{safe}"] = str(value)
    env.update(extra)
    return env


def run_command(command, cwd: Path, env: dict, timeout: float | None) -> tuple[int, str]:
    try:
        proc = subprocess.Popen(
            _argv(command), shell=isinstance(command, str), cwd=cwd, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", start_new_session=True,
        )
    except OSError as exc:
        return 127, f"{exc}\n"
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
        return 124, f"{out}\n[timed out after {timeout:g}s]\n"
    return proc.returncode, out


def run_phase(commands, cwd: Path, env: dict, timeout: float | None, retries: int) -> tuple[bool, str]:
    """Run commands in order, stopping at the first failure; retry the whole phase."""
    log: list[str] = []
    for attempt in range(retries + 1):
        if attempt:
            log.append(f"-- retry {attempt}/{retries}\n")
        failed = False
        for command in commands:
            log.append(f"$ {_display(command)}\n")
            code, out = run_command(command, cwd, env, timeout)
            log.append(out if out.endswith("\n") or not out else out + "\n")
            if code != 0:
                log.append(f"[exit status {code}]\n")
                failed = True
                break
        if not failed:
            return True, "".join(log)
    return False, "".join(log)


def serialise(commands) -> list:
    return [c if isinstance(c, str) else list(c) for c in commands]


def deserialise(commands) -> list:
    return [c if isinstance(c, str) else tuple(c) for c in commands]


def check_module(ws: Workspace, module: Module) -> tuple[bool, str]:
    env = command_env(ws.root, ws.environment, module.name, module.inputs, module.env)
    ok, out = run_phase(module.check, module.path, env, module.timeout, 0)
    return ok, out


class Executor:
    def __init__(
        self,
        ws: Workspace,
        plan: Plan,
        state: State,
        store: StateStore,
        *,
        jobs: int,
        keep_going: bool = False,
        commit: str | None = None,
        on_start: Callable[[str, str], None] | None = None,
        on_finish: Callable[[Outcome], None] | None = None,
    ):
        self.ws, self.plan, self.state, self.store = ws, plan, state, store
        self.jobs = max(1, jobs)
        self.keep_going = keep_going
        self.commit = commit
        self.on_start = on_start or (lambda name, action: None)
        self.on_finish = on_finish or (lambda outcome: None)
        self.outcomes: list[Outcome] = []

    def _finish(self, outcome: Outcome) -> None:
        self.outcomes.append(outcome)
        self.on_finish(outcome)

    def run(self) -> list[Outcome]:
        self._deploy_all()
        if not any(o.status is not Status.OK for o in self.outcomes):
            self._delete_all()
        return self.outcomes

    # deploys

    def _deploy(self, item: PlanItem) -> Outcome:
        module = self.ws.modules[item.name]
        start = time.monotonic()
        env = command_env(self.ws.root, self.ws.environment, module.name, module.inputs, module.env)
        ok, out = run_phase(module.apply, module.path, env, module.timeout, module.retries)
        phase = "apply"
        if ok and module.verify:
            ok, verify_out = run_phase(module.verify, module.path, env, module.timeout, module.retries)
            out += verify_out
            phase = "verify"
        elapsed = time.monotonic() - start
        if ok:
            return Outcome(item.name, item.action.value, Status.OK, elapsed, output=out)
        return Outcome(item.name, item.action.value, Status.FAILED, elapsed, f"{phase} failed", out)

    def _record(self, module: Module, snap: Snapshot, seconds: float) -> Record:
        return Record(
            fingerprint=snap.fingerprint, spec=snap.spec, files_digest=snap.files_digest,
            files=snap.files, deps=snap.deps, inputs=module.inputs,
            depends_on=module.dependency_names, destroy=serialise(module.destroy),
            cwd=Path(os.path.relpath(module.path, self.ws.root)).as_posix(),
            env=module.env, applied_at=utcnow(), commit=self.commit, duration=round(seconds, 3),
        )

    def _deploy_all(self) -> None:
        deploys = self.plan.deploys
        if not deploys:
            return
        deps = {n: self.plan.graph.edges[n] & deploys for n in deploys}
        pending, done = set(deploys), set()
        running: dict = {}
        halted = False

        def skip_dependents(root: str, reason: str) -> None:
            stack = [root]
            while stack:
                cur = stack.pop()
                for n in sorted(p for p in pending if cur in deps[p]):
                    pending.discard(n)
                    self._finish(Outcome(n, self.plan.items[n].action.value, Status.SKIPPED, message=reason))
                    stack.append(n)

        pool = ThreadPoolExecutor(max_workers=self.jobs)
        try:
            while pending or running:
                if not halted:
                    ready = sorted(n for n in pending if deps[n] <= done)
                    for name in ready[: self.jobs - len(running)]:
                        pending.discard(name)
                        item = self.plan.items[name]
                        self.on_start(name, item.action.value)
                        running[pool.submit(self._deploy, item)] = name
                if not running:
                    break
                finished, _ = wait(running, return_when=FIRST_COMPLETED)
                for fut in finished:
                    name = running.pop(fut)
                    self._settle(fut.result(), name, done)
                    if self.outcomes[-1].status is Status.FAILED:
                        skip_dependents(name, f"dependency {name} failed")
                        halted = halted or not self.keep_going
        except KeyboardInterrupt:
            halted = True
            for fut in wait(running).done:
                self._settle(fut.result(), running.pop(fut), done)
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
            for name in sorted(pending):
                self._finish(Outcome(name, self.plan.items[name].action.value, Status.SKIPPED,
                                     message="not started after an earlier failure"))

    def _settle(self, outcome: Outcome, name: str, done: set) -> None:
        if outcome.status is Status.OK:
            module, snap = self.ws.modules[name], self.plan.items[name].snapshot
            self.state.put(name, self._record(module, snap, outcome.seconds))
            self.store.save(self.state)
            done.add(name)
        self._finish(outcome)

    # deletes

    def _delete_all(self) -> None:
        removed = {i.name: i for i in self.plan.deletes}
        if not removed:
            return
        graph = Graph({n: set(i.record.depends_on) & set(removed) for n, i in removed.items()})
        for name in reversed(graph.order()):  # dependents are torn down before their dependencies
            self.on_start(name, "delete")
            outcome = self._destroy(removed[name])
            if outcome.status is Status.OK:
                self.state.drop(name)
                self.store.save(self.state)
            self._finish(outcome)

    def _destroy(self, item: PlanItem) -> Outcome:
        record = item.record
        cwd = self.ws.root / record.cwd
        env = command_env(self.ws.root, self.ws.environment, item.name, record.inputs, record.env)
        start = time.monotonic()
        ok, out = run_phase(deserialise(record.destroy), cwd if cwd.is_dir() else self.ws.root, env, None, 0)
        status = Status.OK if ok else Status.FAILED
        return Outcome(item.name, "delete", status, time.monotonic() - start,
                       "" if ok else "destroy failed", out)
