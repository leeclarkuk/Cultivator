"""Dependency graph operations."""

from __future__ import annotations

from .errors import GraphError


class Graph:
    """Directed graph where ``edges[n]`` is the set of nodes ``n`` depends on."""

    def __init__(self, edges: dict[str, set[str]]):
        self.edges = {n: set(d) for n, d in edges.items()}
        self._reverse: dict[str, set[str]] = {n: set() for n in self.edges}
        for node, deps in self.edges.items():
            for dep in deps:
                self._reverse.setdefault(dep, set()).add(node)

    def find_cycle(self) -> list[str] | None:
        state: dict[str, int] = {}
        stack: list[str] = []

        def visit(node: str) -> list[str] | None:
            state[node] = 1
            stack.append(node)
            for dep in sorted(self.edges.get(node, ())):
                if state.get(dep) == 1:
                    return stack[stack.index(dep):] + [dep]
                if dep not in state:
                    found = visit(dep)
                    if found:
                        return found
            stack.pop()
            state[node] = 2
            return None

        for node in sorted(self.edges):
            if node not in state:
                found = visit(node)
                if found:
                    return found
        return None

    def validate(self) -> None:
        cycle = self.find_cycle()
        if cycle:
            raise GraphError("dependency cycle: " + " -> ".join(cycle))

    def levels(self, subset: set[str] | None = None) -> list[list[str]]:
        """Group nodes into stages; every node's dependencies sit in earlier stages.

        With ``subset`` only edges between members are considered, which is what
        the executor needs when most of the graph is unchanged.
        """
        self.validate()
        nodes = set(self.edges) if subset is None else set(subset)
        remaining = {n: self.edges.get(n, set()) & nodes for n in nodes}
        stages: list[list[str]] = []
        while remaining:
            ready = sorted(n for n, deps in remaining.items() if not deps)
            stages.append(ready)
            for n in ready:
                del remaining[n]
            for deps in remaining.values():
                deps.difference_update(ready)
        return stages

    def order(self, subset: set[str] | None = None) -> list[str]:
        return [n for stage in self.levels(subset) for n in stage]

    def ancestors(self, names) -> set[str]:
        return self._walk(names, self.edges)

    def descendants(self, names) -> set[str]:
        return self._walk(names, self._reverse)

    @staticmethod
    def _walk(names, adjacency: dict[str, set[str]]) -> set[str]:
        seen: set[str] = set()
        todo = list(names)
        while todo:
            for nxt in adjacency.get(todo.pop(), ()):
                if nxt not in seen:
                    seen.add(nxt)
                    todo.append(nxt)
        return seen

    def critical_path(self, weights: dict[str, float] | None = None) -> list[str]:
        """Longest chain by node weight (default 1 each)."""
        weights = weights or {}
        best: dict[str, tuple[float, list[str]]] = {}
        for node in self.order():
            prior = max((best[d] for d in self.edges[node]), default=(0.0, []), key=lambda b: b[0])
            best[node] = (prior[0] + weights.get(node, 1.0), prior[1] + [node])
        return max(best.values(), default=(0.0, []), key=lambda b: b[0])[1]
