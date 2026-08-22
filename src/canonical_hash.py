from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Mapping


def canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_hash(payload: Mapping[str, Any]) -> str:
    normalized = copy.deepcopy(dict(payload))
    normalized.pop("plan_hash", None)
    return hashlib.sha256(canonical_json_bytes(normalized)).hexdigest()
