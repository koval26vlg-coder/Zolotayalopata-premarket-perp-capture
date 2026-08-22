"""External trust root for the immutable PlanOnly.

The plan pins the SHA-256 of the runtime, and the runtime verifies itself against the
plan. That loop is closed and proves nothing on its own: editing both together keeps
it perfectly consistent, which is exactly what a legitimate reissue does and exactly
what an illegitimate one would do too.

This module is deliberately tiny, is NOT part of the bound implementation set, and
pins the one plan identity the runtime is allowed to load. Changing it is a visible,
reviewable act in its own right.
"""

PLAN_SCHEMA = "premarket_perp_capture_planonly_v1"
PLAN_ID = "premarket_perp_capture_20260822"
PLAN_HASH = "aa174438bf457e3a57d94e8f3839ae9a61dbb42504d03f5876825f59a9b2d6c1"
PLAN_FILE_SHA256 = "cac4d34cbc6228fd0a7fc7922afb8ce3b1110388a1df860dba5bbd9f40ae2934"
