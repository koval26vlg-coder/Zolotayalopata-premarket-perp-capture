# ZolotyayLopata Pre-market Perpetual Capture

Research-only захват публичных market-data вокруг листинга бессрочных фьючерсов на
Bybit, OKX и Gate. Цель — переиграть офлайн гипотезу «вход до листинга, выход на
`t0`/+5с/+15с/+60с».

Проект **наблюдает** рынок с плечом и **никогда не берёт** плечо. Отдельный
репозиторий существует именно ради этой границы; она проверяется механически. См.
`AGENTS.md` и `docs/decisions/001-separate-repository-and-risk-gate.md`.

Capture ещё не запускался.

## Что уже есть

| | |
|---|---|
| `src/project_config.py` | пути, общий контроль, `ALLOWED_ENDPOINTS`, `RISK_CONTRACT` |
| `src/capability_scan.py` | статический запрет ордеров, подписи, ключей, смены плеча и URL вне allow-list |
| `src/risk_gate.py` | write-class preflight: план, capability scan, canonical paths, общий gate; claim/token только для capture |
| `src/plan_builder.py` | генератор immutable PlanOnly |
| `src/frozen_plan_bindings.py` | внешний trust-root вне цикла «план пинит runtime → runtime проверяет план» |
| `docs/risk/forbidden-capabilities.txt` | словарь запрещённых возможностей, связан планом |
| `src/public_http.py` | exact HTTPS allow-list, query policy, DNS-validated IP binding, redirects disabled |
| `src/event_registry.py` | v2 event registry: отдельные timestamp streams, hash-chain, O_EXCL lock |

Коллектора рыночных данных и replay пока нет — это следующий этап.

## Реестр событий

В v2 generic `t0` отсутствует. Один lifecycle episode хранит независимо:
`premarket_contract_launch_ts`, `official_spot_t0`, `first_trade_ts`,
`transition_ts` и отдельный `contract_created_ts` для Gate. Источник является частью
идентичности stream; один source class не может переписать другой.

Только `official_spot_t0` из `OFFICIAL_ANNOUNCEMENT` может быть capture anchor.
Metadata/observed timestamps — `DESCRIPTIVE_ONLY`. Официальный resolver ещё не
реализован, поэтому acceptance-grade capture сейчас структурно не выбирается.

Gate отдаёт `create_time` — создание контракта, не обязательно начало торгов, — и
несёт caveat `CONTRACT_CREATION_NOT_TRADING_START`.

```powershell
$env:PYTHONPATH="src"
& $py src\event_registry.py --refresh --run-id metadata_20260822T120000Z
& $py src\event_registry.py --verify       # хеши строк и последовательность ревизий
& $py src\event_registry.py --upcoming --horizon-hours 48 --source-class OFFICIAL_ANNOUNCEMENT
```

Реестр append-only: глобальная `record_hash` chain и независимые
`stream_revision/supersedes_record_hash` chains обнаруживают подмену, fork и orphan.
Summary receipt обязателен для непустого production registry и закрепляет tail;
capture selector повторно проверяет тот же snapshot. Refresh сначала stage-ит все три venue; missing,
malformed, cursor-loop/cap дают `INCOMPLETE_NO_REGISTRY_WRITE`. Запись защищена
отдельным atomic registry lock.

## Проверки

```powershell
$py = "C:\Users\koval\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

& $py -m unittest discover -s tests
$env:PYTHONPATH="src"; & $py src\risk_gate.py --plan-check
& $py src\risk_gate.py --capability-scan
& $py src\risk_gate.py --print-config
```

`--plan-check` сверяет план с trust-root и SHA-256 каждого связанного файла, затем
прогоняет capability scan. Правка runtime без перевыпуска плана роняет его — это и
есть требуемая дисциплина.

## Preflight

```powershell
$env:PYTHONPATH="src"
& $py src\risk_gate.py --preflight --write-class metadata_registry --run-id metadata_1
& $py src\risk_gate.py --preflight --write-class market_data_capture --run-id capture_1
```

Оба класса проверяют PlanOnly, capability scan, exact resolved paths и общий active-run
gate. Metadata refresh не занимает market-data claim и не получает токен. Текущий
PlanOnly v3 имеет статус `...NO_CAPTURE`, поэтому второй вызов обязан вернуть `BLOCK`
и не создать токен. Будущий capture сможет получить одноразовый токен только после
отдельного capture-enabled PlanOnly.

## Перевыпуск плана

План неизменяем. Замена всегда получает новый versioned path и `plan_id`, ссылается на
предыдущий артефакт через `supersedes_*`, затем отдельно перепинивается trust-root.
Старый файл не удаляется и не перезаписывается.

```powershell
& $py src\plan_builder.py --write-plan
```

## Границы

Публичные данные, без ключей и подписи. Никаких ордеров ни в каком режиме. Replay —
симуляция по данным на диске. Ни одна цифра отсюда не поддерживает ACCEPT/REJECT
стратегии: для этого нужен отдельный план с чекпоинтом пользователя.
