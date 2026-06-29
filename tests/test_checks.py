"""Data-quality checks: compile each kind and run it on a seeded SQLite db."""
import os
import sqlite3
import tempfile

import pytest

from sql_steward.compiler import compile_check
from sql_steward.engine import Engine
from sql_steward.semantic import SemanticError, SemanticLayer

LAYER = {
    "dialect": "sqlite",
    "entities": {
        "customers": {
            "table": "customers",
            "primary_key": "id",
            "fields": {
                "id": {"type": "int"},
                "email": {"type": "text", "pii": "EMAIL_ADDRESS"},
                "country": {"type": "text"},
                "age": {"type": "int"},
            },
        }
    },
    "checks": {
        "email_present": {"entity": "customers", "kind": "not_null", "field": "email"},
        "id_unique": {"entity": "customers", "kind": "unique", "field": "id"},
        "age_range": {"entity": "customers", "kind": "range", "field": "age", "min": 0, "max": 120},
        "country_known": {"entity": "customers", "kind": "accepted_values", "field": "country", "values": ["UK", "US"]},
        "has_rows": {"entity": "customers", "kind": "row_count_min", "min": 3},
    },
}


@pytest.fixture
def env():
    tmp = tempfile.mkdtemp(prefix="steward-checks-")
    db = os.path.join(tmp, "d.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE customers (id INTEGER, email TEXT, country TEXT, age INTEGER)")
    con.executemany(
        "INSERT INTO customers VALUES (?,?,?,?)",
        [
            (1, "a@example.com", "UK", 30),
            (2, None, "US", 40),         # null email -> not_null violation
            (1, "c@example.com", "FR", 200),  # duplicate id, unknown country, age out of range
        ],
    )
    con.commit()
    con.close()
    return SemanticLayer.from_dict(LAYER), Engine(f"sqlite:///{db}")


def _violations(engine, layer, name):
    rows = engine.run(compile_check(layer, layer.checks[name]))
    return int(next(iter(rows[0].values())))


def test_not_null(env):
    layer, eng = env
    assert _violations(eng, layer, "email_present") == 1


def test_unique(env):
    layer, eng = env
    assert _violations(eng, layer, "id_unique") == 1  # id=1 appears twice


def test_range(env):
    layer, eng = env
    assert _violations(eng, layer, "age_range") == 1  # age 200


def test_accepted_values(env):
    layer, eng = env
    assert _violations(eng, layer, "country_known") == 1  # FR


def test_row_count_min_passes(env):
    layer, eng = env
    assert _violations(eng, layer, "has_rows") == 0  # 3 rows >= 3


def test_unsupported_kind_rejected():
    bad = dict(LAYER)
    bad["checks"] = {"x": {"entity": "customers", "kind": "bogus", "field": "id"}}
    with pytest.raises(SemanticError):
        SemanticLayer.from_dict(bad)


def test_check_field_must_exist():
    bad = dict(LAYER)
    bad["checks"] = {"x": {"entity": "customers", "kind": "not_null", "field": "nope"}}
    with pytest.raises(SemanticError):
        SemanticLayer.from_dict(bad)
