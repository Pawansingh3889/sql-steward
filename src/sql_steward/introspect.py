"""Draft a semantic layer from a live database.

`sql-steward init --from-db <url>` turns a blank file into a reviewable
starting point: it reflects the schema over SQLAlchemy (so SQL Server, Postgres
and SQLite all work), maps column types to the layer's small type vocabulary,
proposes PII tags from column-name heuristics, and infers joins from foreign
keys. The output is a DRAFT the human then narrows -- the goal is not to expose
the whole database, it is to remove the blank-page problem for a large schema.

Two deliberate biases:
  - Fail toward tagging. If a column name looks like it could be personal data,
    it is tagged and added to block_pii. Over-blocking a harmless column is a
    review edit; under-tagging a real one is a leak. The safe default is loud.
  - Emit only what round-trips. Joins are kept only when both sides are emitted
    entities, so the generated layer always loads and validates.
"""
from __future__ import annotations

import re

from sqlalchemy import create_engine, inspect

# Column-name heuristics -> PII category. Ordered; first match wins. Where
# Presidio has a matching recognizer the name is reused (so pii-veil's policy
# interoperates); where it does not, a descriptive category is used -- the tag
# only has to drive blocking, and a human renames on review. Patterns and their
# categories were hardened against a multi-domain red-team (healthcare, HR,
# finance, CRM). Deliberately NOT matched, to avoid over-tagging dimension and
# catalogue columns: country / region / state / county, image / photo / avatar,
# bare name / title / partner. Bias is toward tagging: a false positive is a
# review edit, a miss is a leak.
_PII_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(^|_)(email|e_mail)($|_)", re.I), "EMAIL_ADDRESS"),
    (re.compile(r"(phone|mobile|telephone|fax|msisdn)", re.I), "PHONE_NUMBER"),
    # Payment-card data, including the security fields (CVV never belongs in a store).
    (re.compile(r"(credit_?card|card_?number|cc_?num|(^|_)pan($|_)|(^|_)cvv2?($|_)|(^|_)cvc($|_)|card_?expiry)", re.I), "CREDIT_CARD"),
    # Secrets. An agent must never read these; blocking beats masking.
    (re.compile(r"(password|passwd|(^|_)pwd($|_)|(^|_)secret($|_)|api_?key|access_?token|refresh_?token|private_?key|security_(answer|question))", re.I), "CREDENTIAL"),
    (re.compile(r"(iban|(^|_)bic($|_)|swift(_?code)?|sort_?code|account_?(number|no|holder|name)|(^|_)acct_?(no|num|number)|routing|bank_?account)", re.I), "IBAN_CODE"),
    # Government / tax / patient / vehicle identifiers -- direct personal IDs.
    (re.compile(
        r"(ssn|social_security|nino|national_insurance|passport"
        r"|(^|_)tin($|_)|tax_?id|taxpayer|(^|_)utr($|_)|itin|(^|_)tfn($|_)|(^|_)sin($|_)|aadhaar|(^|_)nif($|_)|(^|_)dni($|_)|cpf|cnpj|vat_?(number|reg)"
        r"|(^|_)mrn($|_)|medical_?record|hospital_?number|chi_?number|patient_?id|nhs|member_?id|medicare|health_?insurance"
        r"|driving_?licen|driver_?licen|(^|_)dvla($|_)|vehicle_?registration|(^|_)vrm($|_)|number_?plate|reg_?mark"
        r"|document_?number|(^|_)id_?number|identity_?card|(^|_)kyc|(^|_)mrz($|_))", re.I), "NATIONAL_ID"),
    # Special-category health data (UK GDPR Art 9).
    (re.compile(r"(diagnosis|(^|_)icd_?9($|_)|(^|_)icd_?10($|_)|snomed|read_?code|comorbidit)", re.I), "HEALTH_DATA"),
    (re.compile(r"(ethnicity|ethnic_?(group|origin)|religion|religious|sexual_?orientation|gender_?identity|marital_?status)", re.I), "SPECIAL_CATEGORY"),
    (re.compile(r"(ip_?address|ipaddr|geo_?ip|geoip)", re.I), "IP_ADDRESS"),
    (re.compile(r"(date_?of_?birth|(^|_)dob($|_)|birth_?date)", re.I), "DATE_TIME"),
    (re.compile(r"(post_?code|zip_?code|(^|_)zip($|_)|address|street|(^|_)city($|_)|(^|_)uprn($|_)|(^|_)lsoa($|_)|(^|_)msoa($|_)|latitude|longitude|(^|_)lat($|_)|(^|_)lng($|_)|geolocation|grid_?ref)", re.I), "LOCATION"),
    # Person identities: explicit names, the "who did this" audit columns, family
    # and third-party contacts, org-hierarchy roles, and recruitment roles. Bare
    # "name" is intentionally NOT matched -- it is usually a product/thing name.
    (re.compile(r"(first_?name|last_?name|full_?name|sur_?name|fore_?name|middle_?name|maiden_?name|preferred_?name|(^|_)initials($|_)|(^|_)alias($|_)|known_?as|previous_?surname|display_?name|nick_?name)", re.I), "PERSON"),
    (re.compile(r"(^|_)(person|employee|operator|staff|contact|contact_name|customer_name|username|user_name|author|owner)($|_)", re.I), "PERSON"),
    (re.compile(r"(next_?of_?kin|(^|_)nok($|_)|emergency_?contact|(^|_)carer|guardian|beneficiary|dependa?nt|(^|_)spouse|payee|cardholder|card_?holder)", re.I), "PERSON"),
    (re.compile(r"(^|_)(manager|supervisor|line_?manager|reports_?to|reporting_?to|assigned_?to|assignee|reviewer|approver|candidate|applicant|recruiter|interviewer|referee|nominee)($|_)", re.I), "PERSON"),
    (re.compile(r"_by$", re.I), "PERSON"),  # created_by, recorded_by, raised_by, closed_by, ...
]

