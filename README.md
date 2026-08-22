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
| `src/public_http.py` | один HTTP-слой; проверяет allow-list **до** открытия соединения |
| `src/event_registry.py` | реестр листингов: класс источника t0, ревизии, append-only |

Коллектора рыночных данных и replay пока нет — это следующий этап.

## Реестр событий

`t0` — главный датум проекта: гипотеза про секунды вокруг него. Поэтому с каждым
событием едет `t0_source_class`, и события разных классов **никогда не смешиваются**.
Сегодня заполняется только `VENUE_INSTRUMENT_METADATA`; `OFFICIAL_ANNOUNCEMENT`
объявлен, но ничем не заполняется.

Gate отдаёт `create_time` — создание контракта, не обязательно начало торгов, — и
несёт caveat `CONTRACT_CREATION_NOT_TRADING_START`.

```powershell
$env:PYTHONPATH="src"
& $py src\event_registry.py --refresh      # читает метаданные площадок, дописывает изменения
& $py src\event_registry.py --verify       # хеши строк и последовательность ревизий
& $py src\event_registry.py --upcoming --horizon-hours 48
```

Реестр append-only: перенос `t0` дописывается ревизией с прежним значением в
`supersedes`, а не затирает его. Refresh отчитывается о полноте — если курсор площадки
остался живым на потолке страниц, это видно в `truncated_venues`, а не пропадает.

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
