# Аудит market-data capture из `26da7ae`

Дата: 2026-08-22

Объект: commit `26da7ae530010057717564103c0470b9ebbc94de`

Вердикт: **CHANGES_REQUIRED — capture исключён из PlanOnly v2**

Аудит выполнен без сети и без запуска capture. В исходном checkout commit был чистым,
`HEAD == origin/main == 26da7ae530010057717564103c0470b9ebbc94de`; его 132
offline-теста прошли. Это подтверждает внутренние ожидания тестов commit, но не
закрывает причинность `t0`, полноту evidence и общий single-writer/recovery-контракт.

Проверенные SHA-256:

- `src/capture.py` — `47fc1369e430f14cdc83475aae2d6fe936ce6800914d24a86a5bee7ae8f23ed9`;
- `src/global_market_writer_claim.py` — `764af22b0b2ab24a2c36485fda2719e6c0425a2426c07778693a1be90ed3fc4e`;
- `tests/test_capture.py` — `f8be21ce5c66ff94cc5d6e4d7a4baf933dce28d373302e85867c09880100ea9c`.

## Блокирующие замечания

### P0 — collector нацелен не на официальный spot `t0`

`job_from_event()` читает единые `t0_ts` и `t0_source_class`
(`src/capture.py:223-231`). Перед capture `confirm_t0()` повторно читает:

- Bybit `launchTime` контракта;
- OKX `SWAP.listTime`;
- Gate `create_time` контракта

(`src/capture.py:104-206`). Ни одно из этих полей не доказывает официальный момент
начала spot-торгов. Следовательно, процесс может аккуратно снять не то событие, которое
проверяет гипотеза.

Дополнительно `accept_t0_disagreement` разрешает продолжить работу вопреки конфликту
источников (`src/capture.py:515-548`, CLI в `src/capture.py:577-609`). Для
acceptance-grade capture такой override недопустим.

### P0 — proxy-события допускаются в capture

После расходования токена код выбирает `latest_by_event()` и не требует
`OFFICIAL_ANNOUNCEMENT + official_spot_t0` (`src/capture.py:528-535`). Это позволяет
событию `VENUE_INSTRUMENT_METADATA` стать рабочим якорем. PlanOnly v2 требует обратное:
proxy хранится и материализуется только как descriptive evidence.

### P1 — evidence неполон для проверки исполнимости

Таблица probes содержит только `trades`, `orderbook`, `ticker`
(`src/capture.py:42-88`). Не собираются как отдельные доказательные потоки:

- mark и index price;
- funding и open interest;
- price limits и contract-risk параметры;
- lifecycle/phase/transition;
- официальный spot event и фактический first trade.

Используется REST polling, а manifest прямо признаёт интервалы ненаблюдаемыми
(`src/capture.py:389-394`). Такой набор не доказывает очередь, partial fill,
ликвидационный риск и исполнение на +5/+15/+60 секунд.

### P1 — replay readiness может стать положительным на неуспешных запросах

`per_probe_times` пополняется независимо от успеха запроса
(`src/capture.py:333-352`), а `replay_readiness()` проверяет только крайние timestamps
(`src/capture.py:403-436`). Ошибочные ответы могут создать видимость полного временного
покрытия. Нужен success-aware, per-stream coverage с проверкой stale/out-of-order и
наличия BBO/trades по каждой обязательной точке.

### P1 — бюджет HTTP недооценивает реальные попытки

Одна логическая операция разрешает `max_retries=1` (`src/capture.py:243-247`), но
`requests_made` увеличивается один раз после возвращения fetch
(`src/capture.py:317-352`). Поэтому фактических HTTP-попыток может быть больше
заявленного лимита. Запросы `confirm_t0()` (`src/capture.py:163-178`) вообще не входят
в этот budget, а deadline/stop не перепроверяется между отдельными due probes. Бюджет
и ledger должны считать каждую сетевую попытку.

### P1 — HTTP boundary commit допускает обход

Используемый commit вариант `public_http` сравнивает path по prefix и не требует exact
HTTPS (`src/public_http.py:59-67`), а стандартный opener следует redirect без повторной
проверки destination (`src/public_http.py:114-145`). Поэтому заявленный endpoint не
является реальной границей соединения. Registry v2 уже закрывает этот класс ошибок:
exact HTTPS host/path/query, redirects disabled и fail-closed DNS/IP validation.

