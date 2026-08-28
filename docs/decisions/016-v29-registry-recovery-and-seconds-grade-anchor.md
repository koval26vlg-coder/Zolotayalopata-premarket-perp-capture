# Decision 016: v29 registry recovery and seconds-grade official anchor

Date: 2026-08-28

## Decision

Publish immutable PlanOnly `premarket_perp_capture_20260822_v29`, superseding v28
without changing any v1-v28 bytes. The active plan hash is
`63f4173a4d3662e6eed15f9ba1f372c8771f635b84291ed2439e076d6975a8d5`; its file
SHA-256 is `7c93aebec952ec1d52def42ce5ac4165b6b3c8c608436ed702f50dbfb012b822`.

v29 remains capture-disabled. It authorizes only metadata registry refresh, human
official attestation, local registry quarantine, and offline descriptive work.

## Official timestamp precision

`official_spot_t0` precision is now derived from the exact verbatim time fragment.
Minute-only sources remain valid descriptive evidence with `precision_sec=60`.
Only an unambiguous UTC fragment containing an explicit time-of-day second can carry
`precision_sec=1`. The caller cannot override this classification. The registry
independently repeats the same grammar and rejects inconsistent or non-integer stored
precision.

The read-only candidate status path reuses the authoritative selector. It therefore
cannot report a candidate while the registry, active mutation receipt, surface
authority, freshness, lifecycle generation, asset identity, symbol mapping, conflict,
or due-window gates would fail. It never creates a token, writer claim, network call,
capture directory, or capture authority.

## Registry recovery

The active registry summary predates the current registry contract and must not be
silently rebound. Recovery is a separate CAS-bound operation: archive the exact old
generation through `registry_quarantine`, then perform one complete public metadata
refresh under v29. On 2026-08-28 transaction
`20260828T190001Z-ef9e656267-b0d29f75` archived the v24 generation, and the following
complete refresh established 18 v29 metadata events (Bybit 5, Gate 10, OKX 3) with no
truncation or venue error. Verification returned `REGISTRY_OK`; candidate inspection
returned `NO_SECONDS_GRADE_CANDIDATE`. Historical completed quarantine transactions
remain verifiable
after all tombstones were deliberately cleaned; partial cleanup and any tombstone name
outside the three exact published role-bound formats remain fail-closed.

## Next checkpoint

No market-data capture is authorized. A future v30 can consider one bounded visible
capture only after the current generation contains a real `CRYPTO_TOKEN` event with
an official, source-derived seconds-grade `t0`, and only after a separate user-approved
checkpoint.
