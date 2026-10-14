"""Persistent record of what has been applied, plus the hash cache and run lock."""

from __future__ import annotations

import json
import os
import socket
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .errors import StateError

STATE_VERSION = 1


@dataclass
class Record:
    fingerprint: str
    spec: str
    files_digest: str
    files: dict
    deps: dict
    inputs: dict
    depends_on: list
    destroy: list
    cwd: str
    env: dict
    applied_at: str
    commit: str | None = None
    duration: float = 0.0

    @classmethod
    def from_dict(cls, data: dict) -> "Record":
        known = cls.__dataclass_fields__
        return cls(**{k: v for k, v in data.items() if k in known})


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class State:
    environment: str
    modules: dict = field(default_factory=dict)

    def get(self, name: str) -> Record | None:
        return self.modules.get(name)

    def put(self, name: str, record: Record) -> None:
        self.modules[name] = record

    def drop(self, name: str) -> None:
        self.modules.pop(name, None)


class StateStore:
    def __init__(self, directory: Path, environment: str):
        self.directory = directory
        self.environment = environment
        self.path = directory / f"{environment}.json"
        self.lock_path = directory / f"{environment}.lock"
        self.cache_path = directory.parent / "cache" / "hashes.json"

    def load(self) -> State:
        if not self.path.exists():
            return State(self.environment)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if raw.get("version") != STATE_VERSION:
                raise StateError(f"{self.path}: unsupported state version {raw.get('version')!r}")
            modules = {n: Record.from_dict(r) for n, r in raw.get("modules", {}).items()}
        except (json.JSONDecodeError, TypeError, AttributeError) as exc:
            raise StateError(f"{self.path}: unreadable state file ({exc})") from exc
        return State(self.environment, modules)

    def save(self, state: State) -> None:
        _atomic_write(self.path, {
            "version": STATE_VERSION,
            "environment": state.environment,
            "modules": {n: asdict(r) for n, r in sorted(state.modules.items())},
        })

    def load_cache(self) -> dict:
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def save_cache(self, cache: dict) -> None:
        try:
            _atomic_write(self.cache_path, cache)
        except OSError:
            pass  # the cache is an optimisation only

    @contextmanager
    def lock(self):
        """Exclusive per-environment lock; a lock left by a dead process is reclaimed."""
        self.directory.mkdir(parents=True, exist_ok=True)
        me = f"{socket.gethostname()}:{os.getpid()}"
        for _ in range(2):
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self._reclaim_stale():
                    continue
                holder = self._holder() or "unknown"
                raise StateError(
                    f"environment {self.environment!r} is locked by {holder} ({self.lock_path})"
                ) from None
            with os.fdopen(fd, "w") as fh:
                fh.write(f"{me}\n{time.time()}\n")
            break
        else:
            raise StateError(f"could not acquire {self.lock_path}")
        try:
            yield
        finally:
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass

    def _holder(self) -> str | None:
        try:
            return self.lock_path.read_text().splitlines()[0]
        except (OSError, IndexError):
            return None

    def _reclaim_stale(self) -> bool:
        holder = self._holder()
        if not holder or ":" not in holder:
            return False
        host, _, pid = holder.rpartition(":")
        if host != socket.gethostname() or not pid.isdigit():
            return False
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass
            return True
        except PermissionError:
            return False
        return False
