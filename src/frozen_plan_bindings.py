"""External trust root for the immutable PlanOnly.

The plan pins the SHA-256 of the runtime, and the runtime verifies itself against the
plan. That loop is closed and proves nothing on its own: editing both together keeps
it perfectly consistent, which is exactly what a legitimate reissue does and exactly
what an illegitimate one would do too.

This module is deliberately tiny, is NOT part of the bound implementation set, and
pins the one plan identity the runtime is allowed to load. Changing it is a visible,
reviewable act in its own right.
"""

ACTIVE_PLAN = {
    "schema": "premarket_perp_capture_planonly_v5",
    "plan_id": "premarket_perp_capture_20260822_v5",
    "plan_hash": "01b60cf10d82ccd523a43dc96539bce035fda73454c93b702250746b8b10d9e0",
    "plan_file_sha256": "948f8820e52b16ac3804445e830629003b33d8194f17cec46355c66e5213c349",
}

# Every plan this project ever published, in order. They stay on disk and are
# verified: a lineage that can silently lose a version is not a lineage. v1 and v2
# predate the versioned-filename rule and are preserved exactly as they were
# published rather than regenerated into a tidier shape.
RETIRED_PLANS = (
    {
        "schema": "premarket_perp_capture_planonly_v1",
        "plan_id": "premarket_perp_capture_20260822",
        "plan_hash": "aa174438bf457e3a57d94e8f3839ae9a61dbb42504d03f5876825f59a9b2d6c1",
        "plan_file_sha256": "cac4d34cbc6228fd0a7fc7922afb8ce3b1110388a1df860dba5bbd9f40ae2934",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v1",
        "plan_id": "premarket_perp_capture_20260822",
        "plan_hash": "6b4093be300c456794413486879a9302af12e86c3bf0994bfa075f7c7270592a",
        "plan_file_sha256": "22a31cd3e283e492f062e66d0f6353e9c08d336fa1ceddb2a33d0888440e8836",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v2.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v1",
        "plan_id": "premarket_perp_capture_20260822_v3",
        "plan_hash": "ef17f97b00faf1de53eecb16b3bd4355bfabd70fd887e1df0efd787149cdef92",
        "plan_file_sha256": "60e2c64048091ea191ba40a60e69ba4916af2a13f22dcb0c089fca614d114192",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v3.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v4",
        "plan_id": "premarket_perp_capture_20260822_v4",
        "plan_hash": "fae208baf126163e2041fccffe4c1b656848a80647b1a15b0bc0af5901dd3314",
        "plan_file_sha256": "48e8e33171425cff1642b1b9088dd24593edd5649211e3552d958933a42a4f27",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v4.json"
    },
)

# Compatibility aliases keep the verifier's trust-root surface deliberately tiny.
PLAN_SCHEMA = ACTIVE_PLAN["schema"]
PLAN_ID = ACTIVE_PLAN["plan_id"]
PLAN_HASH = ACTIVE_PLAN["plan_hash"]
PLAN_FILE_SHA256 = ACTIVE_PLAN["plan_file_sha256"]
