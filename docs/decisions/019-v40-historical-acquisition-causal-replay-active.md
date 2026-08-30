# 019 — v40 historical acquisition/replay active

Дата: 2026-08-30

## Решение

Активировать immutable PlanOnly v40 для bounded post-hoc acquisition публичных
Bybit/OKX/Gate OHLCV и deterministic offline replay. Gate archive URL закреплён
полным literal, поэтому Plan/SHA и capability preflight проходят до writer claim.

Исторический writer использует общий active-run gate и global writer claim, пишет
только в отдельные append-only raw/manifest/receipt roots и не получает capture
token. Ошибка venue оставляет результат fail-closed и не превращается в успешное
событие.

## Immutable identity

- plan id: `premarket_perp_capture_20260822_v40`;
- status: `HISTORICAL_ACQUISITION_REPLAY_READY_NO_CAPTURE`;
- plan hash: `fbc4456333a2d7886fac3f887d7cca1258dec5091d9671af17ccdddf42eb6c2f`;
- file SHA-256: `e60cc27bcaaaff01576026e8649b3be8aca38a5e9827286001d79dbd5ec9498e`;
- supersedes: immutable v39;
- next event-bound proposal: v41.

## Evidence boundary

- official и proxy timestamps не объединяются;
- post-hoc OHLCV всегда `DESCRIPTIVE_ONLY`;
- минутные свечи могут показать направление/амплитуду bucket-open markout, но не
  доказывают BBO, latency, queue position, partial fill, funding или net PnL;
- execution-grade replay разрешён только для sealed evidence с exact hash,
  receive timestamps, BBO/depth, contract units, fees, funding, mark/index и
  liquidation inputs;
- real orders, private API, keys, leverage, margin и capital запрещены.

## Следующий checkpoint

Сначала получить preregistered historical receipts и descriptive markouts. Для
доказательства исполнимости конкретного будущего листинга отдельно выпустить v41,
связанный с official seconds-grade `t0`, и собрать непрерывное event-window L2.
