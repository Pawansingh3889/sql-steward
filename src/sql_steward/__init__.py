"""sql-steward: a governed SQL gateway for AI agents.

The agent never writes SQL and never sees a connection string. It calls typed
tools; sql-steward compiles every query from a semantic layer you control,
refuses blocked PII before running, and (optionally) routes the result through
your existing query-warden / pii-veil / agent-blackbox pieces.
"""
from sql_steward.compiler import Compiled, Refusal, compile_metric, compile_records
from sql_steward.semantic import SemanticLayer

try:  # single source of truth: the installed package metadata
    from importlib.metadata import version as _version

    __version__ = _version("sql-steward")
except Exception:  # running from a source tree without installation
    __version__ = "0.0.0.dev0"
__all__ = [
    "SemanticLayer",
    "Compiled",
    "Refusal",
    "compile_metric",
    "compile_records",
    "__version__",
]
