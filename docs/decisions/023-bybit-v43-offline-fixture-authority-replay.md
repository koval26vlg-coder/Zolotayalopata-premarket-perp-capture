# 023 — Bybit v43 offline fixture authority and replay

Дата: 2026-08-30

## Решение

Подготовить и сквозным synthetic fixture проверить будущий Bybit L2 путь, не
активируя production capture и не выпуская generic/placeholder event-bound v43.

Активным остаётся immutable v42
`premarket_perp_capture_20260822_v42` с Plan hash
`72acbc1426ddfc5ccb168dd1d75d6414e5af0d30507b80f32fa8d85020691926`.
Новые модули не входят в его `BOUND_RUNTIME_FILES`, не имеют launcher/scheduler и
не получают write-class, capture token или global writer claim.

## Реализованный offline путь

1. `bybit_l2_writer_v43.py` принимает только статическую in-memory fixture-ленту,
   сохраняет исходные публичные WS bytes, receive/monotonic clocks, connection
   epochs, append-only hashes и causal normalized snapshots во внешний одноразовый
   temp-каталог.
2. `v43_fixture_authority.py` проверяет canonical exact-layout bundle с внешним raw
   SHA плана, event/official-record, arming, proposal, lifecycle, one-shot approval,
   attempt, terminal-before-release claim, manifest, receipt и capture lineage.
   Успех выдаёт только opaque process-local capability без request/receipt fields.
3. `v43_verified_replay.py` принимает только этот одноразовый handoff. Authority
   извлекает его до callback, переносит request/receipt во второй одноразовый store
   и передаёт callback только exact-type opaque record. Callback первым действием
   потребляет этот record; `finally` уничтожает остаток при возврате или исключении.
   Только после этого fixed execution model получает synthetic calculation input,
   а результат жёстко маркируется `FIXTURE_REHEARSAL_ONLY`,
   `acceptance_capable=false`, `production_external_authority_verified=false`,
   `network_allowed=false`, `orders_allowed=false`, `orders_created=0`.
4. Прямой caller-supplied sealed request по-прежнему завершается
   `NOT_RUN_TRUSTED_EVIDENCE_LOADER_REQUIRED`; production
   `l2_evidence.load_verified_execution_request()` по-прежнему требует реальную
   внешнюю event-bound authority.

## Найденная Bybit continuity-граница

Текущая публичная Bybit orderbook schema содержит update id, но используемый
normalizer не может доказать predecessor каждого delta. Поэтому writer не подставляет
`last_sequence` как якобы биржевой predecessor. Такой delta сохраняется в raw tape,
получает `CONTINUITY_UNVERIFIABLE`, не создаёт execution-ready snapshot и переводит
tape в `DESCRIPTIVE_ONLY / STOPPED_INCOMPLETE`.

Даже gap-free fixture tape имеет `completion_scope=FIXTURE_L2_TAPE_ONLY`,
`execution_bundle_ready=false` и `replay_ready=false`: writer сам не собирает fixed
offset coverage, contract cost, funding и mark/index evidence. Слово `COMPLETED`
описывает только целостность L2 tape, а не доказанную исполнимость сделки.

## Что доказано fixture-тестом

Synthetic Bybit sealed bundle с полными entry/exit snapshots, cost, funding,
mark/index и lineage проходит цепочку:

`external Plan SHA -> authority cross-bindings -> one-shot handoff -> fixed replay`

и выдаёт четыре заранее зарегистрированных горизонта `t0/+5/+15/+60s`, учитывая
depth walk, partial/unfilled, fees, funding и liquidation stress. Это доказательство
готовности программного контура, а не торговой гипотезы и не market evidence.

## Оставшиеся activation blockers

1. Один реальный `CRYPTO_TOKEN` event с official seconds-grade `t0` и достаточным lead.
2. Формально reviewed Bybit continuity basis либо transport evidence, доказывающее
   отсутствие потерь/перестановок между snapshot и delta и сохраняющее исходные bytes.
3. Production writer для полного L2 event window плюс contract cost, funding,
   mark/index и заранее фиксированных causal offsets.
4. Новый immutable event-specific v43 с exact runtime SHA, registry prefix,
   arming/proposal/lifecycle, WSS binding, output namespace и one-attempt authority.
5. Отдельное разрешение пользователя на один видимый public-data capture.

До закрытия всех пяти пунктов сеть этим пакетом не запускается, production capture
не авторизован, net PnL остаётся fixture-only, а реальные и биржевые paper/testnet
ордера запрещены.