### P1 — lineage результата недостаточен

Manifest (`src/capture.py:361-399`) и receipt (`src/capture.py:477-506`) не закрепляют:

- `episode_id`;
- hash/revision конкретной строки registry;
- активные `plan_id`, canonical plan hash и file SHA;
- отдельные contract launch / official spot `t0` / first trade / transition;
- URL и `received_at` официального источника.

Без этого результат нельзя однозначно связать с неизменяемым событием и планом.

### P1 — terminal commit и claim release расположены в неверном порядке

Claim освобождается в `finally` (`src/capture.py:558-571`), а evidence receipt пишется
после освобождения (`src/capture.py:573`). Ошибка или crash между этими действиями
оставляет released claim без terminal receipt. Manifest и receipt пишутся обычным
`write_text`, без temp + fsync + atomic replace (`src/capture.py:397-399, 505`).

Исключение до manifest также не создаёт append-only terminal attempt, а
`final_status="premarket_perp_capture"` не различает успех, частичный результат и
ошибку (`src/capture.py:563-568`).

### P1 — shared writer claim не завершает общий fail-closed контракт

Первичное создание claim через `O_EXCL` и fsync сделано правильно
(`src/global_market_writer_claim.py:66-132`). Но attach/release — read-modify-replace
без отдельного transaction lock (`src/global_market_writer_claim.py:135-191`), нет
process-start identity для защиты от PID reuse, fail-closed stale recovery и
обязательной проверки точного plan hash. CLI позволяет управлять shared claim напрямую.

Между consume токена, выбором события, проверкой каталога и захватом claim остаются
TOCTOU-окна (`src/capture.py:528-557`).

### P1 — runtime и PlanOnly allow-list расходятся

Capture использует OKX `/api/v5/market/ticker` (`src/capture.py:78-81`), тогда как
PlanOnly v2 разрешает только заявленные exact endpoints и не содержит этот путь.
Hardened `public_http` корректно заблокирует такой запрос. Endpoint нельзя добавлять
неявно: нужен новый versioned PlanOnly.

### P1 — прежний immutable PlanOnly был переписан

Commit `26da7ae` изменяет
`docs/plans/premarket-perp-capture-planonly-20260822.json`, сохраняя прежний
`plan_id=premarket_perp_capture_20260822`:

- восстановленный v1: canonical hash
  `aa174438bf457e3a57d94e8f3839ae9a61dbb42504d03f5876825f59a9b2d6c1`, file SHA
  `cac4d34cbc6228fd0a7fc7922afb8ce3b1110388a1df860dba5bbd9f40ae2934`;
- вариант из `26da7ae`: canonical hash
  `6e020bcd2a2f3ba83a9e17eaab1ac578fc72118bc014b27ff69a23a0cc8a2c77`, file SHA
  `cdb3f3b3b1f8c3b64133366a961c30567648395eab24dfb97d3105e94cf1a5ac`.

Это повторное использование immutable identity. Поэтому commit нельзя переносить
целиком поверх восстановленного v1/v2.

## P2

- latency считается разницей wall-clock timestamps, хотя для длительности передан
  monotonic clock (`src/capture.py:321-342`);
- проверка `capture_dir.exists()` и последующий `mkdir()` разделены
  (`src/capture.py:536-539`, `284`), поэтому каталог можно подменить между действиями;
- нет append-only attempts ledger, `RETRY_NEXT_INTERVAL`, duplicate-tick/recovery
  состояния и видимого orchestrator; прямой CLI остаётся доступным.

## Решение

`26da7ae` сохраняется как независимо проаудированный прототип. В PlanOnly v2 его
capture, claim и tests не включаются. Следующая реализация должна строить job только из
верифицированного `official_spot_t0`, закреплять registry/plan lineage, собирать полный
event-window evidence и завершать terminal receipt до освобождения общего claim. Так
как v3 зафиксирован в статусе `AWAIT_CAPTURE_IMPLEMENTATION_AUDIT_NO_CAPTURE`, включение
нового collector потребует отдельного capture-enabled PlanOnly v4, а не изменения
v1/v2/v3.
