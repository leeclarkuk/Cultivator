"""Plain-text rendering for the CLI."""

from __future__ import annotations

import os
import sys
from datetime import datetime

from .executor import Outcome, Status
from .planner import Action, Plan

_CODES = {"red": "31", "green": "32", "yellow": "33", "blue": "34", "dim": "2", "bold": "1"}
_SYMBOLS = {
    Action.CREATE: ("+", "green"),
    Action.UPDATE: ("~", "yellow"),
    Action.DELETE: ("-", "red"),
    Action.NOOP: ("=", "dim"),
}
_TAIL_LINES = 40


class Console:
    def __init__(self, color: bool | None = None, out=None, err=None):
        self.out = out or sys.stdout
        self.err = err or sys.stderr
        if color is None:
            color = self.out.isatty() and "NO_COLOR" not in os.environ
        self.color = color

    def paint(self, text: str, style: str) -> str:
        return f"\033[{_CODES[style]}m{text}\033[0m" if self.color else text

    def say(self, text: str = "") -> None:
        print(text, file=self.out, flush=True)

    def warn(self, text: str) -> None:
        print(self.paint(f"warning: {text}", "yellow"), file=self.err, flush=True)

    def error(self, text: str) -> None:
        print(self.paint(f"error: {text}", "red"), file=self.err, flush=True)

    def stamp(self, text: str) -> None:
        self.say(f"{self.paint(datetime.now().strftime('%H:%M:%S'), 'dim')} {text}")


def render_plan(plan: Plan, console: Console, environment: str, jobs: int,
                verbose: bool = False, commit: str | None = None) -> None:
    head = f"Environment: {environment}"
    if commit:
        head += f"   Commit: {commit}"
    console.say(console.paint(head, "bold"))
    console.say()

    position = {n: k for k, n in enumerate(plan.graph.order())}
    items = sorted(plan.items.values(), key=lambda i: (i.action is Action.NOOP, position.get(i.name, -1), i.name))
    shown = [i for i in items if verbose or i.action is not Action.NOOP]
    width = max((len(i.name) for i in shown), default=0)
    for item in shown:
        symbol, style = _SYMBOLS[item.action]
        reason = "; ".join(item.reasons) if item.action is not Action.NOOP or item.reasons else ""
        console.say(f"  {console.paint(symbol, style)}  {item.name.ljust(width)}  {console.paint(reason, 'dim')}".rstrip())
    if shown:
        console.say()

    counts = plan.counts()
    console.say(
        f"Plan: {counts['create']} to create, {counts['update']} to update, "
        f"{counts['delete']} to delete, {counts['unchanged']} unchanged."
    )
    stages = plan.stages()
    if stages:
        widest = max(len(s) for s in stages)
        console.say(f"Schedule: {len(stages)} stage{'s' if len(stages) != 1 else ''}, "
                    f"up to {min(widest, jobs)} in parallel (jobs={jobs}).")
        if verbose:
            for n, stage in enumerate(stages, 1):
                console.say(f"  stage {n}: {', '.join(stage)}")
    total_modules = counts["create"] + counts["update"] + counts["unchanged"]
    if counts["unchanged"] and total_modules:
        console.say(f"Skipping {counts['unchanged']} of {total_modules} modules (nothing changed).")


def render_outcome(console: Console, outcome: Outcome, verbose: bool) -> None:
    label = {Status.OK: ("ok", "green"), Status.FAILED: ("FAILED", "red"), Status.SKIPPED: ("skipped", "yellow")}
    text, style = label[outcome.status]
    detail = f"{outcome.seconds:.1f}s" if outcome.status is not Status.SKIPPED else outcome.message
    if outcome.status is Status.FAILED:
        detail = f"{outcome.message} after {outcome.seconds:.1f}s"
    console.stamp(f"{console.paint(text.ljust(7), style)} {outcome.name} {console.paint('(' + detail + ')', 'dim')}")
    if outcome.output and (verbose or outcome.status is Status.FAILED):
        lines = outcome.output.rstrip("\n").splitlines()
        if outcome.status is Status.FAILED and not verbose and len(lines) > _TAIL_LINES:
            lines = [f"... {len(lines) - _TAIL_LINES} earlier lines omitted (use -v)"] + lines[-_TAIL_LINES:]
        for line in lines:
            console.say(f"    {console.paint('|', 'dim')} {line}")


def render_summary(console: Console, outcomes: list[Outcome]) -> bool:
    ok = sum(o.status is Status.OK for o in outcomes)
    failed = sum(o.status is Status.FAILED for o in outcomes)
    skipped = sum(o.status is Status.SKIPPED for o in outcomes)
    console.say()
    console.say(f"Applied {ok}, failed {failed}, skipped {skipped}.")
    return failed == 0
