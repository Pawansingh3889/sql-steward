"""The error envelope the MCP server hands an agent for unknown names.

A misspelled metric or field is the most common agent mistake; the reply must
carry what IS available (and closest spellings) so the agent corrects itself
in the same turn instead of burning a round-trip on list_metrics /
describe_entity. Mirrors the Refusal envelope, additively: the `error` key
stays for anything already keying on it.
"""
from sql_steward.semantic import SemanticError, SemanticLayer
from sql_steward.server import _semantic_error

LAYER = SemanticLayer.from_dict(
    {
        "dialect": "sqlite",
        "entities": {
            "orders": {
                "table": "orders",
                "fields": {"id": {"type": "int"}, "total": {"type": "numeric"}},
            }
        },
        "metrics": {
            "revenue": {"entity": "orders", "aggregate": "sum", "field": "total"}
        },
    }
)


def _catch(fn) -> SemanticError:
    try:
        fn()
    except SemanticError as ex:
        return ex
    raise AssertionError("expected SemanticError")


def test_unknown_metric_envelope_is_actionable():
    out = _semantic_error(_catch(lambda: LAYER.get_metric("revenu")))
    assert out["error"] == "Unknown metric 'revenu'"
    assert out["kind"] == "unknown_metric"
    assert out["recovery"]["available"] == ["revenue"]
    assert out["recovery"]["did_you_mean"] == ["revenue"]


def test_unknown_field_envelope_names_the_entity():
    out = _semantic_error(
        _catch(lambda: LAYER.get_entity("orders").get_field("totl"))
    )
    assert out["kind"] == "unknown_field"
    assert out["recovery"]["entity"] == "orders"
    assert "total" in out["recovery"]["available"]


def test_bare_semantic_error_stays_a_plain_error():
    out = _semantic_error(SemanticError("Semantic layer must declare a 'dialect'"))
    assert out == {"error": "Semantic layer must declare a 'dialect'"}
