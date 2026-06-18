"""Execute a compiled (SELECT-only) statement and return rows.

Execution goes through SQLAlchemy, so the same compiled query runs against
SQL Server, Postgres or SQLite. Values are always bound parameters, never
inlined. The read-only guarantee comes from the compiler -- it cannot emit
anything but a SELECT -- and is reinforced here for engines that support a
read-only transaction.
"""
from __future__ import annotations

from sqlalchemy import create_engine, text

from sql_steward.compiler import Compiled


class Engine:
    def __init__(self, url: str):
        self.url = url
        self._engine = create_engine(url)

    def run(self, compiled: Compiled) -> list[dict]:
        with self._engine.connect() as conn:
            self._set_read_only(conn)
            result = conn.execute(text(compiled.sql), compiled.params)
            cols = list(result.keys())
            return [dict(zip(cols, row)) for row in result.fetchall()]

    def _set_read_only(self, conn) -> None:
        backend = self._engine.url.get_backend_name()
        try:
            if backend.startswith("postgres"):
                conn.execute(text("SET TRANSACTION READ ONLY"))
            # SQLite/MSSQL: the compiler's SELECT-only guarantee is the control;
            # add driver read-only flags via the connection URL if you want a
            # second layer (e.g. ?mode=ro for sqlite, ApplicationIntent for mssql).
        except Exception:
            # Best effort; never let a read-only hint break a legitimate read.
            pass
