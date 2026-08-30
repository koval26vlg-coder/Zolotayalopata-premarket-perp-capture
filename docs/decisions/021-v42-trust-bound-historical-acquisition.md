# 021 — v42 trust-bound historical acquisition

Дата: 2026-08-30

## Решение

Активировать новый immutable PlanOnly v42 для одного bounded descriptive-only
historical acquisition tick по preregistered Bybit/OKX/Gate events. v41 остаётся
byte-identical отклонённым выпуском. v42 не выдаёт capture token и не разрешает
forward capture, authenticated API, venue paper/testnet, реальные ордера, margin,
leverage или capital.

## Immutable identity

- plan id: `premarket_perp_capture_20260822_v42`;
- status: `HISTORICAL_ACQUISITION_REPLAY_TRUST_BOUND_NO_CAPTURE`;
- plan hash: `72acbc1426ddfc5ccb168dd1d75d6414e5af0d30507b80f32fa8d85020691926`;
- file SHA-256: `696f6368f1f2a72fdcaa598148766324ea0d24bdc2e28308f8e15470a5e081b5`;
- supersedes: immutable v41;
- next event-bound proposal: v43.

## Acquisition boundary

- exact PlanOnly-bound seed path и SHA-256;
- canonical official assertion hash, seconds-grade `OFFICIAL_ANNOUNCEMENT`, exact
  venue host и crypto identity проверяются до claim/network;
- plan id/hash, прочитанные вместе с seed binding, обязаны совпасть со свежим
  preflight до writer claim;
- Gate archive сортируется oldest-first;
- O_EXCL/fsync/exact-readback terminal receipt предшествует successful claim release;
- claim collision возвращает JSON `RETRY_NEXT_INTERVAL`, без traceback и tight loop.

## Replay boundary

- minute OHLCV даёт только `DESCRIPTIVE_ONLY` fixed markout;
- fixed LONG execution model (`t0-60s` → `t0/+5/+15/+60s`, fees, depth,
  partial/unfilled, funding, mark/index и paper liquidation stress) проверяется на
  explicit synthetic fixtures;
- caller-supplied sealed L2 input не является provenance и всегда получает
  `NOT_RUN_TRUSTED_EVIDENCE_LOADER_REQUIRED`;
- production execution replay останется неисполненным до независимого loader,
  проверяющего manifest, terminal receipt, raw evidence и lineage;
- отсутствующий, невалидный или внеокновый mark/index path означает неизвестный
  liquidation result и `liquidation_model_missing=true`.

## Следующий checkpoint

После полного offline suite, Plan/SHA/capability preflight и отдельного review разрешён
один bounded historical tick. Его результат не доказывает fill. Для execution-grade
evidence нужен отдельный event-bound v43 и непрерывный event-window L2 capture.
