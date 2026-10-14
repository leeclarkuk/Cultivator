"""Loading and validating declarative manifests.

A workspace is described by a root ``cultivator.yaml`` and one ``module.yaml``
per module. Manifests are resolved for a single environment: variables are
interpolated and per-environment overrides applied, so everything downstream
(fingerprinting, planning, execution) sees concrete values only.
"""

from __future__ import annotations

import glob
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Union

import yaml

from .errors import ManifestError

API_VERSION = "cultivator/v1"
CONFIG_FILE = "cultivator.yaml"
MODULE_FILE = "module.yaml"
DEFAULT_STATE_DIR = ".cultivator/state"

# A command is either a shell string or an argv tuple executed directly.
Command = Union[str, tuple]

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_VAR_RE = re.compile(r"\$\{(vars|inputs|module|environment)(?:\.([A-Za-z0-9_.-]+))?\}")

_CONFIG_KEYS = {"modules", "defaultEnvironment", "environments", "vars", "parallelism", "stateDir"}
_ENV_KEYS = {"vars"}
_MODULE_KEYS = {
    "dependsOn", "watch", "ignore", "inputs", "env",
    "apply", "verify", "check", "destroy",
    "timeout", "retries", "overrides",
}
_OVERRIDE_KEYS = {"inputs", "env"}


@dataclass(frozen=True)
class Dependency:
    name: str
    propagate: bool = True


@dataclass(frozen=True)
class Module:
    name: str
    path: Path
    manifest: Path
    description: str
    depends_on: tuple
    watch: tuple
    ignore: tuple
    inputs: dict
    env: dict
    apply: tuple
    verify: tuple
    check: tuple
    destroy: tuple
    timeout: float | None
    retries: int

    @property
    def dependency_names(self) -> list[str]:
        return [d.name for d in self.depends_on]

    def propagating(self) -> list[str]:
        return [d.name for d in self.depends_on if d.propagate]


@dataclass(frozen=True)
class Config:
    root: Path
    module_globs: tuple
    environments: dict
    default_environment: str | None
    global_vars: dict
    parallelism: int
    state_dir: Path


@dataclass
class Workspace:
    config: Config
    environment: str
    vars: dict
    modules: dict = field(default_factory=dict)

    @property
    def root(self) -> Path:
        return self.config.root


def find_root(start: Path) -> Path:
    start = start.resolve()
    for candidate in (start, *start.parents):
        if (candidate / CONFIG_FILE).is_file():
            return candidate
    raise ManifestError(f"no {CONFIG_FILE} found in {start} or any parent directory")


def _read_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ManifestError(f"{path}: invalid YAML: {exc}") from exc
    except OSError as exc:
        raise ManifestError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError(f"{path}: expected a mapping at the top level")
    return data


def _check_header(data: dict, kind: str, path: Path) -> None:
    if data.get("apiVersion") != API_VERSION:
        raise ManifestError(f"{path}: apiVersion must be {API_VERSION!r}")
    if data.get("kind") != kind:
        raise ManifestError(f"{path}: kind must be {kind!r}")


def _unknown(keys, allowed, where: str, path: Path) -> None:
    extra = sorted(set(keys) - allowed)
    if extra:
        raise ManifestError(f"{path}: unknown field(s) in {where}: {', '.join(map(str, extra))}")


def _mapping(value: Any, where: str, path: Path) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ManifestError(f"{path}: {where} must be a mapping")
    return value


def _str_list(value: Any, where: str, path: Path) -> tuple:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ManifestError(f"{path}: {where} must be a list of strings")
    return tuple(value)


def _commands(value: Any, where: str, path: Path) -> tuple:
    """A string or list of commands. A nested list is an argv executed without a shell."""
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, list):
        raise ManifestError(f"{path}: {where} must be a string or a list of commands")
    out = []
    for item in value:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, list) and item and all(isinstance(a, (str, int, float)) for a in item):
            out.append(tuple(str(a) for a in item))
        else:
            raise ManifestError(f"{path}: {where} entries must be strings or argv lists")
    return tuple(out)


