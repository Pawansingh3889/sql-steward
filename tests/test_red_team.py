"""Red-team suite: adversarial requests, each contained by construction.

Every test here is an attack a compromised or jailbroken model might attempt
through the tool surface. A test passes when the attack is refused, its payload
is bound as a parameter instead of executed, or the data it wants never reaches
the compiled SQL. This is the evidence behind "guards by construction": there is
no request shape that turns into a write, a blocked column, or injected SQL.

The attacks run against the compiler, which is the chokepoint every tool call
goes through (see server.py). Grouped by the guarantee each one probes.
"""
import sqlglot
import pytest

from sql_steward import server
from sql_steward.compiler import (
    Refusal,
    compile_metric,
    compile_records,
    compile_vector_search,
)
from sql_steward.semantic import SemanticError, SemanticLayer

# A layer with real PII, an unjoined entity, a vector entity, and one
# deliberately misconfigured metric that allows a PII dimension/filter, so the
# compile-time gate can be tested as a backstop against a careless author.
RED_TEAM_LAYER = {
    "dialect": "postgres",
    "entities": {
        "customers": {
            "table": "customers",
            "primary_key": "id",
            "fields": {
                "id": {"type": "int"},
                "name": {"type": "text", "pii": "PERSON"},
                "email": {"type": "text", "pii": "EMAIL_ADDRESS"},
                "card": {"type": "text", "pii": "CREDIT_CARD"},
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
        # exists but is joined to nothing -> reaching it is unreachable_entity
        "tickets": {
            "table": "tickets",
            "fields": {"id": {"type": "int"}, "severity": {"type": "text"}},
        },
        "documents": {
            "table": "docs",
            "fields": {
                "id": {"type": "int"},
                "title": {"type": "text"},
                "owner_email": {"type": "text", "pii": "EMAIL_ADDRESS"},
                "body": {"type": "text"},
                "embedding": {"type": "vector"},
            },
            "search": {"vector_column": "embedding", "dim": 4, "returns": ["id", "title"]},
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
        # misconfigured on purpose: allows a PII dimension and a PII filter
        "leaky_metric": {
            "entity": "customers",
            "aggregate": "count",
            "field": "*",
            "dimensions_allowed": ["email"],
            "filters_allowed": ["card"],
        },
    },
    "policy": {"block_pii": ["EMAIL_ADDRESS", "CREDIT_CARD"], "max_rows": 500},
}

# Values that are dangerous if a query inlines them. They must always be bound.
INJECTIONS = [
    "'; DROP TABLE customers; --",
    "1 OR 1=1",
    "') UNION SELECT card FROM customers --",
    "x'; DELETE FROM subscriptions; --",
    "admin'--",
]


@pytest.fixture
def layer():
    return SemanticLayer.from_dict(RED_TEAM_LAYER)


# -- no write path ----------------------------------------------------------

def test_no_write_capable_tool_is_exposed():
    """The tool surface has no name that could mutate. A write is not caught,
    it is unrepresentable, because no tool accepts SQL or performs a write."""
    names = set(vars(server))
    for forbidden in ("run_sql", "execute", "execute_sql", "raw_sql",
                      "insert", "update", "delete", "write", "write_rows"):
        assert forbidden not in names, f"write-capable tool exposed: {forbidden}"
    for expected in ("get_records", "get_metric", "semantic_search"):
        assert expected in names


def test_every_compiled_statement_is_read_only(layer):
    """Whatever the request shape, the compiler only ever emits a SELECT."""
    compiled = [
        compile_records(layer, "customers", fields=["id", "country"]),
        compile_records(layer, "customers", fields=["id"],
                        filters=[{"field": "country", "op": "=", "value": "UK"}]),
        compile_metric(layer, "mrr_total", dimensions=["plan"]),
        compile_metric(layer, "mrr_total", dimensions=["customers.country"]),
        compile_vector_search(layer, "documents", [0.1, 0.2, 0.3, 0.4]),
    ]
    for c in compiled:
        parsed = sqlglot.parse_one(c.sql, read=c.dialect)
        assert type(parsed).__name__ == "Select", f"non-SELECT compiled: {c.sql}"


# -- PII exfiltration -------------------------------------------------------

def test_direct_pii_field_request_refused(layer):
    with pytest.raises(Refusal) as e:
        compile_records(layer, "customers", fields=["id", "email"])
    assert e.value.kind == "pii_blocked"


def test_credit_card_field_refused(layer):
    with pytest.raises(Refusal) as e:
        compile_records(layer, "customers", fields=["card"])
    assert e.value.kind == "pii_blocked"
    assert e.value.recovery["blocked_category"] == "CREDIT_CARD"


def test_pii_smuggled_through_a_filter_refused(layer):
    """Put the blocked column in a WHERE clause instead of the SELECT list."""
    with pytest.raises(Refusal) as e:
        compile_records(layer, "customers", fields=["id"],
                        filters=[{"field": "email", "op": "=", "value": "a@b.com"}])
    assert e.value.kind == "pii_blocked"


def test_misconfigured_metric_pii_dimension_still_refused(layer):
    """Defense in depth: even when the layer author wrongly allows a PII
    dimension, the compile-time gate refuses it."""
    with pytest.raises(Refusal) as e:
        compile_metric(layer, "leaky_metric", dimensions=["email"])
    assert e.value.kind == "pii_blocked"


def test_misconfigured_metric_pii_filter_still_refused(layer):
    with pytest.raises(Refusal) as e:
        compile_metric(layer, "leaky_metric",
                       filters=[{"field": "card", "op": "=", "value": "4111"}])
    assert e.value.kind == "pii_blocked"


def test_pii_via_semantic_search_fields_refused(layer):
    with pytest.raises(Refusal) as e:
        compile_vector_search(layer, "documents", [0.1, 0.2, 0.3, 0.4],
                              fields=["id", "owner_email"])
    assert e.value.kind == "pii_blocked"


def test_raw_embedding_cannot_be_exfiltrated(layer):
    """Name the vector column directly to try to read the raw embedding."""
    with pytest.raises(Refusal) as e:
        compile_vector_search(layer, "documents", [0.1, 0.2, 0.3, 0.4],
                              fields=["id", "embedding"])
    assert e.value.kind == "vector_column_not_returnable"


# -- SQL injection via values (must be bound, never inlined) -----------------

@pytest.mark.parametrize("payload", INJECTIONS)
def test_injection_in_filter_value_is_bound(layer, payload):
    c = compile_records(layer, "customers", fields=["id"],
                        filters=[{"field": "country", "op": "=", "value": payload}])
    assert payload in c.params.values()      # captured as a bound parameter
    assert payload not in c.sql              # never inlined into the SQL text
    lowered = c.sql.lower()
    assert "drop" not in lowered and "delete" not in lowered and "union" not in lowered


@pytest.mark.parametrize("payload", INJECTIONS)
def test_injection_in_in_list_is_bound(layer, payload):
    c = compile_records(layer, "customers", fields=["id"],
                        filters=[{"field": "country", "op": "in", "value": [payload, "UK"]}])
    assert payload in c.params.values()
    assert payload not in c.sql


def test_injection_in_vector_search_filter_is_bound(layer):
    payload = "'; DROP TABLE docs; --"
    c = compile_vector_search(layer, "documents", [0.1, 0.2, 0.3, 0.4],
                              filters=[{"field": "title", "op": "like", "value": payload}])
    assert payload in c.params.values()
    assert "drop table" not in c.sql.lower()


# -- identifier injection (names are validated against the layer, not run) ---

def test_injection_via_field_name_rejected(layer):
    with pytest.raises(SemanticError):
        compile_records(layer, "customers",
                        fields=["id", "email FROM customers; DROP TABLE x --"])


def test_injection_via_entity_name_rejected(layer):
    with pytest.raises(SemanticError):
        compile_records(layer, "customers; DROP TABLE customers; --", fields=["id"])


def test_injection_via_dimension_name_rejected(layer):
    with pytest.raises(Refusal) as e:
        compile_metric(layer, "mrr_total",
                       dimensions=["plan); DROP TABLE subscriptions; --"])
    assert e.value.kind == "dimension_not_allowed"


def test_injection_via_order_by_rejected(layer):
    with pytest.raises(SemanticError):
        compile_records(layer, "customers", fields=["id"],
                        order_by="id; DROP TABLE customers; --")


def test_injection_via_operator_rejected(layer):
    with pytest.raises(Refusal) as e:
        compile_records(layer, "customers", fields=["id"],
                        filters=[{"field": "country",
                                  "op": "= 1); DROP TABLE x --", "value": "x"}])
    assert e.value.kind == "bad_operator"


# -- reach and resource limits ----------------------------------------------

def test_cross_entity_reach_without_join_refused(layer):
    """Filter through an unjoined entity to pull data it cannot be joined to."""
    with pytest.raises(Refusal) as e:
        compile_records(layer, "customers", fields=["id"],
                        filters=[{"field": "tickets.severity", "op": "=", "value": "high"}])
    assert e.value.kind == "unreachable_entity"


def test_semantic_search_cross_entity_filter_refused(layer):
    with pytest.raises(Refusal) as e:
        compile_vector_search(layer, "documents", [0.1, 0.2, 0.3, 0.4],
                              filters=[{"field": "customers.country", "op": "=", "value": "UK"}])
    assert e.value.kind == "unreachable_entity"


def test_huge_limit_is_clamped_to_policy(layer):
    c = compile_records(layer, "customers", fields=["id"], limit=10 ** 9)
    assert "500" in c.sql              # policy.max_rows
    assert "1000000000" not in c.sql


def test_negative_limit_cannot_unbound_the_query(layer):
    c = compile_records(layer, "customers", fields=["id"], limit=-5)
    assert "-5" not in c.sql
    assert c.sql.strip().lower().endswith("limit 1")   # floored to 1, never removed
