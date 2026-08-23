# Решение 009: final capture authority и evidence hardening v8

Дата: 2026-08-23

Статус: **SUPERSEDED_BY_V9**. Этот документ фиксирует опубликованный v8 и не
переопределяет активный план.

Активный immutable PlanOnly: **v8**, `plan_id`
`premarket_perp_capture_20260822_v8`, `plan_hash`
`fb9a44f17ca2f3ffcb8f9ef87c7e9ad42684bfd80ad03dfe5ad48d05f34d223f`,
file SHA-256
`045c614865cc0744025b93eb1ee5ef1de2d093d8680a1a9d3f2e64909839ced5`.
v1-v7 сохранены побайтово и входят в проверяемую retired lineage.

## Решение

Повторный независимый review v7 принят как новый RED-пакет. Каждый воспроизведённый
дефект закрыт offline regression-тестом до перевыпуска v8. Статус остаётся
`CAPTURE_IMPLEMENTATION_AUDIT_GREEN_NO_CAPTURE`: план фиксирует реализацию, но не
авторизует `market_data_capture`, collector, scheduler или replay.

## Registry и attestation

- due-selection использует один конфигурационный контракт: target равен
  `official_spot_t0 - window_before_t0_sec`, grace равен 30 с до target и 5 с после;
- quoted sentence, time и symbol хранятся как `VERBATIM_UTF8`; нормализация, управляющие
  символы и переписывание внешних пробелов запрещены;
- каждая mutation получает O_EXCL receipt в локальной hash-chain, привязанной к registry
  tail, summary, active lifecycle state, plan и run;
- это tamper-evident crash-recovery evidence, а не подпись и не доказательство
  криптографической аутентичности;
- caught failure до immutable commit откатывает registry и summary под lock;
- повтор идентичной attestation остаётся идемпотентным даже после lead boundary;
- новая attestation и capture обязаны ссылаться на текущую active lifecycle generation.

## Capture authority и artifacts

- public `run_capture` принимает только injected synthetic/offline fetch, запрещён в
  PlanOnly-bound capture root и всегда выдаёт `SYNTHETIC_OFFLINE_ONLY`;
- live HTTP fetch существует только в `capture_event` после token, writer claim,
  post-claim gate, plan и registry-lineage revalidation;
- lower-level loop возвращает неклассифицированный draft и не имеет boolean-переключателя
  acceptance; live manifest коммитится только локальным одноразовым permit;
- Gate orderbook без echoed contract принимается только при совпадении точного
  on-wire URL/query identity с contract текущего capture job;
- samples и manifest создаются через exclusive create; manifest не заменяется;
- receipt самостоятельно повторно хеширует `samples.jsonl` и требует совпадения с
  `manifest.output_sha256`.

## Проверка

- полный offline-suite: **346/346 OK**;
- PlanOnly: `PLAN_OK`;
- capability scan: `CAPABILITY_SCAN_CLEAN`;
- отрицательный capture preflight: `BLOCK`, единственный blocker — no-capture status;
- active gate при проверке: `READY_FOR_POSTPROCESS` с разрешением только bounded
  implementation/analysis;
- v7 file SHA-256 повторно подтверждён;
- network, registry refresh, official attestation, market-data capture, replay,
  scheduler и automation не запускались.

## Открытая evidence-граница

Human-attestation из минутного анонса имеет точность 60 с. Она может направить окно,
но не способна доказать выходы `t0`/+5с/+15с/+60с с секундной точностью. Seconds-grade
результат потребует отдельного официального или causally observed якоря точностью 1 с,
нового PlanOnly checkpoint и отдельного разрешения пользователя на видимый capture.
