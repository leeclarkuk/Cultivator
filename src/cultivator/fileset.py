"""Watched-file matching and content hashing.

Patterns are glob-style, relative to the module directory, with ``**`` spanning
directories. A pattern that matches a directory covers everything beneath it.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import time
from pathlib import Path

ALWAYS_EXCLUDED = {".git", ".cultivator", "__pycache__", ".DS_Store"}
_GLOB_CHARS = set("*?[")
# Files modified this recently are not cached: a later edit within the same
# timestamp tick would otherwise go unnoticed.
_RACY_NS = 2_000_000_000


def glob_to_regex(pattern: str) -> re.Pattern:
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern.startswith("**", i):
                j = i + 2
                if j < n and pattern[j] == "/":
                    out.append("(?:.*/)?")
                    i = j + 1
                else:
                    out.append(".*")
                    i = j
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[" and (j := pattern.find("]", i + 2)) != -1:
            body = pattern[i + 1:j].replace("\\", "\\\\")
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append(f"[{body}]")
            i = j
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out))


def _ancestors(rel: str):
    parts = rel.split("/")
    for i in range(1, len(parts) + 1):
        yield "/".join(parts[:i])


class FileSet:
    def __init__(self, base: Path, watch, ignore=()):
        self.base = base
        self.watch = [posixpath.normpath(p) for p in watch]
        # (regex, may_leave_base): only patterns that start with ".." may match outside the module
        self._include = [(glob_to_regex(p), p.startswith("..")) for p in self.watch]
        self._ignore = [glob_to_regex(posixpath.normpath(p)) for p in ignore]

    def _rel(self, path: Path) -> str:
        return Path(os.path.normpath(os.path.relpath(path, self.base))).as_posix()

    def includes(self, path: Path) -> bool:
        rel = self._rel(path)
        if any(p in ALWAYS_EXCLUDED for p in rel.split("/")):
            return False
        outside = rel == ".." or rel.startswith("../")
        chain = list(_ancestors(rel))
        if not any(rx.fullmatch(a) for rx, leaves in self._include if leaves or not outside for a in chain):
            return False
        return not any(rx.fullmatch(a) for rx in self._ignore for a in chain)

    def _roots(self):
        for pattern in self.watch:
            static = []
            for part in pattern.split("/"):
                if _GLOB_CHARS & set(part):
                    break
                static.append(part)
            yield Path(os.path.normpath(self.base.joinpath(*static)))

    def files(self) -> list[Path]:
        seen: set[Path] = set()
        for root in dict.fromkeys(self._roots()):
            if root.is_file():
                candidates = [root]
            elif root.is_dir():
                candidates = []
                for dirpath, dirnames, filenames in os.walk(root):
                    dirnames[:] = sorted(d for d in dirnames if d not in ALWAYS_EXCLUDED)
                    candidates.extend(Path(dirpath, f) for f in filenames)
            else:
                continue
            seen.update(p for p in candidates if self.includes(p))
        return sorted(seen)


class FileHasher:
    """Content hashing with an mtime/size cache so unchanged trees are cheap."""

    def __init__(self, cache: dict | None = None):
        self.cache: dict = cache if cache is not None else {}
        self.hits = 0
        self.misses = 0

    def digest(self, path: Path) -> str:
        key = str(path)
        try:
            st = path.stat()
        except OSError:
            return "missing"
        entry = self.cache.get(key)
        if entry and entry[0] == st.st_mtime_ns and entry[1] == st.st_size:
            self.hits += 1
            return entry[2]
        self.misses += 1
        h = hashlib.sha256()
        try:
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
        except OSError:
            return "unreadable"
        value = h.hexdigest()
        if time.time_ns() - st.st_mtime_ns > _RACY_NS:
            self.cache[key] = [st.st_mtime_ns, st.st_size, value]
        return value


def canonical_digest(obj) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=list)
    return hashlib.sha256(blob.encode()).hexdigest()
