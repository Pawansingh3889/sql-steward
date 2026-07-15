"""Export to Apache Ossie (OSI): mapping, governance extensions, lossless notes."""
import copy
import json

import pytest
import yaml

from sql_steward.osi_export import OSI_VERSION, VENDOR, to_osi, to_osi_yaml
from sql_steward.semantic import SemanticLayer

LAYER_DICT = {
    "dialect": "postgres",
    "entities": {
        "customers": {
            "table": "public.customers",
            "primary_key": "id",
            "description": "People who pay.",
            "fields": {
                "id": {"type": "int"},
                "email": {"type": "text", "pii": "EMAIL_ADDRESS"},
                "country": {"type": "text"},
                "created_at": {"type": "timestamp"},
            },
        },
        "subscriptions": {
            "table": "subscriptions",
            "primary_key": "id",
            "fields": {
                "id": {"type": "int"},
                "customer_id": {"type": "int"},
                "mrr": {"type": "numeric"},
                "plan": {"type": "text"},
            },
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
            "description": "Total MRR.",
            "dimensions_allowed": ["plan", "customers.country"],
            "filters_allowed": ["plan"],
        },
        "customer_count": {"entity": "customers", "aggregate": "count", "field": "*"},
    },
    "policy": {"block_pii": ["EMAIL_ADDRESS"], "max_rows": 500},
}


@pytest.fixture()
def layer():
    return SemanticLayer.from_dict(copy.deepcopy(LAYER_DICT))


def _extension_payload(obj: dict) -> dict:
    exts = [e for e in obj.get("custom_extensions", []) if e["vendor_name"] == VENDOR]
    assert len(exts) == 1
    return json.loads(exts[0]["data"])


def test_document_shape(layer):
    doc, issues = to_osi(layer)
    assert doc["version"] == OSI_VERSION
    assert set(doc) == {"version", "semantic_model"}
    (model,) = doc["semantic_model"]
    assert model["name"] == "sql_steward_model"
    assert {d["name"] for d in model["datasets"]} == {"customers", "subscriptions"}
    assert issues == []


def test_dataset_mapping(layer):
    doc, _ = to_osi(layer)
    customers = next(d for d in to_osi(layer)[0]["semantic_model"][0]["datasets"]
                     if d["name"] == "customers")
    assert customers["source"] == "public.customers"
    assert customers["primary_key"] == ["id"]
    assert customers["description"] == "People who pay."


def test_pii_rides_in_field_extension(layer):
    customers = next(d for d in to_osi(layer)[0]["semantic_model"][0]["datasets"]
                     if d["name"] == "customers")
    email = next(f for f in customers["fields"] if f["name"] == "email")
    assert _extension_payload(email) == {"pii": "EMAIL_ADDRESS"}
    country = next(f for f in customers["fields"] if f["name"] == "country")
    assert "custom_extensions" not in country


def test_time_fields_flagged(layer):
    customers = next(d for d in to_osi(layer)[0]["semantic_model"][0]["datasets"]
                     if d["name"] == "customers")
    created = next(f for f in customers["fields"] if f["name"] == "created_at")
    assert created["dimension"] == {"is_time": True}


def test_relationship_direction_from_primary_key(layer):
    (model,) = to_osi(layer)[0]["semantic_model"]
    (rel,) = model["relationships"]
    # customers.id is customers' primary key, so customers is the one side.
    assert rel["from"] == "subscriptions"
    assert rel["to"] == "customers"
    assert rel["from_columns"] == ["customer_id"]
    assert rel["to_columns"] == ["id"]


def test_non_equality_join_is_skipped_with_note(layer):
    data = copy.deepcopy(LAYER_DICT)
    data["joins"] = [{"left": "subscriptions", "right": "customers",
                      "on": "subscriptions.customer_id > customers.id"}]
    doc, issues = to_osi(SemanticLayer.from_dict(data))
    assert "relationships" not in doc["semantic_model"][0]
    assert any("skipped" in i for i in issues)


def test_metric_expressions(layer):
    (model,) = to_osi(layer)[0]["semantic_model"]
    by_name = {m["name"]: m for m in model["metrics"]}
    mrr = by_name["mrr_total"]["expression"]["dialects"]
    assert mrr == [{"dialect": "ANSI_SQL", "expression": "SUM(subscriptions.mrr)"}]
    count = by_name["customer_count"]["expression"]["dialects"][0]["expression"]
    assert count == "COUNT(*)"


def test_metric_allow_lists_ride_in_extension(layer):
    (model,) = to_osi(layer)[0]["semantic_model"]
    mrr = next(m for m in model["metrics"] if m["name"] == "mrr_total")
    payload = _extension_payload(mrr)
    assert payload["dimensions_allowed"] == ["plan", "customers.country"]
    assert payload["aggregate"] == "sum"


def test_policy_and_dialect_in_model_extension(layer):
    (model,) = to_osi(layer)[0]["semantic_model"]
    payload = _extension_payload(model)
    assert payload["dialect"] == "postgres"
    assert payload["policy"] == {"block_pii": ["EMAIL_ADDRESS"], "max_rows": 500}


def test_native_dialect_added_for_snowflake():
    data = copy.deepcopy(LAYER_DICT)
    data["dialect"] = "snowflake"
    (model,) = to_osi(SemanticLayer.from_dict(data))[0]["semantic_model"]
    mrr = next(m for m in model["metrics"] if m["name"] == "mrr_total")
    dialects = {d["dialect"] for d in mrr["expression"]["dialects"]}
    assert dialects == {"ANSI_SQL", "SNOWFLAKE"}


def test_yaml_round_trips(layer):
    text, _ = to_osi_yaml(layer)
    doc = yaml.safe_load(text)
    assert doc["version"] == OSI_VERSION
    assert doc["semantic_model"][0]["datasets"]
