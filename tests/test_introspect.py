"""Tests for `sql-steward init --from-db`: draft a layer from a live schema."""
from __future__ import annotations

import sqlite3

import pytest

from sql_steward.introspect import introspect, pii_tag, to_yaml
from sql_steward.semantic import SemanticLayer


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "shop.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY,
            full_name TEXT,
            email TEXT,
            mobile_phone TEXT,
            country TEXT
        );
        CREATE TABLE products (
            id INTEGER PRIMARY KEY,
            name TEXT,               -- a product name: must NOT be tagged PERSON
            unit_cost REAL
        );
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER REFERENCES customers(id),
            product_id INTEGER REFERENCES products(id),
            quantity INTEGER,
            created_by TEXT,         -- audit identity: PERSON
            order_date DATE
        );
        CREATE TABLE order_tags (
            order_id INTEGER,
            tag TEXT,
            PRIMARY KEY (order_id, tag)   -- composite PK: no single primary_key
        );
        """
    )
    con.commit()
    con.close()
    return f"sqlite:///{path}"


def test_reflects_tables_columns_and_types(db):
    layer, stats = introspect(db)
    assert stats["tables"] == 4
    assert set(layer["entities"]) == {"customers", "products", "orders", "order_tags"}
    fields = layer["entities"]["orders"]["fields"]
    assert fields["quantity"]["type"] == "int"
    assert fields["order_date"]["type"] == "date"
    assert layer["entities"]["products"]["fields"]["unit_cost"]["type"] == "numeric"


def test_pii_heuristics_tag_person_email_phone_but_not_product_name(db):
    layer, _ = introspect(db)
    cust = layer["entities"]["customers"]["fields"]
    assert cust["email"]["pii"] == "EMAIL_ADDRESS"
    assert cust["mobile_phone"]["pii"] == "PHONE_NUMBER"
    assert cust["full_name"]["pii"] == "PERSON"
    assert cust["country"].get("pii") is None
    assert layer["entities"]["orders"]["fields"]["created_by"]["pii"] == "PERSON"
    # A product's `name` is a thing, not a person -- must stay untagged.
    assert layer["entities"]["products"]["fields"]["name"].get("pii") is None


def test_block_pii_aggregates_every_category_found(db):
    layer, stats = introspect(db)
    assert set(layer["policy"]["block_pii"]) == {"EMAIL_ADDRESS", "PHONE_NUMBER", "PERSON"}
    assert layer["policy"]["max_rows"] == 1000
    assert set(stats["blocked_pii"]) == {"EMAIL_ADDRESS", "PHONE_NUMBER", "PERSON"}


def test_infers_joins_from_foreign_keys(db):
    layer, _ = introspect(db)
    conds = {j["on"] for j in layer["joins"]}
    assert "orders.customer_id = customers.id" in conds
    assert "orders.product_id = products.id" in conds


def test_single_pk_set_composite_pk_omitted(db):
    layer, _ = introspect(db)
    assert layer["entities"]["customers"]["primary_key"] == "id"
    assert "primary_key" not in layer["entities"]["order_tags"]


def test_generated_layer_round_trips_through_the_loader(db, tmp_path):
    """The whole point: the draft must load and validate unchanged."""
    layer, stats = introspect(db)
    text = to_yaml(layer, stats)
    out = tmp_path / "semantic.yaml"
    out.write_text(text, encoding="utf-8")

    loaded = SemanticLayer.from_yaml(out)  # raises SemanticError if invalid
    assert loaded.dialect == "sqlite"
    assert set(loaded.entities) == {"customers", "products", "orders", "order_tags"}
    assert loaded.get_entity("customers").get_field("email").pii == "EMAIL_ADDRESS"
    assert "PERSON" in loaded.policy.block_pii


def test_include_and_exclude_filter_tables(db):
    only, _ = introspect(db, include=["customers", "orders"])
    assert set(only["entities"]) == {"customers", "orders"}
    # A join to a dropped entity is not emitted (keeps the layer valid).
    assert all("products" not in j["right"] for j in only["joins"])

    without, _ = introspect(db, exclude=["order_tags"])
    assert "order_tags" not in without["entities"]


def test_pii_tag_unit():
    assert pii_tag("email") == "EMAIL_ADDRESS"
    assert pii_tag("customer_email") == "EMAIL_ADDRESS"
    assert pii_tag("recorded_by") == "PERSON"
    assert pii_tag("operator_id") == "PERSON"
    assert pii_tag("post_code") == "LOCATION"
    assert pii_tag("credit_card_number") == "CREDIT_CARD"
    assert pii_tag("widget_count") is None
    assert pii_tag("name") is None  # bare 'name' left for human review
