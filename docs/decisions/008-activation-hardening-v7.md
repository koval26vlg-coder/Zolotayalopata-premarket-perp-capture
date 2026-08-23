# Решение 008: causal capture hardening v7 без запуска capture

Дата: 2026-08-23

Статус: **SUPERSEDED_BY_V8**. Immutable PlanOnly v7: `plan_id`
`premarket_perp_capture_20260822_v7`, `plan_hash`
`0fb59db93f3f52a47614e080e04d59b77fbdbbc990da888b291b4cc832330e59`,
file SHA-256
`6ac94a64be7a83835b764115d1805f05d2194ac060c4b4df7ddfb768bb5ab75e`.
v6 и v7 сохранены побайтово и переведены в retired lineage.

## Решение

Независимый review v6 принят как RED-воспроизведение, а не как основание для merge.
Каждый найденный на этом этапе обход закреплён offline regression-тестом и закрыт в runtime. v7 имеет
статус `CAPTURE_IMPLEMENTATION_AUDIT_GREEN_NO_CAPTURE`: реализация прошла аудит, но
`market_data_capture` по-прежнему отсутствует в разрешённых write-class.

Финальный повторный review после публикации v7 нашёл дополнительные границы: cadence
selection расходилась с PlanOnly, quotation evidence допускала переписывание пробелов,
registry summary не имела отдельной immutable mutation-chain, public synthetic capture
мог быть неправильно классифицирован внутренним флагом, Gate orderbook не был связан с
точным on-wire request, а manifest/receipt не обеспечивали требуемую эксклюзивность и
повторное хеширование samples. Поэтому v7 сохранён как промежуточный checkpoint, но не
остаётся активным trust-root.

## Registry и official provenance

- receipt time берётся внутренними часами writer; caller не может передать историческое
  `now_ts`;
- `received_at_utc <= official_spot_t0`, lead пересчитывается и обязан покрывать окно;
- полная candidate-chain проверяется до append/fsync;
- preflight связан с точными schema/action и текущими trust-root plan id/hash;
- selection невозможен раньше receipt и происходит только у границы capture window;
- refresh сохраняет active contract ids в summary, поэтому disappear/reappear создаёт
  новую lifecycle generation;
- OKX dated FUTURES, `-SWAP` и spot symbol сопоставляются по нормализованному market;
- snapshot registry+summary читается под lock, проверяется из одних bytes и затем
  перепроверяется на изменение;
- повтор той же attestation идемпотентно возвращает исходный official record hash.

## Capture evidence

- lower-level `run_capture` не имеет default live fetch; live path остаётся только у
  gate/token/claim entrypoint;
- venue payload проверяет instrument identity, BBO/depth/trades, exchange timestamp и
  freshness; HTTP success без market evidence не считается sample;
- coverage, burst gaps и fixed exits считаются по `received_ts`, а не request start;
- пустой probe set и `t0_precision_sec > 1` fail closed;
- metadata и poll используют `max_retries=0`, все transport attempts входят в manifest;
- capture id и все artifact paths проверяются; samples/receipt создаются exclusive и
  никогда не truncate-ятся;
- после writer claim повторно проверяются gate и та же registry lineage до первого HTTP;
- receipt failure оставляет terminal status `FAILED_EXCEPTION`; если terminal record не
  фиксируется, claim не освобождается и остаётся видимым stale/fail-closed evidence.

## Token semantics

- mint использует `O_EXCL`, поэтому outstanding token нельзя перезаписать;
- JSON/schema/binding/caller/run/event/source/expiry проверяются до mutation;
- неверный caller не уничтожает валидный token;
- только после проверки caller token забирается atomic rename, затем повторно
  проверяются plan, paths, capability scan, gate, claim и run record.

## Точность t0

Human-attestation из текста анонса фиксирует минутную точность (`60 с`). Для проверки
выходов `t0`/+5с/+15с/+60с replay readiness требует секундный якорь (`1 с`). Поэтому
v7 честно разрешает использовать минутный t0 для описательного наведения окна, но не
объявляет такой capture seconds-grade. Это открытая evidence-задача, а не скрытое
округление.

## Проверка

- полный offline-suite: **317/317 OK**;
- PlanOnly: `PLAN_OK`;
- capability scan: `CAPABILITY_SCAN_CLEAN`;
- отрицательный capture preflight: `BLOCK`, причина — no-capture status;
- active gate при проверке: `READY_FOR_POSTPROCESS`;
- `git diff --check`: чисто.

Сетевые запросы площадок, registry refresh, official attestation, market-data capture,
replay, scheduler и automation не запускались. Пустая production-выборка не выдаётся за
доказательство исполнимости стратегии.