def _dependencies(value: Any, path: Path) -> tuple:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ManifestError(f"{path}: dependsOn must be a list")
    deps = []
    for item in value:
        if isinstance(item, str):
            deps.append(Dependency(item))
        elif isinstance(item, dict) and set(item) <= {"name", "propagate"} and "name" in item:
            propagate = item.get("propagate", True)
            if not isinstance(propagate, bool):
                raise ManifestError(f"{path}: dependsOn.propagate must be true or false")
            deps.append(Dependency(str(item["name"]), propagate))
        else:
            raise ManifestError(f"{path}: dependsOn entries must be a name or {{name, propagate}}")
    names = [d.name for d in deps]
    if len(set(names)) != len(names):
        raise ManifestError(f"{path}: dependsOn lists a module more than once")
    return tuple(deps)


def _lookup(source: dict, dotted: str):
    node: Any = source
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(dotted)
        node = node[part]
    return node


def interpolate(value: Any, context: dict, where: str):
    """Expand ``${vars.x}``, ``${inputs.x}``, ``${module.name}`` and ``${environment}``.

    Other ``${...}`` forms are left alone so shell expansions keep working.
    """
    if isinstance(value, str):
        def sub(match: re.Match) -> str:
            namespace, key = match.group(1), match.group(2)
            if namespace == "environment":
                return str(context["environment"])
            try:
                if key is None:
                    raise KeyError(namespace)
                found = _lookup(context[namespace], key)
            except KeyError:
                raise ManifestError(f"{where}: undefined reference {match.group(0)}") from None
            if isinstance(found, (dict, list)):
                raise ManifestError(f"{where}: {match.group(0)} is not a scalar")
            return str(found)

        return _VAR_RE.sub(sub, value)
    if isinstance(value, list):
        return [interpolate(v, context, where) for v in value]
    if isinstance(value, tuple):
        return tuple(interpolate(v, context, where) for v in value)
    if isinstance(value, dict):
        return {k: interpolate(v, context, where) for k, v in value.items()}
    return value


def load_config(root: Path) -> Config:
    path = root / CONFIG_FILE
    data = _read_yaml(path)
    _check_header(data, "Config", path)
    spec = _mapping(data.get("spec"), "spec", path)
    _unknown(spec, _CONFIG_KEYS, "spec", path)

    environments = _mapping(spec.get("environments"), "environments", path)
    for env_name, env in environments.items():
        env = _mapping(env, f"environments.{env_name}", path)
        _unknown(env, _ENV_KEYS, f"environments.{env_name}", path)
        _mapping(env.get("vars"), f"environments.{env_name}.vars", path)

    default = spec.get("defaultEnvironment")
    if default is not None and default not in environments:
        raise ManifestError(f"{path}: defaultEnvironment {default!r} is not defined")

    parallelism = spec.get("parallelism", 4)
    if not isinstance(parallelism, int) or isinstance(parallelism, bool) or parallelism < 1:
        raise ManifestError(f"{path}: parallelism must be a positive integer")

    return Config(
        root=root,
        module_globs=_str_list(spec.get("modules", ["modules/*"]), "modules", path),
        environments=environments,
        default_environment=default,
        global_vars=_mapping(spec.get("vars"), "vars", path),
        parallelism=parallelism,
        state_dir=root / str(spec.get("stateDir", DEFAULT_STATE_DIR)),
    )


def discover_manifests(config: Config) -> list[Path]:
    found: set[Path] = set()
    for pattern in config.module_globs:
        for hit in glob.glob(str(config.root / pattern), recursive=True):
            hit_path = Path(hit)
            manifest = hit_path / MODULE_FILE if hit_path.is_dir() else hit_path
            if manifest.name == MODULE_FILE and manifest.is_file():
                found.add(manifest.resolve())
    return sorted(found)


