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
PLAN_HASH = "6e020bcd2a2f3ba83a9e17eaab1ac578fc72118bc014b27ff69a23a0cc8a2c77"
PLAN_FILE_SHA256 = "cdb3f3b3b1f8c3b64133366a961c30567648395eab24dfb97d3105e94cf1a5ac"
