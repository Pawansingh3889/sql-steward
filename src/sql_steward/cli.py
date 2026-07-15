"""Command line: serve the MCP server, validate a layer, or run the demo.

    sql-steward                 # serve over stdio (for Claude Desktop, Cursor, ...)
    sql-steward serve
    sql-steward validate [path] # load + validate a semantic layer
    sql-steward demo            # zero-config end-to-end demo on a temp SQLite db
    sql-steward audit-verify    # verify the agent-blackbox audit chain
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile

from sql_steward import __version__
from sql_steward.compiler import Refusal, compile_metric, compile_records
from sql_steward.engine import Engine
from sql_steward.semantic import SemanticLayer

# A self-contained layer for the demo (SQLite so it needs no external services).
DEMO_LAYER = {
    "dialect": "sqlite",
    "entities": {
        "customers": {
            "table": "customers",
            "primary_key": "id",
            "description": "People who pay for the product.",
            "fields": {
                "id": {"type": "int"},
                "name": {"type": "text", "pii": "PERSON"},
                "email": {"type": "text", "pii": "EMAIL_ADDRESS"},
                "country": {"type": "text"},
            },
        },
        "subscriptions": {
            "table": "subscriptions",
            "description": "Active and past subscriptions.",
            "fields": {
                "id": {"type": "int"},
                "customer_id": {"type": "int"},
                "plan": {"type": "text"},
                "mrr": {"type": "numeric"},
                "status": {"type": "text"},
            },
        },
    },
    "joins": [
        {"left": "subscriptions", "right": "customers",
         "on": "subscriptions.customer_id = customers.id"},
    ],
    "metrics": {
        "mrr_total": {
            "entity": "subscriptions", "aggregate": "sum", "field": "mrr",
            "description": "Total monthly recurring revenue.",
            "dimensions_allowed": ["plan", "status", "customers.country"],
            "filters_allowed": ["status", "customers.country"],
        },
    },
    "policy": {"block_pii": ["EMAIL_ADDRESS", "CREDIT_CARD"], "max_rows": 1000},
}

_DEMO_CUSTOMERS = [
    (1, "Ada Lovelace", "ada@example.com", "UK"),
    (2, "Alan Turing", "alan@example.com", "UK"),
    (3, "Grace Hopper", "grace@example.com", "US"),
    (4, "Edsger Dijkstra", "edsger@example.com", "NL"),
    (5, "Margaret Hamilton", "margaret@example.com", "US"),
]
_DEMO_SUBS = [
    (1, 1, "pro", 99.0, "active"),
    (2, 2, "team", 299.0, "active"),
    (3, 3, "pro", 99.0, "active"),
    (4, 4, "pro", 99.0, "cancelled"),
    (5, 5, "team", 299.0, "active"),
]


def _seed_demo_db(path: str) -> None:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE customers (id INTEGER, name TEXT, email TEXT, country TEXT)")
    con.execute(
        "CREATE TABLE subscriptions (id INTEGER, customer_id INTEGER, plan TEXT, mrr REAL, status TEXT)"
    )
    con.executemany("INSERT INTO customers VALUES (?,?,?,?)", _DEMO_CUSTOMERS)
    con.executemany("INSERT INTO subscriptions VALUES (?,?,?,?,?)", _DEMO_SUBS)
    con.commit()
    con.close()


def cmd_validate(args) -> int:
    layer = SemanticLayer.from_yaml(args.layer)
    print(f"OK  {args.layer}")
    print(f"  dialect : {layer.dialect}")
    print(f"  entities: {', '.join(layer.entities)}")
    print(f"  metrics : {', '.join(layer.metrics)}")
    print(f"  joins   : {len(layer.joins)}")
    print(f"  blocked PII: {', '.join(sorted(layer.policy.block_pii)) or '(none)'}")
    return 0


def cmd_demo(args) -> int:
    tmp = tempfile.mkdtemp(prefix="sql-steward-demo-")
    db = os.path.join(tmp, "demo.db")
    _seed_demo_db(db)
    layer = SemanticLayer.from_dict(DEMO_LAYER)
    engine = Engine(f"sqlite:///{db}")

    print("sql-steward demo  (SQLite, no API key, no agent)\n")
    print("The agent can only call typed tools. Watch what it can and cannot do.\n")

    print("1) get_metric('mrr_total', dimensions=['plan'])  -> safe aggregate")
    c = compile_metric(layer, "mrr_total", dimensions=["plan"])
    print(f"   compiled: {c.sql}")
    for row in engine.run(c):
        print(f"   {row}")
    print()

    print("2) get_metric('mrr_total', dimensions=['customers.country'])  -> auto-join")
    c = compile_metric(layer, "mrr_total", dimensions=["customers.country"])
    print(f"   compiled: {c.sql}")
    for row in engine.run(c):
        print(f"   {row}")
    print()

    print("3) get_records('customers', fields=['id','email'])  -> PII refusal")
    try:
        compile_records(layer, "customers", fields=["id", "email"])
    except Refusal as r:
        print(f"   refused: {json.dumps(r.as_dict())}")
    print()

    print("Every successful call would also land in the agent-blackbox audit")
    print("chain when that integration is enabled. Nothing here wrote to the db.")
    return 0


def cmd_init(args) -> int:
    from sql_steward.introspect import introspect, to_yaml

    include = [t.strip() for t in args.include.split(",")] if args.include else None
    exclude = [t.strip() for t in args.exclude.split(",")] if args.exclude else None
    layer, stats = introspect(args.from_db, include=include, exclude=exclude)
    text = to_yaml(layer, stats)

    if args.out and args.out != "-":
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        # Summary to stderr so `--out -` piping stays clean.
        print(
            f"drafted {args.out}: {stats['tables']} tables, {stats['fields']} fields "
            f"({stats['pii_fields']} PII-tagged), {stats['joins']} joins -> "
            f"block_pii={stats['blocked_pii'] or '(none)'}",
            file=sys.stderr,
        )
        print("Review it before serving: narrow entities, check PII tags, add metrics.", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


def cmd_audit_verify(args) -> int:
    from sql_steward.safety import audit_status

    print(json.dumps(audit_status(), indent=2))
    return 0


def cmd_serve(args) -> int:
    from sql_steward.server import main as serve

    serve()
    return 0


def cmd_export(args) -> int:
    from sql_steward.osi_export import to_osi_yaml

    layer = SemanticLayer.from_yaml(args.layer)
    text, issues = to_osi_yaml(layer, model_name=args.model_name)
    for issue in issues:
        print(f"note: {issue}", file=sys.stderr)
    if args.out == "-":
        print(text, end="")
    else:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"Wrote {args.out} (OSI {len(text.splitlines())} lines, "
              f"{len(issues)} notes)", file=sys.stderr)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="sql-steward", description=__doc__)
    parser.add_argument("--version", action="version", version=f"sql-steward {__version__}")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="run the MCP server over stdio")
    v = sub.add_parser("validate", help="load and validate a semantic layer")
    v.add_argument("layer", nargs="?", default="semantic.yaml")
    sub.add_parser("demo", help="zero-config end-to-end demo on a temp SQLite db")
    sub.add_parser("audit-verify", help="verify the agent-blackbox audit chain")
    i = sub.add_parser("init", help="draft a semantic layer from a live database")
    i.add_argument("--from-db", required=True, metavar="URL",
                   help="SQLAlchemy URL, e.g. sqlite:///data.db or postgresql+psycopg://user@host/db")
    i.add_argument("--out", default="semantic.yaml",
                   help="output path, or '-' for stdout (default: semantic.yaml)")
    i.add_argument("--include", help="comma-separated tables to keep (default: all)")
    i.add_argument("--exclude", help="comma-separated tables to drop")
    e = sub.add_parser("export", help="export the layer as an Apache Ossie (OSI) document")
    e.add_argument("layer", nargs="?", default="semantic.yaml")
    e.add_argument("--out", default="-", help="output path, or '-' for stdout (default: -)")
    e.add_argument("--model-name", default="sql_steward_model",
                   help="OSI semantic_model name (default: sql_steward_model)")

    args = parser.parse_args()
    if args.cmd in (None, "serve"):
        raise SystemExit(cmd_serve(args))
    if args.cmd == "validate":
        raise SystemExit(cmd_validate(args))
    if args.cmd == "demo":
        raise SystemExit(cmd_demo(args))
    if args.cmd == "audit-verify":
        raise SystemExit(cmd_audit_verify(args))
    if args.cmd == "init":
        raise SystemExit(cmd_init(args))
    if args.cmd == "export":
        raise SystemExit(cmd_export(args))


if __name__ == "__main__":
    main()
