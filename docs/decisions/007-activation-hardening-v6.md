# Решение 007: activation hardening без активации capture

Дата: 2026-08-23

PlanOnly: опубликованный immutable checkpoint **v6**, `plan_id` `premarket_perp_capture_20260822_v6`,
`plan_hash` `b2e07bd3475b57b4d815bf1adca8dbd5b52f120d4b544ea10d3227186682ab2e`,
file SHA-256 `0be95c2a4a60e6457697bfa0bf612ada7b0e63efdd903abafb7ab9c77f1bbe6f`.
Тесты: 243 → **283**. Capture, metadata refresh и official attestation не запускались.

Статус после независимого review: **SUPERSEDED_BY_V7**. Сам v6 не изменён и остаётся
в lineage побайтово; обнаруженные activation-блокеры исправлены только новым планом.

## Почему v6 не стал активным итогом

Review воспроизвёл оставшиеся fail-closed дефекты: caller-controlled receipt clock,
append до полной semantic validation, lifecycle generation без durable active-state,
слишком ранний capture selector, default live fetch, слабую venue freshness/identity
валидацию, truncating evidence writes, post-claim TOCTOU, terminal accounting и
перезапись/уничтожение одноразового токена неверным caller. Поэтому результаты ниже
фиксируют состояние v6 на момент публикации, а не текущую активную гарантию.

## Решение

Пакет закрывает реализацию capture-контура, но не открывает его. Статус v6 остаётся
`AWAIT_CAPTURE_IMPLEMENTATION_AUDIT_NO_CAPTURE`; `activation_gate.capture_authorized`
равен `false`. Поэтому `market_data_capture` не входит в разрешённые write-class и
одноразовый токен нельзя получить ни через preflight, ни прямым вызовом mint.

Это разделяет два разных утверждения:

1. реализация и её проверки готовы к независимому аудиту;
2. live public-data capture разрешён к запуску.

Второе из первого не следует. Для активации нужен новый immutable PlanOnly/checkpoint
и отдельное разрешение пользователя на видимый запуск.

## Official t0 и реестр

- lifecycle generation входит в identity эпизода; official t0 обязан ссылаться на
  существующий metadata-эпизод той же генерации;
- `premarket_contract_launch_ts`, `official_spot_t0`, `first_trade_ts` и
  `transition_ts` остаются разными величинами и потоками;
- proxy-классы descriptive-only и не проходят capture selector;
- official attestation имеет отдельные write-class и action, а не использует authority
  metadata refresh;
- verifier перепроверяет официальный домен, UTC receipt, автора, точную цитату,
  quoted symbol и соответствие quoted time официальному t0;
- OKX native id с суффиксом `-SWAP` сопоставляется с соответствующей spot-парой, а не
  отвергается ложным сравнением строк;
- append official row и новый summary receipt выполняются как одна mutation под
  registry lock; реестр и summary несут точные SHA/tail provenance.

## Capture evidence

- только структурно валидный venue payload считается успешным наблюдением;
- failed/invalid запросы остаются в denominator, а 100% failure даёт
  `STOPPED_INCOMPLETE`;
- replay readiness требует causal `request_ts`/`received_ts`, все три probe,
  двустороннее burst-покрытие, ограниченный worst gap и evidence на заранее
  зафиксированных выходах `0/5/15/60`;
- poll использует `max_retries=0`: один logical poll равен одному counted transport
  attempt;
- request/runtime ceilings и capture root нельзя расширить программным аргументом;
  `run_id` не может выйти из PlanOnly-bound namespace;
- manifest и receipt несут episode, official-record/source, registry/summary/tail и
  PlanOnly lineage; перед writer claim она сверяется с текущим планом;
- samples fsync-ятся перед atomic manifest, evidence receipt и terminal run record
  записываются до release global writer claim; exception архивирует claim как
  `FAILED_EXCEPTION`.

## Control plane и HTTP

- capture token связан с run/event/source, PlanOnly id/hash, resolved paths, gate и
  capability report hash и потребляется атомарно один раз;
- на consume повторно проверяются текущие plan/path/capability/gate/claim/run record;
- dynamically assembled URL проверяется до DNS/open;
- OKX `code != 0` считается venue error даже без поля `data`;
- capture transport не прячет retries вне заявленного бюджета;
- lineage verifier проверяет наличие, file SHA, identity и canonical hash каждого
  опубликованного плана v1–v5;
- safety suite добавлен на `windows-latest`, потому что read-only attributes и checkout
  bytes являются частью реального Windows-контура проекта.

## Проверка

- `python -m unittest discover -s tests -v`: **283/283 OK**;
- `python src/risk_gate.py --plan-check`: `PLAN_OK`;
- capability scan: `CAPABILITY_SCAN_CLEAN`;
- отрицательный capture preflight: `BLOCK`, причина — активный no-capture status;
- `git diff --check`: чисто.

## Что намеренно не сделано

Не выполнялись сетевые запросы площадок, refresh рабочего реестра, human attestation,
capture, replay, scheduler или automation. Пустой реестр не выдаётся за собранную
выборку, а зелёные offline-тесты не выдаются за доказательство торговой стратегии.
