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
| `src/risk_gate.py` | preflight: план, capability scan, общий gate и claim, свой run, одноразовый токен |
| `src/plan_builder.py` | генератор immutable PlanOnly |
| `src/frozen_plan_bindings.py` | внешний trust-root вне цикла «план пинит runtime → runtime проверяет план» |
| `docs/risk/forbidden-capabilities.txt` | словарь запрещённых возможностей, связан планом |

Коллектора и replay пока нет — это следующий этап.

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
$env:PYTHONPATH="src"; & $py src\risk_gate.py --preflight
```

Читает общий active-run gate и общий writer claim, ничего не пишет кроме токена, и
возвращает ненулевой код при любой блокировке. Каждый блокер перечисляется отдельно —
не только первый.

## Перевыпуск плана

План неизменяем. Замена — осознанное действие: удалить файл, сгенерировать заново,
перепинить trust-root, записать решение в `docs/decisions/`.

```powershell
& $py src\plan_builder.py --write-plan
```

## Границы

Публичные данные, без ключей и подписи. Никаких ордеров ни в каком режиме. Replay —
симуляция по данным на диске. Ни одна цифра отсюда не поддерживает ACCEPT/REJECT
стратегии: для этого нужен отдельный план с чекпоинтом пользователя.
