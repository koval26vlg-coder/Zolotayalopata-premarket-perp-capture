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
PLAN_HASH = "6b4093be300c456794413486879a9302af12e86c3bf0994bfa075f7c7270592a"
PLAN_FILE_SHA256 = "22a31cd3e283e492f062e66d0f6353e9c08d336fa1ceddb2a33d0888440e8836"
