"""Fingerprinting and plan construction.

A module's fingerprint covers its resolved spec, the content of its watched
files and the fingerprints of the dependencies it propagates changes from. A
module is out of date exactly when that fingerprint differs from the one
recorded at its last successful apply, so change propagation falls out of the
hashing rather than needing a separate traversal.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .fileset import FileHasher, FileSet, canonical_digest
from .graph import Graph
from .manifest import Module, Workspace
from .state import Record, State


class Action(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    NOOP = "unchanged"


@dataclass(frozen=True)
class Snapshot:
    spec: str
    files: dict
    files_digest: str
    deps: dict
    fingerprint: str


@dataclass
class PlanItem:
    name: str
    action: Action
    reasons: list = field(default_factory=list)
    snapshot: Snapshot | None = None
    record: Record | None = None  # previous state, kept for deletes


@dataclass
class Plan:
    items: dict
    graph: Graph

    def with_action(self, *actions: Action) -> list[PlanItem]:
        return [i for i in self.items.values() if i.action in actions]

    @property
    def deploys(self) -> set[str]:
        return {i.name for i in self.with_action(Action.CREATE, Action.UPDATE)}

    @property
    def deletes(self) -> list[PlanItem]:
        return self.with_action(Action.DELETE)

    @property
    def has_changes(self) -> bool:
        return any(i.action is not Action.NOOP for i in self.items.values())

    def stages(self) -> list[list[str]]:
        return self.graph.levels(self.deploys) if self.deploys else []

    def counts(self) -> dict[str, int]:
        out = {a.value: 0 for a in Action}
        for item in self.items.values():
            out[item.action.value] += 1
        return out

    def to_dict(self) -> dict:
        return {
            "summary": self.counts(),
            "stages": self.stages(),
            "modules": [
                {"name": i.name, "action": i.action.value, "reasons": i.reasons,
                 "fingerprint": i.snapshot.fingerprint if i.snapshot else None}
                for i in sorted(self.items.values(), key=lambda i: i.name)
            ],
        }


def build_graph(ws: Workspace) -> Graph:
    graph = Graph({n: set(m.dependency_names) for n, m in ws.modules.items()})
    graph.validate()
    return graph


def fileset_for(module: Module) -> FileSet:
    return FileSet(module.path, module.watch, module.ignore)


def spec_payload(module: Module) -> dict:
    return {"inputs": module.inputs, "env": module.env,
            "apply": module.apply, "verify": module.verify}


def take_snapshots(ws: Workspace, graph: Graph, hasher: FileHasher) -> dict[str, Snapshot]:
    snaps: dict[str, Snapshot] = {}
    for name in graph.order():
        module = ws.modules[name]
        files = {}
        for path in fileset_for(module).files():
            if path == module.manifest:
                continue  # covered semantically by the spec digest
            files[Path(os.path.relpath(path, ws.root)).as_posix()] = hasher.digest(path)
        spec = canonical_digest(spec_payload(module))
        files_digest = canonical_digest(files)
        deps = {d: snaps[d].fingerprint for d in module.propagating()}
        snaps[name] = Snapshot(
            spec=spec, files=files, files_digest=files_digest, deps=deps,
            fingerprint=canonical_digest({"spec": spec, "files": files_digest, "deps": deps}),
        )
    return snaps


def _brief(names, limit: int = 3) -> str:
    names = sorted(names)
    shown = ", ".join(names[:limit])
    return shown + (f", +{len(names) - limit} more" if len(names) > limit else "")


def explain(record: Record, snap: Snapshot, inputs: dict) -> list[str]:
    """Human-readable reasons a deployed module is out of date."""
    reasons: list[str] = []
    if record.spec != snap.spec:
        changed = {k for k in set(record.inputs) | set(inputs) if record.inputs.get(k) != inputs.get(k)}
        reasons.append(f"inputs changed: {_brief(changed)}" if changed else "commands or environment changed")
    if record.files_digest != snap.files_digest:
        old, new = record.files, snap.files
        delta = {p for p in set(old) | set(new) if old.get(p) != new.get(p)}
        reasons.append(f"{len(delta)} file{'s' if len(delta) != 1 else ''} changed ({_brief(delta)})")
    moved = sorted(d for d in set(record.deps) | set(snap.deps) if record.deps.get(d) != snap.deps.get(d))
    if moved:
        reasons.append(f"dependency changed: {_brief(moved)}")
    if not reasons and record.fingerprint != snap.fingerprint:
        reasons.append("fingerprint changed")
    return reasons


def build_plan(
    ws: Workspace,
    graph: Graph,
    snaps: dict[str, Snapshot],
    state: State,
    *,
    targets: set[str] | None = None,
    changed: dict[str, str] | None = None,
    drifted: dict[str, str] | None = None,
    force: set[str] | None = None,
    prune: bool = True,
) -> Plan:
    """Decide what to do for each module.

    ``changed`` switches to stateless mode (as used by ``--since``): modules named
    there are treated as directly changed, and their dependents follow through
    propagating edges. Otherwise decisions come from comparing fingerprints with
    ``state``.
    """
    drifted = drifted or {}
    force = force or set()
    selected = set(ws.modules) if not targets else set(targets) | graph.ancestors(targets)
    items: dict[str, PlanItem] = {}

    for name in graph.order():
        snap = snaps[name]
        if name not in selected:
            items[name] = PlanItem(name, Action.NOOP, ["not targeted"], snap, state.get(name))
            continue

        record = state.get(name)
        reasons: list[str] = []
        if changed is not None:
            if name in changed:
                reasons.append(changed[name])
            upstream = [d for d in ws.modules[name].propagating() if items[d].action in (Action.CREATE, Action.UPDATE)]
            if upstream:
                reasons.append(f"dependency changed: {_brief(upstream)}")
            action = Action.UPDATE
        elif record is None:
            reasons.append("not deployed yet")
            action = Action.CREATE
        else:
            reasons = explain(record, snap, ws.modules[name].inputs)
            action = Action.UPDATE
        if name in drifted:
            reasons.append(f"drift: {drifted[name]}")
        if name in force:
            reasons.append("forced")
        if not reasons:
            action = Action.NOOP
        items[name] = PlanItem(name, action, reasons, snap, record)

    if prune and not targets and changed is None:
        for name, record in state.modules.items():
            if name not in ws.modules:
                items[name] = PlanItem(name, Action.DELETE, ["no longer in the manifests"], None, record)

    return Plan(items=items, graph=graph)
