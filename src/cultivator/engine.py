"""High-level operations used by the CLI: planning and applying for one environment."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import gitutil
from .errors import CultivatorError
from .executor import Executor, Outcome, check_module
from .fileset import FileHasher
from .manifest import CONFIG_FILE, load_workspace
from .planner import Action, Plan, build_graph, build_plan, fileset_for, take_snapshots
from .state import StateStore


class Engine:
    def __init__(self, root: Path, environment: str | None = None):
        self.ws = load_workspace(root, environment)
        self.graph = build_graph(self.ws)
        self.store = StateStore(self.ws.config.state_dir, self.ws.environment)
        self.commit = gitutil.head(root) if gitutil.is_repo(root) else None
        self.hasher: FileHasher | None = None

    def lock(self):
        return self.store.lock()

    @property
    def jobs(self) -> int:
        return self.ws.config.parallelism

    def _changed_since(self, ref: str) -> dict[str, str]:
        files = gitutil.changed_files(self.ws.root, ref)
        if (self.ws.root / CONFIG_FILE).resolve() in {f.resolve() for f in files}:
            return {name: f"{CONFIG_FILE} changed since {ref}" for name in self.ws.modules}
        changed = {}
        for name, module in self.ws.modules.items():
            fs = fileset_for(module)
            hits = [f for f in files if fs.includes(f) or f == module.manifest]
            if hits:
                changed[name] = f"{len(hits)} file{'s' if len(hits) != 1 else ''} changed since {ref}"
        return changed

    def _detect_drift(self, plan: Plan, jobs: int) -> dict[str, str]:
        candidates = [i.name for i in plan.items.values()
                      if i.action is Action.NOOP and i.record is not None and self.ws.modules[i.name].check]
        if not candidates:
            return {}
        with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
            results = list(pool.map(lambda n: check_module(self.ws, self.ws.modules[n]), candidates))
        drifted = {}
        for name, (ok, output) in zip(candidates, results):
            if not ok:
                lines = [l for l in output.splitlines() if l and not l.startswith(("$", "[exit"))]
                drifted[name] = lines[-1] if lines else "check failed"
        return drifted

    def plan(
        self,
        *,
        targets=(),
        since: str | None = None,
        refresh: bool = False,
        force=(),
        everything: bool = False,
        prune: bool = True,
        jobs: int | None = None,
    ) -> Plan:
        unknown = sorted((set(targets) | set(force)) - set(self.ws.modules))
        if unknown:
            raise CultivatorError(f"unknown module(s): {', '.join(unknown)}")

        self.hasher = FileHasher(self.store.load_cache())
        snaps = take_snapshots(self.ws, self.graph, self.hasher)
        self.store.save_cache(self.hasher.cache)

        state = self.store.load()
        changed = self._changed_since(since) if since else None
        forced = set(self.ws.modules) if everything else set(force)
        options = dict(targets=set(targets) or None, changed=changed, force=forced, prune=prune)

        plan = build_plan(self.ws, self.graph, snaps, state, **options)
        if refresh:
            drifted = self._detect_drift(plan, jobs or self.jobs)
            if drifted:
                plan = build_plan(self.ws, self.graph, snaps, state, drifted=drifted, **options)
        self._state = state
        return plan

    def apply(self, plan: Plan, *, jobs: int | None = None, keep_going: bool = False,
              on_start=None, on_finish=None) -> list[Outcome]:
        executor = Executor(
            self.ws, plan, self._state, self.store,
            jobs=jobs or self.jobs, keep_going=keep_going, commit=self.commit,
            on_start=on_start, on_finish=on_finish,
        )
        return executor.run()
