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
PLAN_ID = "premarket_perp_capture_20260822_v3"
PLAN_HASH = "ef17f97b00faf1de53eecb16b3bd4355bfabd70fd887e1df0efd787149cdef92"
PLAN_FILE_SHA256 = "60e2c64048091ea191ba40a60e69ba4916af2a13f22dcb0c089fca614d114192"
