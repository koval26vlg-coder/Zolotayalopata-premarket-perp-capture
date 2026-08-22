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
    "schema": "premarket_perp_capture_planonly_v3",
    "plan_id": "premarket_perp_capture_20260822_v3",
    "plan_hash": "ee5f555f88691e18207ec22231217a73ec2a82f25069402b14e8d85646350627",
    "plan_file_sha256": "d6b67c4f52f05bd6902855bc58b416eaab2ef9e3bd430e948ff77d9c6bdb9f94",
}

RETIRED_PLANS = (
    {
        "schema": "premarket_perp_capture_planonly_v1",
        "plan_id": "premarket_perp_capture_20260822",
        "plan_hash": "aa174438bf457e3a57d94e8f3839ae9a61dbb42504d03f5876825f59a9b2d6c1",
        "plan_file_sha256": "cac4d34cbc6228fd0a7fc7922afb8ce3b1110388a1df860dba5bbd9f40ae2934",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822.json",
    },
    {
        "schema": "premarket_perp_capture_planonly_v2",
        "plan_id": "premarket_perp_capture_20260822_v2",
        "plan_hash": "b7c0543a81b9afa6781f1ca89871d0632405551b3ff51e18e10348da405910d7",
        "plan_file_sha256": "1d990fbfd84cf5d9d06fd927074b50d200e1d49c0f9bc4200020ec43cb4aac57",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v2.json",
    },
)

# Compatibility aliases keep the verifier's trust-root surface deliberately tiny.
PLAN_SCHEMA = ACTIVE_PLAN["schema"]
PLAN_ID = ACTIVE_PLAN["plan_id"]
PLAN_HASH = ACTIVE_PLAN["plan_hash"]
PLAN_FILE_SHA256 = ACTIVE_PLAN["plan_file_sha256"]
