# 022 — v43 L2 preparation without placeholder activation

Дата: 2026-08-30

## Решение

Подготовить новые, additive и неавторитетные primitives для будущего event-bound
v43, но не выпускать placeholder PlanOnly и не включать capture без одного реального
crypto-event с official seconds-grade `t0`.

Активным остаётся immutable v42
`premarket_perp_capture_20260822_v42` со статусом
`HISTORICAL_ACQUISITION_REPLAY_TRUST_BOUND_NO_CAPTURE`. Новый PlanOnly v43, capture
token, network capture, scheduler и replay/net-PnL в этом пакете не создаются.

## Подготовленные primitives

- `public_ws.py`: bounded public RFC 6455 transport с injected exact endpoint
  allow-list, TLS/SNI, frame/message limits, overall operation deadline,
  control-frame budget, poison-on-protocol-error, ping/pong и без auth/reconnect loop;
- `l2_book.py`: pure sequence-aware snapshot/delta state, connection epochs,
  non-regressing clocks, bounded decisions, cumulative frame lineage, persistent
  gap taint и causal BBO/depth snapshots;
- `venue_ws_v43.py`: candidate-only exact public WSS profiles, subscription builders
  и fail-closed normalizers для Bybit/OKX/Gate;
- `v43_event_binding.py`: create-only structural candidate; он проверяет exact active
  v42 identity, official source, fixed bounds/caps, Bybit contract-bound WSS topics,
  fresh lifecycle и short-lived one-shot review receipt, но явно возвращает
  `external_authority_verified=false` и `issuable=false`;
- `l2_evidence.py`: internal-chain inspector для synthetic/candidate bundle. Публичный
  production loader всегда возвращает
  `EXTERNAL_V43_AUTHORITY_VERIFIER_REQUIRED`, поэтому ordinary dict не может обойти
  существующий `NOT_RUN_TRUSTED_EVIDENCE_LOADER_REQUIRED` в execution engine.

Все перечисленные модули намеренно отсутствуют в v42 `BOUND_RUNTIME_FILES` и не имеют
launcher/caller под активным планом.

## Почему не выпускается v43 сейчас

Event-bound PlanOnly обязан содержать точную идентичность одного события: venue,
contract, spot symbol, official record/source, official `t0`, registry prefix и
mutation receipt, current arming/proposal head, свежий lifecycle/contract snapshot,
exact WSS connection/channel profile и capture window. Сейчас такого eligible события
и durable material в production paths нет. Generic placeholder позволил бы менять
authority после появления результата и поэтому запрещён.

## Найденные и закрытые границы

Первый candidate builder принимал self-rehashed lineage, localhost/private WSS,
произвольные topics, изменённые capture caps и древний approval. Эти входы теперь
отсекаются отдельным adversarial test suite; результат всё равно остаётся
неавторитетным до проверки durable heads.

Первый evidence loader называл internally consistent каталог `VERIFIED`, хотя тот не
был привязан к внешнему PlanOnly/registry/arming/claim authority. Теперь внутренний
inspection и production trust handoff разделены: production всегда fail-closed.
Windows no-reparse/TOCTOU proof, independent terminal receipt и claim-release archive
остаются обязательными activation blockers.

## Exact activation checklist

1. Получить один `CRYPTO_TOKEN` event с official seconds-grade `t0` и достаточным lead.
2. Перепроверить official source и current registry prefix, записать durable arming и
   event-bound proposal штатными v42 writers.
3. Снять свежий lifecycle/contract snapshot и провести no-write venue schema rehearsal.
4. Выпустить новый immutable event-specific v43, связать все runtime SHA и расширить
   capability scan на `wss://` exact host/path/channel profiles.
5. Rebind external trust root отдельным review, получить explicit one-attempt approval
   и одноразовый capture token.
6. Запустить один visible, one-writer continuous capture; raw frames, gaps, reconnect
   epochs, manifest, terminal receipt и claim release записать append-only.
7. Проверить bundle против внешних durable artifacts и выполнить replay внутри trusted
   boundary. Только после этого считать fill, fees, slippage, funding, liquidation и
   net PnL для `t0/+5/+15/+60s`.
8. После terminal event перевыпустить no-capture plan; v43 authority не переиспользовать.

До выполнения всего списка `capture_authorized=false`, `orders_created=0`, private API,
venue paper/testnet, реальные ордера, margin и leverage changes запрещены.
