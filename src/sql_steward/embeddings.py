"""Optional local embedding generation for semantic_search.

Off by default. Point ``SQL_STEWARD_EMBED_URL`` at a local embeddings endpoint
(Ollama by default, e.g. http://localhost:11434/api/embeddings) and set
``SQL_STEWARD_EMBED_MODEL``. Keeps the on-prem promise: embeddings can be
generated locally, nothing leaves the building. Returns None if not configured
or unreachable, so the caller can refuse cleanly.
"""
from __future__ import annotations

import json
import os
import urllib.request


def embed(text: str) -> list[float] | None:
    url = os.environ.get("SQL_STEWARD_EMBED_URL")
    if not url:
        return None
    model = os.environ.get("SQL_STEWARD_EMBED_MODEL", "nomic-embed-text")
    try:
        payload = json.dumps({"model": model, "prompt": text}).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        emb = data.get("embedding")
        if emb is None and isinstance(data.get("data"), list) and data["data"]:
            emb = data["data"][0].get("embedding")
        return [float(x) for x in emb] if emb else None
    except Exception:
        return None
