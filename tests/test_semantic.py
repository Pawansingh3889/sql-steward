"""Tests for loading and validating the semantic layer."""
import copy

import pytest

from sql_steward.semantic import SemanticError, SemanticLayer

BASE = {
    "dialect": "postgres",
    "entities": {
        "orders": {
            "table": "orders",
            "fields": {"id": {"type": "int"}, "total": {"type": "numeric"}},
        }
    },
    "metrics": {
        "revenue": {"entity": "orders", "aggregate": "sum", "field": "total"}
    },
    "policy": {"block_pii": ["EMAIL_ADDRESS"]},
}


def test_loads_and_resolves():
    layer = SemanticLayer.from_dict(BASE)
    assert layer.dialect == "postgres"
    assert "orders" in layer.entities
    ent, fdef = layer.resolve_ref("orders.total", "orders")
    assert ent == "orders" and fdef.name == "total"


def test_requires_dialect():
    d = copy.deepcopy(BASE)
    del d["dialect"]
    with pytest.raises(SemanticError):
        SemanticLayer.from_dict(d)


def test_unknown_aggregate_rejected():
    d = copy.deepcopy(BASE)
    d["metrics"]["revenue"]["aggregate"] = "median"
    with pytest.raises(SemanticError):
        SemanticLayer.from_dict(d)


def test_metric_field_must_exist():
    d = copy.deepcopy(BASE)
    d["metrics"]["revenue"]["field"] = "does_not_exist"
    with pytest.raises(SemanticError):
        SemanticLayer.from_dict(d)


def test_join_must_reference_real_entities():
    d = copy.deepcopy(BASE)
    d["joins"] = [{"left": "orders", "right": "ghost", "on": "orders.x = ghost.y"}]
    with pytest.raises(SemanticError):
        SemanticLayer.from_dict(d)


def test_yaml_on_keyword_is_tolerated():
    # PyYAML parses a bare `on:` key as boolean True; the loader must cope.
    import yaml

    text = """
dialect: sqlite
entities:
  a: {table: a, fields: {id: {type: int}}}
  b: {table: b, fields: {a_id: {type: int}}}
joins:
  - left: b
    right: a
    on: b.a_id = a.id
"""
    layer = SemanticLayer.from_dict(yaml.safe_load(text))
    join = layer.find_join("a", "b")
    assert join is not None and join.on == "b.a_id = a.id"


def test_count_star_metric_allowed():
    d = copy.deepcopy(BASE)
    d["metrics"]["n"] = {"entity": "orders", "aggregate": "count", "field": "*"}
    layer = SemanticLayer.from_dict(d)
    assert layer.get_metric("n").field == "*"
