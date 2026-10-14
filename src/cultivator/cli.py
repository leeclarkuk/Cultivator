"""Command line interface."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import __version__, gitutil
from .engine import Engine
from .errors import CultivatorError
from .executor import Status
from .manifest import API_VERSION, CONFIG_FILE, find_root, load_workspace
from .planner import build_graph
from .ui import Console, render_outcome, render_plan, render_summary

_SCAFFOLD_CONFIG = f"""\
apiVersion: {API_VERSION}
kind: Config
spec:
  modules:
    - modules/*
  defaultEnvironment: dev
  parallelism: 4
  environments:
    dev:
      vars:
        greeting: hello
"""

_SCAFFOLD_MODULE = f"""\
apiVersion: {API_VERSION}
kind: Module
metadata:
  name: hello
  description: Example module
spec:
  watch:
    - "**"
  inputs:
    greeting: ${{vars.greeting}}
  apply:
    - echo "${{inputs.greeting}} from ${{module.name}} in ${{environment}}"
"""


def _add_planning_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("-t", "--target", action="append", default=[], metavar="MODULE",
                   help="only this module and what it depends on (repeatable)")
    p.add_argument("--since", metavar="REF",
                   help="stateless mode: treat modules with files changed since this git ref as changed")
    p.add_argument("--refresh", action="store_true",
                   help="run each module's check command and treat failures as drift")
    p.add_argument("--force", action="append", default=[], metavar="MODULE",
                   help="redeploy this module even if unchanged (repeatable)")
    p.add_argument("--all", dest="everything", action="store_true", help="redeploy every module")
    p.add_argument("--no-prune", dest="prune", action="store_false",
                   help="do not destroy modules that were removed from the manifests")


def _add_apply_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("-j", "--jobs", type=int, metavar="N", help="parallel modules (default: from config)")
    p.add_argument("--keep-going", action="store_true",
                   help="after a failure, keep deploying branches that do not depend on it")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cultivator", description="GitOps-style module orchestration.")
    parser.add_argument("--version", action="version", version=f"cultivator {__version__}")
    parser.add_argument("-C", "--root", metavar="DIR", help="workspace directory (default: search upwards)")
    parser.add_argument("--no-color", action="store_true", help="disable coloured output")

    # Accepted after the subcommand too; SUPPRESS keeps a value given before it.
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("-C", "--root", metavar="DIR", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    shared.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    common = argparse.ArgumentParser(add_help=False, parents=[shared])
    common.add_argument("-e", "--env", metavar="NAME", help="environment (default: from config)")
    common.add_argument("-v", "--verbose", action="store_true", help="show unchanged modules and command output")

    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    p = sub.add_parser("init", parents=[shared], help="scaffold a new workspace")
    p.add_argument("directory", nargs="?", default=".")

    sub.add_parser("validate", parents=[common], help="check manifests and the dependency graph")

    p = sub.add_parser("graph", parents=[common], help="show the dependency graph")
    p.add_argument("--format", choices=["text", "dot", "json"], default="text")

    p = sub.add_parser("plan", parents=[common], help="show what would change")
    _add_planning_options(p)
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.add_argument("--detailed-exitcode", action="store_true",
                   help="exit 2 when there are changes (for CI)")

    p = sub.add_parser("apply", parents=[common], help="deploy what changed")
    _add_planning_options(p)
    _add_apply_options(p)
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    p.add_argument("--dry-run", action="store_true", help="show the plan and stop")

    p = sub.add_parser("watch", parents=[common], help="reconcile continuously, optionally pulling from git")
    _add_planning_options(p)
    _add_apply_options(p)
    p.add_argument("--interval", type=float, default=60.0, metavar="SECONDS")
    p.add_argument("--pull", action="store_true", help="git pull --ff-only before each pass")
    p.add_argument("--once", action="store_true", help="run a single pass and exit")
    return parser


def _planning_kwargs(args) -> dict:
    return dict(targets=args.target, since=args.since, refresh=args.refresh,
                force=args.force, everything=args.everything, prune=args.prune)


def _root(args) -> Path:
    return find_root(Path(getattr(args, "root", None) or "."))


def cmd_init(args, console: Console) -> int:
    base = Path(args.directory).resolve()
    config = base / CONFIG_FILE
    if config.exists():
        raise CultivatorError(f"{config} already exists")
    module_dir = base / "modules" / "hello"
    module_dir.mkdir(parents=True, exist_ok=True)
    config.write_text(_SCAFFOLD_CONFIG, encoding="utf-8")
    (module_dir / "module.yaml").write_text(_SCAFFOLD_MODULE, encoding="utf-8")
    ignore = base / ".gitignore"
    existing = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
    if ".cultivator/" not in existing.splitlines():
        ignore.write_text(existing + ("" if not existing or existing.endswith("\n") else "\n") + ".cultivator/\n",
                          encoding="utf-8")
    console.say(f"Created {config.relative_to(Path.cwd()) if config.is_relative_to(Path.cwd()) else config}")
    console.say("Next: cultivator plan, then cultivator apply")
    return 0


def cmd_validate(args, console: Console) -> int:
    ws = load_workspace(_root(args), args.env)
    build_graph(ws)
    console.say(f"OK: {len(ws.modules)} modules, environment {ws.environment!r}.")
    return 0


def cmd_graph(args, console: Console) -> int:
    ws = load_workspace(_root(args), args.env)
    graph = build_graph(ws)
    if args.format == "json":
        console.say(json.dumps({n: sorted(d) for n, d in sorted(graph.edges.items())}, indent=2))
    elif args.format == "dot":
        console.say("digraph cultivator {")
        console.say("  rankdir=LR;")
        for name, module in ws.modules.items():
            console.say(f'  "{name}";')
            for dep in module.depends_on:
                style = "" if dep.propagate else ' [style=dashed]'
                console.say(f'  "{dep.name}" -> "{name}"{style};')
        console.say("}")
    else:
        for number, stage in enumerate(graph.levels(), 1):
            console.say(f"stage {number}")
            for name in stage:
                deps = ws.modules[name].depends_on
                needs = ", ".join(d.name + ("" if d.propagate else " (no propagate)") for d in deps)
                console.say(f"  {name}" + (f"  <- {needs}" if needs else ""))
        path = graph.critical_path()
        console.say()
        console.say("Longest chain: " + " -> ".join(path))
    return 0


def cmd_plan(args, console: Console) -> int:
    engine = Engine(_root(args), args.env)
    plan = engine.plan(**_planning_kwargs(args))
    if args.format == "json":
        console.say(json.dumps({"environment": engine.ws.environment, "commit": engine.commit, **plan.to_dict()}, indent=2))
    else:
        render_plan(plan, console, engine.ws.environment, engine.jobs, args.verbose, engine.commit)
    return 2 if args.detailed_exitcode and plan.has_changes else 0


def _confirm(console: Console) -> bool:
    if not sys.stdin.isatty():
        raise CultivatorError("refusing to apply without confirmation on a non-interactive terminal; pass --yes")
    return input("Apply these changes? [y/N] ").strip().lower() in {"y", "yes"}


def _reconcile(engine: Engine, args, console: Console, assume_yes: bool) -> bool:
    plan = engine.plan(**_planning_kwargs(args))
    render_plan(plan, console, engine.ws.environment, args.jobs or engine.jobs, args.verbose, engine.commit)
    if not plan.has_changes:
        console.say("Nothing to do.")
        return True
    if getattr(args, "dry_run", False):
        return True
    if not assume_yes and not _confirm(console):
        console.say("Cancelled.")
        return True
    console.say()
    outcomes = engine.apply(
        plan, jobs=args.jobs, keep_going=args.keep_going,
        on_start=lambda name, action: console.stamp(f"{console.paint(action.ljust(7), 'blue')} {name}"),
        on_finish=lambda o: render_outcome(console, o, args.verbose),
    )
    return render_summary(console, outcomes)


def cmd_apply(args, console: Console) -> int:
    engine = Engine(_root(args), args.env)
    with engine.lock():
        return 0 if _reconcile(engine, args, console, args.yes) else 1


def cmd_watch(args, console: Console) -> int:
    root = _root(args)
    ok = True
    while True:
        try:
            if args.pull:
                gitutil.pull(root)
            engine = Engine(root, args.env)  # reloaded each pass so manifest edits take effect
            with engine.lock():
                ok = _reconcile(engine, args, console, assume_yes=True)
        except CultivatorError as exc:
            console.error(str(exc))
            ok = False
        if args.once:
            return 0 if ok else 1
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0


_COMMANDS = {
    "init": cmd_init,
    "validate": cmd_validate,
    "graph": cmd_graph,
    "plan": cmd_plan,
    "apply": cmd_apply,
    "watch": cmd_watch,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    console = Console(color=False if getattr(args, "no_color", False) else None)
    try:
        return _COMMANDS[args.command](args, console)
    except CultivatorError as exc:
        console.error(str(exc))
        return 1
    except BrokenPipeError:
        sys.stderr.close()  # downstream reader (e.g. head) went away; nothing more to say
        return 0
    except KeyboardInterrupt:
        console.error("interrupted")
        return 130
