import pytest
import yaml

from cultivator.errors import ManifestError
from cultivator.manifest import find_root, interpolate, load_workspace


def test_loads_modules_and_dependencies(chain):
    loaded = load_workspace(chain.root)
    assert list(loaded.modules) == ["api", "database", "network", "web"]
    assert loaded.modules["web"].dependency_names == ["api"]


def test_find_root_searches_upwards(chain):
    nested = chain.root / "modules" / "api" / "src"
    assert find_root(nested) == chain.root


def test_find_root_fails_cleanly(tmp_path):
    with pytest.raises(ManifestError, match="no cultivator.yaml"):
        find_root(tmp_path)


def test_interpolation_and_environment_overrides(ws):
    ws.write_config(
        vars={"site": "primary"},
        environments={"dev": {"vars": {"size": "small"}}, "prod": {"vars": {"size": "large"}}},
    )
    ws.module(
        "app",
        inputs={"size": "${vars.size}", "site": "${vars.site}"},
        apply='deploy ${inputs.size} ${module.name} ${environment} "${HOME}"',
        overrides={"prod": {"inputs": {"size": "xlarge"}, "env": {"MODE": "${environment}"}}},
    )
    dev = load_workspace(ws.root, "dev").modules["app"]
    prod = load_workspace(ws.root, "prod").modules["app"]
    assert dev.apply == ('deploy small app dev "${HOME}"',)
    assert prod.inputs["size"] == "xlarge" and prod.inputs["site"] == "primary"
    assert prod.env == {"MODE": "prod"}


def test_unknown_environment_is_rejected(ws):
    ws.write_config(environments={"dev": {}})
    with pytest.raises(ManifestError, match="unknown environment 'qa'"):
        load_workspace(ws.root, "qa")


def test_undefined_reference_names_the_module(ws):
    ws.module("app", apply="echo ${vars.nope}")
    with pytest.raises(ManifestError, match=r"undefined reference \$\{vars.nope\}"):
        load_workspace(ws.root)


def test_unknown_fields_are_errors(ws):
    ws.module("app", aply="typo")
    with pytest.raises(ManifestError, match="unknown field"):
        load_workspace(ws.root)


def test_unknown_dependency(ws):
    ws.module("app", depends_on=["ghost"])
    with pytest.raises(ManifestError, match="unknown module 'ghost'"):
        load_workspace(ws.root)


def test_duplicate_module_names(ws):
    ws.module("one")
    other = ws.module("two")
    doc = yaml.safe_load((other / "module.yaml").read_text())
    doc["metadata"]["name"] = "one"
    (other / "module.yaml").write_text(yaml.safe_dump(doc))
    with pytest.raises(ManifestError, match="duplicate module name"):
        load_workspace(ws.root)


def test_argv_commands_and_propagate_flag(ws):
    ws.module("app", apply=[["echo", "hi"], "echo there"],
              depends_on=[{"name": "base", "propagate": False}])
    ws.module("base")
    app = load_workspace(ws.root).modules["app"]
    assert app.apply == (("echo", "hi"), "echo there")
    assert app.propagating() == []


def test_bad_values(ws):
    ws.module("app", retries=-1)
    with pytest.raises(ManifestError, match="retries"):
        load_workspace(ws.root)


def test_interpolate_leaves_shell_expansions():
    assert interpolate("${FOO} $BAR ${vars.x}", {"vars": {"x": 1}, "environment": "e"}, "t") == "${FOO} $BAR 1"