def _merge_overrides(spec: dict, environment: str, path: Path) -> tuple[dict, dict]:
    inputs = dict(_mapping(spec.get("inputs"), "inputs", path))
    env = dict(_mapping(spec.get("env"), "env", path))
    overrides = _mapping(spec.get("overrides"), "overrides", path)
    chosen = _mapping(overrides.get(environment), f"overrides.{environment}", path)
    _unknown(chosen, _OVERRIDE_KEYS, f"overrides.{environment}", path)
    inputs.update(_mapping(chosen.get("inputs"), "overrides.inputs", path))
    env.update(_mapping(chosen.get("env"), "overrides.env", path))
    return inputs, env


def load_module(manifest: Path, workspace_vars: dict, environment: str) -> Module:
    data = _read_yaml(manifest)
    _check_header(data, "Module", manifest)
    meta = _mapping(data.get("metadata"), "metadata", manifest)
    name = meta.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ManifestError(f"{manifest}: metadata.name must match {_NAME_RE.pattern}")
    spec = _mapping(data.get("spec"), "spec", manifest)
    _unknown(spec, _MODULE_KEYS, "spec", manifest)

    inputs, env = _merge_overrides(spec, environment, manifest)
    where = f"{manifest} ({name})"
    base = {"vars": workspace_vars, "environment": environment,
            "module": {"name": name, "path": str(manifest.parent)}}
    inputs = interpolate(inputs, base, where)
    context = {**base, "inputs": inputs}

    timeout = spec.get("timeout")
    if timeout is not None and (not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0):
        raise ManifestError(f"{manifest}: timeout must be a positive number of seconds")
    retries = spec.get("retries", 0)
    if not isinstance(retries, int) or isinstance(retries, bool) or retries < 0:
        raise ManifestError(f"{manifest}: retries must be zero or a positive integer")

    def cmds(key: str) -> tuple:
        return interpolate(_commands(spec.get(key), key, manifest), context, where)

    return Module(
        name=name,
        path=manifest.parent,
        manifest=manifest,
        description=str(meta.get("description", "")),
        depends_on=_dependencies(spec.get("dependsOn"), manifest),
        watch=_str_list(spec.get("watch", ["**"]), "watch", manifest),
        ignore=_str_list(spec.get("ignore"), "ignore", manifest),
        inputs=inputs,
        env={str(k): str(v) for k, v in interpolate(env, context, where).items()},
        apply=cmds("apply"),
        verify=cmds("verify"),
        check=cmds("check"),
        destroy=cmds("destroy"),
        timeout=float(timeout) if timeout is not None else None,
        retries=retries,
    )


def pick_environment(config: Config, requested: str | None) -> str:
    if requested:
        if config.environments and requested not in config.environments:
            known = ", ".join(sorted(config.environments))
            raise ManifestError(f"unknown environment {requested!r} (defined: {known})")
        return requested
    if config.default_environment:
        return config.default_environment
    if len(config.environments) == 1:
        return next(iter(config.environments))
    return "default"


def load_workspace(root: Path, environment: str | None = None) -> Workspace:
    config = load_config(root)
    env_name = pick_environment(config, environment)
    env_vars = _mapping(config.environments.get(env_name, {}).get("vars"), "vars", root / CONFIG_FILE)
    merged = {**config.global_vars, **env_vars}

    modules: dict[str, Module] = {}
    for manifest in discover_manifests(config):
        module = load_module(manifest, merged, env_name)
        if module.name in modules:
            raise ManifestError(
                f"duplicate module name {module.name!r}: {modules[module.name].manifest} and {manifest}"
            )
        modules[module.name] = module

    for module in modules.values():
        for dep in module.dependency_names:
            if dep == module.name:
                raise ManifestError(f"{module.manifest}: module depends on itself")
            if dep not in modules:
                raise ManifestError(f"{module.manifest}: {module.name} depends on unknown module {dep!r}")

    return Workspace(config=config, environment=env_name, vars=merged,
                     modules=dict(sorted(modules.items())))
