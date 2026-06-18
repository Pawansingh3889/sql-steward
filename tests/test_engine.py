"""End-to-end: compile from the layer, execute on a seeded SQLite db."""
import os
import tempfile

import pytest

from sql_steward.cli import DEMO_LAYER, _seed_demo_db
from sql_steward.compiler import compile_metric, compile_records
from sql_steward.engine import Engine
from sql_steward.semantic import SemanticLayer


@pytest.fixture
def env():
    tmp = tempfile.mkdtemp(prefix="steward-test-")
    db = os.path.join(tmp, "d.db")
    _seed_demo_db(db)
    return SemanticLayer.from_dict(DEMO_LAYER), Engine(f"sqlite:///{db}")


def test_metric_sum_by_plan(env):
    layer, engine = env
    rows = engine.run(compile_metric(layer, "mrr_total", dimensions=["plan"]))
    by = {r["plan"]: r["mrr_total"] for r in rows}
    assert by["pro"] == 297.0      # 3 pro subs at 99
    assert by["team"] == 598.0     # 2 team subs at 299


def test_metric_join_by_country(env):
    layer, engine = env
    rows = engine.run(compile_metric(layer, "mrr_total", dimensions=["customers.country"]))
    by = {r["country"]: r["mrr_total"] for r in rows}
    assert by["UK"] == 398.0       # 99 + 299
    assert by["US"] == 398.0       # 99 + 299
    assert by["NL"] == 99.0


def test_records_filter_binds_and_returns(env):
    layer, engine = env
    rows = engine.run(
        compile_records(
            layer, "customers", fields=["id", "country"],
            filters=[{"field": "country", "op": "=", "value": "UK"}],
        )
    )
    assert {r["id"] for r in rows} == {1, 2}


def test_metric_filtered_by_joined_entity(env):
    layer, engine = env
    rows = engine.run(
        compile_metric(
            layer, "mrr_total", dimensions=["plan"],
            filters=[{"field": "customers.country", "op": "=", "value": "US"}],
        )
    )
    by = {r["plan"]: r["mrr_total"] for r in rows}
    assert by == {"pro": 99.0, "team": 299.0}  # only US customers (grace, margaret)
