# Решение 010: lifecycle authority, causal capture и immutable rebind v9

Дата: 2026-08-23

Активный immutable PlanOnly: **v9**, `plan_id`
`premarket_perp_capture_20260822_v9`, `plan_hash`
`513ecd6667fc2b5c1a1e66e5e8c9855f9cdb5a6404714b963cdb5ea0ec634296`,
file SHA-256
`6b6c88868ad49e73f557dbe47c56305222174e67142e5214eaf4120229f5a098`.
v1-v8 сохранены побайтово и входят в проверяемую retired lineage.

## Решение

После независимого review v8 выпущен новый immutable v9. Статус остаётся
`CAPTURE_IMPLEMENTATION_AUDIT_GREEN_NO_CAPTURE`: этот checkpoint фиксирует
реализацию и её ограничения, но не авторизует `market_data_capture`, collector,
scheduler, automation или replay.

## Lifecycle и registry authority

- реестр хранит точную `contract -> lifecycle_generation` и отдельный generation
  high-water; disappear, cancellation, transition и relist не могут переиспользовать
  старый official episode;
- OKX `xperp` считается terminal transition и не остаётся active pre-market;
- production refresh получает timestamp только после всех HTTP responses, повторно
  проверяет gate и authority под lock и коммитится через receipt-head CAS;
- injected payload разрешён только в явно non-production destination;
- пустой universe и падение ниже 50% предыдущего полного universe считаются
  acquisition failure без terminalization текущего active state;
- attestation и capture требуют current lifecycle generation и полного metadata
  refresh не старше 300 секунд;
- capture lineage включает `mutation_receipt_seq`, `mutation_receipt_hash`,
  `summary_content_sha256` и `registry_authority_state_hash`.

## Causal capture и evidence

- deadline/stop boundary проверяется до и сразу после каждого metadata/poll request;
- ответ, завершившийся за пределами окна, не разрешает начать остальные запросы batch;
- fixed exit поддерживает только первая валидная запись с
  `target <= received_ts <= target + cadence`; pre-target sample запрещён;
- failed и structurally invalid rows остаются в readiness denominator;
- sampling gaps считаются по `received_ts`;
- production metadata входит в общий bounded runtime;
- public `run_capture` принимает только exact static JSON synthetic transport и не
  может стать обходным live collector;
- non-zero exit общего gate-script всегда блокирует, даже если stdout похож на
  разрешающий JSON;
- official quote/author и URL отклоняют Unicode `Cc`/`Cf`, explicit port,
  backslash и управляющие символы.

## Проверка

- полный offline-suite: **386/386 OK**;
- PlanOnly: `PLAN_OK`;
- capability scan: `CAPABILITY_SCAN_CLEAN`;
- отрицательный capture preflight: `BLOCK`, единственный blocker — активный
  `CAPTURE_IMPLEMENTATION_AUDIT_GREEN_NO_CAPTURE`;
- capture token, capture run record и stop request не создавались;
- network, registry refresh, official attestation, market-data capture, replay,
  scheduler и automation не запускались.

## Зафиксированные ограничения

Durable WAL не реализован. При crash между registry append, summary и receipt система
работает по контракту
`FAIL_CLOSED_MANUAL_RECOVERY_NO_AUTOMATIC_ATOMICITY_CLAIM`; автоматическая
crash-atomicity не заявляется.

Universe retention 50% ловит крупное усечение ответа, но не доказывает полноту каждого
отдельного contract ID. Human official `t0` с точностью 60 секунд также не доказывает
выходы `t0`/+5с/+15с/+60с; для seconds-grade evidence нужен отдельный причинный якорь
точностью 1 секунда.
