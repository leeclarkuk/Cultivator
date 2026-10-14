import pytest

from cultivator.errors import GraphError
from cultivator.graph import Graph


def diamond():
    return Graph({"a": set(), "b": {"a"}, "c": {"a"}, "d": {"b", "c"}})


def test_levels_group_parallel_work():
    assert diamond().levels() == [["a"], ["b", "c"], ["d"]]


def test_levels_for_subset_ignore_outside_edges():
    assert diamond().levels({"b", "d"}) == [["b"], ["d"]]
    assert diamond().levels({"b", "c"}) == [["b", "c"]]


def test_ancestors_and_descendants():
    g = diamond()
    assert g.ancestors(["d"]) == {"a", "b", "c"}
    assert g.descendants(["a"]) == {"b", "c", "d"}
    assert g.descendants(["d"]) == set()


def test_cycle_is_reported_with_path():
    g = Graph({"a": {"c"}, "b": {"a"}, "c": {"b"}})
    with pytest.raises(GraphError, match="a -> c -> b -> a"):
        g.validate()


def test_critical_path():
    g = Graph({"a": set(), "b": {"a"}, "c": {"b"}, "x": set()})
    assert g.critical_path() == ["a", "b", "c"]
