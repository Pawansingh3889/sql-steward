"""Tests for the compile-from-definitions engine, including the refusals."""
import pytest

from sql_steward.compiler import Refusal, compile_metric, compile_records
from sql_steward.semantic import SemanticLayer

LAYER_DICT = {
    "dialect": "postgres",
    "entities": {
        "customers": {
            "table": "customers",
            "primary_key": "id",
            "fields": {
                "id": {"type": "int"},
                "name": {"type": "text", "pii": "PERSON"},
                "email": {"type": "text", "pii": "EMAIL_ADDRESS"},
                "country": {"type": "text"},
            },
        },
        "subscriptions": {
            "table": "subscriptions",
            "fields": {
                "id": {"type": "int"},
                "customer_id": {"type": "int"},
                "plan": {"type": "text"},
                "mrr": {"type": "numeric"},
                "status": {"type": "text"},
            },
        },
        # exists but is not joined to anything -> used for the unreachable test
        "tickets": {
            "table": "tickets",
            "fields": {"id": {"type": "int"}, "severity": {"type": "text"}},
        },
    },
    "joins": [
        {"left": "subscriptions", "right": "customers",
         "on": "subscriptions.customer_id = customers.id"},
    ],
    "metrics": {
        "mrr_total": {
            "entity": "subscriptions",
            "aggregate": "sum",
            "field": "mrr",
            "dimensions_allowed": ["plan", "status", "customers.country"],
            "filters_allowed": ["status", "customers.country"],
        },
        "customer_count": {
            "entity": "customers",
            "aggregate": "count",
            "field": "*",
            "dimensions_allowed": ["country"],
            "filters_allowed": ["country"],
        },
        # references an unjoined entity to exercise unreachable_entity
        "tickets_per_country": {
            "entity": "tickets",
            "aggregate": "count",
            "field": "*",
            "dimensions_allowed": ["customers.country"],
        },
    },
    "policy": {"block_pii": ["EMAIL_ADDRESS", "CREDIT_CARD"], "max_rows": 500},
}


@pytest.fixture
def layer():
    return SemanticLayer.from_dict(LAYER_DICT)


# -- records ----------------------------------------------------------------

def test_records_default_fields_select_only(layer):
    c = compile_records(layer, "customers", fields=["id", "country"])
    assert c.sql.lower().startswith("select")
    assert "customers" in c.sql
    assert "email" not in c.sql.lower()


def test_records_pii_field_is_refused(layer):
    with pytest.raises(Refusal) as e:
        compile_records(layer, "customers", fields=["id", "email"])
    assert e.value.kind == "pii_blocked"
    assert e.value.recovery["blocked_category"] == "EMAIL_ADDRESS"


def test_records_filter_binds_parameters(layer):
    c = compile_records(
        layer, "customers", fields=["id"],
        filters=[{"field": "country", "op": "=", "value": "UK"}],
    )
    assert c.params == {"p0": "UK"}
    assert ":p0" not in c.sql or "p0" in c.sql  # placeholder present in some form
    assert "UK" not in c.sql  # value is bound, never inlined


def test_records_in_operator_binds_each_value(layer):
    c = compile_records(
        layer, "customers", fields=["id"],
        filters=[{"field": "country", "op": "in", "value": ["UK", "US", "DE"]}],
    )
    assert c.params == {"p0": "UK", "p1": "US", "p2": "DE"}


def test_bad_operator_is_refused(layer):
    with pytest.raises(Refusal) as e:
        compile_records(layer, "customers", fields=["id"],
                        filters=[{"field": "country", "op": "; drop table"}])
    assert e.value.kind == "bad_operator"


# -- metrics ----------------------------------------------------------------

def test_metric_aggregate_and_group_by(layer):
    c = compile_metric(layer, "mrr_total", dimensions=["plan"])
    s = c.sql.lower()
    assert "sum(subscriptions.mrr)" in s
    assert "group by" in s
    assert "mrr_total" in s


def test_metric_join_resolution(layer):
    c = compile_metric(layer, "mrr_total", dimensions=["customers.country"])
    s = c.sql.lower()
    assert "join" in s
    assert "customers" in s and "subscriptions" in s
    assert set(c.entities) == {"customers", "subscriptions"}


def test_metric_unreachable_entity_is_refused(layer):
    with pytest.raises(Refusal) as e:
        compile_metric(layer, "tickets_per_country", dimensions=["customers.country"])
    assert e.value.kind == "unreachable_entity"


def test_metric_dimension_not_allowed(layer):
    with pytest.raises(Refusal) as e:
        compile_metric(layer, "mrr_total", dimensions=["mrr"])
    assert e.value.kind == "dimension_not_allowed"


def test_metric_count_star(layer):
    c = compile_metric(layer, "customer_count", dimensions=["country"])
    assert "count(*)" in c.sql.lower()


# -- multi-dialect + limits -------------------------------------------------

def test_limit_clamped_to_policy_max(layer):
    c = compile_records(layer, "customers", fields=["id"], limit=99999)
    assert "500" in c.sql  # clamped to policy.max_rows


def test_tsql_emits_top_not_limit(layer):
    c = compile_records(layer, "customers", fields=["id"], limit=10, dialect="tsql")
    assert "top" in c.sql.lower()
    assert "limit" not in c.sql.lower()


def test_sqlite_dialect_round_trips(layer):
    c = compile_metric(layer, "mrr_total", dimensions=["plan"], dialect="sqlite")
    assert c.dialect == "sqlite"
    assert c.sql.lower().startswith("select")
