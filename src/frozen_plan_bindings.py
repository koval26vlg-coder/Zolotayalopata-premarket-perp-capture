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
    "schema": "premarket_perp_capture_planonly_v9",
    "plan_id": "premarket_perp_capture_20260822_v9",
    "plan_hash": "513ecd6667fc2b5c1a1e66e5e8c9855f9cdb5a6404714b963cdb5ea0ec634296",
    "plan_file_sha256": "6b6c88868ad49e73f557dbe47c56305222174e67142e5214eaf4120229f5a098",
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
    {
        "schema": "premarket_perp_capture_planonly_v5",
        "plan_id": "premarket_perp_capture_20260822_v5",
        "plan_hash": "01b60cf10d82ccd523a43dc96539bce035fda73454c93b702250746b8b10d9e0",
        "plan_file_sha256": "948f8820e52b16ac3804445e830629003b33d8194f17cec46355c66e5213c349",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v5.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v6",
        "plan_id": "premarket_perp_capture_20260822_v6",
        "plan_hash": "b2e07bd3475b57b4d815bf1adca8dbd5b52f120d4b544ea10d3227186682ab2e",
        "plan_file_sha256": "0be95c2a4a60e6457697bfa0bf612ada7b0e63efdd903abafb7ab9c77f1bbe6f",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v6.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v7",
        "plan_id": "premarket_perp_capture_20260822_v7",
        "plan_hash": "0fb59db93f3f52a47614e080e04d59b77fbdbbc990da888b291b4cc832330e59",
        "plan_file_sha256": "6ac94a64be7a83835b764115d1805f05d2194ac060c4b4df7ddfb768bb5ab75e",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v7.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v8",
        "plan_id": "premarket_perp_capture_20260822_v8",
        "plan_hash": "fb9a44f17ca2f3ffcb8f9ef87c7e9ad42684bfd80ad03dfe5ad48d05f34d223f",
        "plan_file_sha256": "045c614865cc0744025b93eb1ee5ef1de2d093d8680a1a9d3f2e64909839ced5",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v8.json"
    },
)

# Compatibility aliases keep the verifier's trust-root surface deliberately tiny.
PLAN_SCHEMA = ACTIVE_PLAN["schema"]
PLAN_ID = ACTIVE_PLAN["plan_id"]
PLAN_HASH = ACTIVE_PLAN["plan_hash"]
PLAN_FILE_SHA256 = ACTIVE_PLAN["plan_file_sha256"]
