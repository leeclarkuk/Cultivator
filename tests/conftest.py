from pathlib import Path

import pytest
import yaml


class Workspace:
    """Builds a throwaway workspace on disk."""

    def __init__(self, root: Path):
        self.root = root
        self.write_config()

    def write_config(self, **spec):
        spec.setdefault("modules", ["modules/*"])
        spec.setdefault("parallelism", 4)
        doc = {"apiVersion": "cultivator/v1", "kind": "Config", "spec": spec}
        (self.root / "cultivator.yaml").write_text(yaml.safe_dump(doc))

    def module(self, name, *, depends_on=(), apply=None, files=None, **spec):
        base = self.root / "modules" / name
        base.mkdir(parents=True, exist_ok=True)
        if apply is None:
            apply = f'echo {name} >> "$CULTIVATOR_ROOT/applied.log"'
        spec = {"apply": apply, **spec}
        if depends_on:
            spec["dependsOn"] = list(depends_on)
        doc = {"apiVersion": "cultivator/v1", "kind": "Module", "metadata": {"name": name}, "spec": spec}
        (base / "module.yaml").write_text(yaml.safe_dump(doc))
        for rel, content in (files or {"src/app.txt": "v1"}).items():
            target = base / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return base

    def edit(self, module, rel="src/app.txt", content="changed"):
        (self.root / "modules" / module / rel).write_text(content)

    def applied(self) -> list[str]:
        log = self.root / "applied.log"
        return log.read_text().split() if log.exists() else []

    def clear_log(self):
        (self.root / "applied.log").unlink(missing_ok=True)


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path.resolve())


@pytest.fixture
def chain(ws):
    """network <- database <- api <- web"""
    ws.module("network")
    ws.module("database", depends_on=["network"])
    ws.module("api", depends_on=["database"])
    ws.module("web", depends_on=["api"])
    return ws