_DIALECT_ALIASES = {"postgresql": "postgres", "mssql": "mssql", "sqlite": "sqlite"}


def _dialect(engine) -> str:
    name = engine.url.get_backend_name().lower()
    return _DIALECT_ALIASES.get(name, name)


def _semantic_type(sa_type) -> str:
    """Map a SQLAlchemy column type to the layer's small type vocabulary."""
    t = str(sa_type).upper()
    if any(k in t for k in ("INT", "SERIAL")):
        return "int"
    if any(k in t for k in ("NUMERIC", "DECIMAL", "REAL", "FLOAT", "DOUBLE", "MONEY")):
        return "numeric"
    if "BOOL" in t or t == "BIT":
        return "bool"
    if "TIMESTAMP" in t or "DATETIME" in t:
        return "datetime"
    if "DATE" in t:
        return "date"
    if "TIME" in t:
        return "text"
    return "text"


def pii_tag(column_name: str) -> str | None:
    """The PII category proposed for a column name, or None. Public for testing."""
    for pattern, category in _PII_RULES:
        if pattern.search(column_name):
            return category
    return None


def _entity_name(table: str) -> str:
    # Logical name = table name, lightly sanitised. A human renames in review.
    return re.sub(r"[^0-9a-zA-Z_]", "_", table)


def introspect(db_url: str, *, include=None, exclude=None) -> tuple[dict, dict]:
    """Reflect a schema into a draft semantic-layer dict.

    Returns (layer_dict, stats). ``include``/``exclude`` are optional iterables
    of table names to keep / drop.
    """
    engine = create_engine(db_url)
    insp = inspect(engine)

    include = {t.lower() for t in include} if include else None
    exclude = {t.lower() for t in exclude} if exclude else set()

    tables = [
        t for t in insp.get_table_names()
        if (include is None or t.lower() in include) and t.lower() not in exclude
    ]
    kept = {_entity_name(t) for t in tables}

    entities: dict[str, dict] = {}
    joins: list[dict] = []
    n_fields = n_pii = 0

    for table in sorted(tables):
        ename = _entity_name(table)
        cols = insp.get_columns(table)
        fields: dict[str, dict] = {}
        for col in cols:
            n_fields += 1
            fdef: dict = {"type": _semantic_type(col["type"])}
            tag = pii_tag(col["name"])
            if tag:
                fdef["pii"] = tag
                n_pii += 1
            fields[col["name"]] = fdef

        entity: dict = {"table": table}
        try:
            pk = insp.get_pk_constraint(table).get("constrained_columns") or []
        except Exception:
            pk = []
        if len(pk) == 1:
            entity["primary_key"] = pk[0]
        entity["fields"] = fields
        entities[ename] = entity

        try:
            fks = insp.get_foreign_keys(table)
        except Exception:
            fks = []
        for fk in fks:
            ref = fk.get("referred_table")
            loc = fk.get("constrained_columns") or []
            rem = fk.get("referred_columns") or []
            rname = _entity_name(ref) if ref else None
            # Keep a join only if both entities are emitted, so it validates.
            if rname and rname in kept and loc and rem:
                joins.append({
                    "left": ename,
                    "right": rname,
                    "on": f"{ename}.{loc[0]} = {rname}.{rem[0]}",
                })

    # Dedupe joins (same pair + condition).
    seen = set()
    deduped = []
    for j in joins:
        key = (j["left"], j["right"], j["on"])
        if key not in seen:
            seen.add(key)
            deduped.append(j)

    block = sorted({f["pii"] for e in entities.values() for f in e["fields"].values() if "pii" in f})

    layer = {
        "dialect": _dialect(engine),
        "entities": entities,
        "joins": deduped,
        "policy": {"block_pii": block, "max_rows": 1000},
    }
    stats = {
        "tables": len(entities),
        "fields": n_fields,
        "pii_fields": n_pii,
        "joins": len(deduped),
        "blocked_pii": block,
    }
    return layer, stats


_HEADER = """\
# sql-steward semantic layer -- DRAFT generated by `sql-steward init --from-db`.
#
# This is a starting point, not a finished contract. Before you point an agent
# at it:
#   1. DELETE entities the agent does not need. A governed layer should expose
#      the handful of tables required, not the whole database.
#   2. REVIEW every `pii:` tag. They come from column-name heuristics and bias
#      toward over-tagging -- correct the category or remove a false positive,
#      and add any the heuristic missed (a bare `name`, a free-text notes field).
#   3. ADD metrics and checks. The generator emits none: it cannot know which
#      aggregates are safe to pre-approve.
#   4. NARROW joins to the relationships you actually want traversable.
#
# Summary: {tables} tables, {fields} fields ({pii_fields} PII-tagged), {joins} joins.
"""


def to_yaml(layer: dict, stats: dict) -> str:
    """Serialise a draft layer to YAML with a review header."""
    import yaml

    header = _HEADER.format(**stats)
    body = yaml.safe_dump(layer, sort_keys=False, default_flow_style=False, width=100)
    return header + "\n" + body
