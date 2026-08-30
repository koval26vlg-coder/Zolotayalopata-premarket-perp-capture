# 024 — Bybit Full Orderbook: офлайн-синхронизация до event-bound плана

Дата решения: 2026-08-30.

## Решение

Добавить только **непривязанный офлайн-пакет v43** для репетиции нового публичного
Full Orderbook протокола Bybit. Пакет не является PlanOnly, не входит в active v42,
не разрешает network capture и не создаёт replay/execution/acceptance authority.

Активный trust root остаётся без изменений:

- `plan_id = premarket_perp_capture_20260822_v42`;
- `plan_hash = 72acbc1426ddfc5ccb168dd1d75d6414e5af0d30507b80f32fa8d85020691926`;
- `status = HISTORICAL_ACQUISITION_REPLAY_TRUST_BOUND_NO_CAPTURE`.

## Почему появился отдельный протокол

11 августа 2026 Bybit добавил futures к публичным REST и WebSocket Full
Orderbook. Официальный контракт отличается от обычного `orderbook.50`:

- WebSocket topic — `orderbook.full.{symbol}`;
- поток содержит только `delta`, без начального snapshot;
- сначала открывается WS и буферизуются delta, затем берётся REST snapshot
  `GET /v5/market/full_orderbook`;
- связь доказывается точным совпадением `(seq, u)` REST snapshot с одним
  буферизованным delta;
- `u` обязан продолжаться как `u + 1`, а `seq` монотонен, но не обязан быть
  последовательным;
- `u = 1`, reconnect, gap или regression сбрасывают готовность и требуют новой
  синхронизации;
- REST ограничен 10 000 уровнями на сторону, а RPI-ордера не входят в feed.

Источники: [Bybit Full Orderbook WS](https://bybit-exchange.github.io/docs/v5/websocket/public/full-ob),
[Bybit Full Depth REST](https://bybit-exchange.github.io/docs/v5/market/full-ob),
[Bybit V5 changelog](https://bybit-exchange.github.io/docs/changelog/v5).

Старый `orderbook.50` остаётся `DESCRIPTIVE_ONLY`: в его документированном
контракте нет требуемого Full Orderbook REST↔WS predecessor-доказательства.
Generic parser также никогда не объявляет `orderbook.full` непрерывным по одному
нетипизированному `last_sequence`: он оставляет событие `DESCRIPTIVE_ONLY` и
`REST_SNAPSHOT_REQUIRED`. Полномочие доказать bridge принадлежит только отдельному
`BybitFullBookSynchronizer`.

## Что реализовано

1. Byte-exact WebSocket ingress с wall/monotonic clocks, снятыми сразу после
   socket receive, включая handshake remainder; ordinal и bounded fragmentation.
2. Strict parser для `orderbook.full`, REST snapshot и биржевых `cts/ts/seq/u`.
3. Чистый синхронизатор: buffer-before-REST, exact bridge, bounded retries,
   epoch/generation-bound REST attempt с exact path/category, gap/reset/reconnect
   invalidation. Типизированные записи повторно разбираются из raw bytes на ingest
   и отделяются от caller-owned объектов до сохранения во внутреннем состоянии.
4. Evidence chain связывает raw SHA-256 и локальные receive clocks каждого
   использованного сообщения.
5. Статический transcript rehearsal пишет create-only временный bundle:
   `raw-ingress.jsonl`, `sync-decisions.jsonl`, `normalized-depth.jsonl`,
   `manifest.json`, `terminal-receipt.json`.
6. Ошибка parsing/continuity оставляет `STOPPED_INCOMPLETE`; успешная репетиция
   заканчивается только `FULL_BOOK_SYNC_ONLY` с `replay_ready=false`,
   `execution_bundle_ready=false`, `acceptance_capable=false`.
7. На Windows parent и созданный bundle удерживаются no-delete-share handles на
   всём протяжении репетиции. Evidence, manifest и terminal receipt имеют
   handle-bound readback и остаются pinned до финальной проверки.
8. Аварийная очистка намеренно ничего не удаляет по pathname: неполный временный
   bundle сохраняется как forensic artifact, чтобы исключить check/delete race с
   чужим файлом или каталогом.

Даже непротиворечивая книга в этом непривязанном пакете имеет только
`book_structurally_ready=true`; `execution_ready` остаётся `false` до отдельной
внешней проверки request provenance и event-bound authority.

## Что этим не сделано

- network collector и реальный event-window capture не запускались;
- event-bound PlanOnly v43 не выпущен;
- REST endpoint не добавлен в production allow-list;
- capture token, writer claim, private API и ордера не использовались;
- синтетический результат не доказывает fill, slippage или net PnL.

Следующий пакет после появления official seconds-grade `t0` должен отдельно
выпустить event-specific immutable PlanOnly, SHA-привязать transport/sync/writer,
расширить exact HTTP allow-list и только затем проходить видимый capture preflight.
