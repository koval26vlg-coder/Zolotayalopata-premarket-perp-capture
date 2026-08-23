# ZolotyayLopata Pre-market Perpetual Capture

Research-only контур для публичных pre-market perpetual данных Bybit, OKX и Gate.
Гипотеза: LONG до официального spot-листинга и описательные выходы на
`t0`/`+5s`/`+15s`/`+60s`.

Проект наблюдает рынок с плечом, но никогда не берёт плечо: private API, ключи,
подпись запросов, ордера, margin, real capital и переводы запрещены. Capture не
запускался и активным PlanOnly v17 не разрешён.

## Состояние v17

- immutable PlanOnly: `premarket_perp_capture_20260822_v17`;
- status: `REGISTRY_QUARANTINE_HARDENED_NO_CAPTURE`;
- разрешены только metadata registry, human official attestation и локальная
  fail-closed registry quarantine;
- `market_data_capture` отсутствует в authorization matrix;
- replay descriptive-only и не поддерживает ACCEPT/REJECT стратегии.

Активные v17 identity: plan hash
`56cc373e25d1710e2fbd6fe5ac039ecb1065dfb1fbe0ead53757ae6342fb731b`, file
SHA-256 `748f116c785aa5a9cc694be394eb355e554eceef7cd44a32d746cf673c406209`.
Они закреплены во внешнем trust root `src/frozen_plan_bindings.py`; v16 сохранён
byte-identical как непосредственный предшественник и никогда не был активирован для
capture.

## Компоненты

| Файл | Назначение |
|---|---|
| `src/project_config.py` | risk contract, paths, endpoint allow-list, write classes |
| `src/risk_gate.py` | Plan/SHA/capability/gate preflight и capture token boundary |
| `src/public_http.py` | HTTPS allow-list до DNS/connect, без redirects и скрытых retries |
| `src/event_registry.py` | append-only registry v3, asset identity, lifecycle и lineage |
| `src/official_attestation.py` | human-attested official spot `t0` с дословным evidence |
| `src/registry_quarantine.py` | локальная CAS-bound quarantine повреждённого поколения |
| `src/capture.py` | bounded collector implementation; активным планом не авторизован |
| `src/replay.py` | строгий offline loader и causal gross BBO markout |
| `src/frozen_plan_bindings.py` | внешний trust root PlanOnly lineage |

## Registry v3

Раздельно хранятся:

| Величина | Смысл |
|---|---|
| `premarket_contract_launch_ts` | запуск pre-market контракта по метаданным venue |
| `official_spot_t0` | официальный старт spot-торгов из анонса |
| `first_trade_ts` | первая наблюдённая публичная сделка |
| `transition_ts` | наблюдённый lifecycle transition |
| `contract_created_ts` | создание контракта |

Только `official_spot_t0` класса `OFFICIAL_ANNOUNCEMENT` для известного
`CRYPTO_TOKEN` может быть capture anchor. Metadata, observed trade/lifecycle и любой
proxy остаются `DESCRIPTIVE_ONLY`.

Asset identity не выводится из одного тикера. Registry хранит `asset_class`, issuer
namespace/id и identity hash. Классы `EQUITY_ISSUER`, `TOKENIZED_EQUITY`,
`TRADFI_OTHER` и `UNCLASSIFIED` не смешиваются с crypto acceptance universe.

Для Gate crypto identity требуется явный и проверенный `contract_type`. Если поле
отсутствует или его значение неизвестно, контракт остаётся `UNCLASSIFIED` и
`DESCRIPTIVE_ONLY`; v17 не заявляет доступность Gate capture-кандидатов.

Discovery состоит из пяти независимых поверхностей:

- Bybit Linear `PreLaunch`;
- Bybit Linear `Trading` для terminal transition уже отслеживаемого контракта;
- OKX SWAP;
- OKX FUTURES;
- Gate USDT Futures.

Каждая поверхность exact-bound к endpoint/query, `rows_path`, native ID, envelope,
lifecycle fields и instrument type. Non-object row, missing/noncanonical или duplicate
ID и malformed lifecycle field являются acquisition failure без mutation. Bybit
дополнительно требует exact `category=linear`, integer/string zero `retCode` (не bool
или float), канонический cursor без trim и совпадение строк с запрошенной
`PreLaunch`/`Trading` поверхностью.

Production completeness проверяется не только по venue aggregate: обязательные
full-universe поверхности `Bybit Trading`, `OKX SWAP`, `OKX FUTURES` и `Gate USDT`
должны быть непустыми и не могут потерять более 50% строк между complete refresh.
Только `Bybit PreLaunch` может законно быть пустой.

Полный relevant identity set и его hash фиксируются по каждой поверхности. Пропавший
tracked ID без явного terminal evidence — acquisition failure без mutation. OKX
`xperp` и `normal` для уже tracked поколения распознаются как cross-surface
transition. Gate `launch_time` — срок действия контракта, не старт торговли;
`create_time` — только создание, а `in_delisting=true` является явным terminal
DELISTING.

Исторические terminal-строки не создают local episode: Closed/xperp/delisted evidence
привязывается только к поколению, которое раньше наблюдалось active или scheduled.
Untracked terminal row игнорируется без выделения high-water. Bybit v17 опрашивает
только `PreLaunch` и `Trading`; отмена/закрытие, отсутствующие в обеих поверхностях,
остаются acquisition failure, а не inferred terminal event. Отдельная terminal-status
поверхность потребует нового immutable PlanOnly.

Причина явного terminal lifecycle сохраняется отдельной append-only observation с
`lifecycle_phase`. Terminal ids последнего complete refresh читаются на следующем
refresh только как classification memory: они не входят в required/relevant/
completeness/active/high-water authority. При одновременных OKX terminal-поверхностях
exact `preMktSwTime` имеет приоритет над detection proxy, включая уже expired xperp.

Mutation receipt закрепляет полный pre-hash record: registry/summary lineage,
`mutation_run_id`, active/high-water state, venue/surface counts, relevant IDs/hashes и
terminal IDs. Валидатор PlanOnly сверяет этот словарь целиком.

Текущий official-attestation producer фиксирует `t0` с точностью 60 секунд, тогда как
секундная гипотеза требует не хуже одной секунды. Поэтому даже структурно полный
capture этого поколения остаётся `DESCRIPTIVE_ONLY_PRECISION_GT_ONE_SECOND`; повышение
authority требует нового producer и следующего immutable PlanOnly.

REST books OKX не возвращает `instId`, поэтому инструмент привязан к exact URL/query и
их hash, записанным рядом с payload. Gate orderbook поддерживает документированный
формат уровней `{p,s}`. Gate futures ticker не содержит exchange timestamp; такой
payload остаётся optional descriptive и не участвует в causal readiness, а не получает
время приёма как подмену биржевого времени. Для Gate readiness требуются timestamped
trades и orderbook.

Legacy registry v2 остаётся byte-identical migration source и закреплён SHA/head/
mutation receipt в PlanOnly. Production v3 имеет отдельный путь и не был наполнен.

## Replay v2

Replay не открывает сеть, не берёт writer claim и не пишет project artifacts.

Причинные часы — `received_ts`. Для target выбирается первая валидная BBO-точка на
target или после него, не позднее одной объявленной каденции. Pre-target fallback,
интерполяция и bracket inference запрещены. `exchange_ts` используется только для
staleness/future-skew checks.

Вход маркируется по ask, выход — по bid. Результат — gross BBO markout, а не fill,
очередь, slippage, fees, funding, liquidation или net PnL.

Production evidence принимается только если одновременно проверены:

- каталог — строгий потомок `capture_root` именно того исторического PlanOnly,
  который авторизовал исходный capture;
- immutable receipt в `docs/evidence/<capture_id>.json`;
- raw manifest/samples SHA-256 и canonical receipt hash;
- точные Plan identity и implementation hashes;
- одинаковая manifest/receipt lineage;
- исторический registry prefix и точная mutation receipt;
- official unsuperseded crypto anchor.

Synthetic fixture требует явного режима `SYNTHETIC_DESCRIPTIVE_ONLY`; production
capture нельзя понизить до synthetic.

## Registry quarantine

Quarantine — отдельный локальный write class без сетевых запросов. Она требует initial
и commit preflight, exact operator CAS по registry/summary/raw receipt bytes, registry
O_EXCL lock и global market-writer claim. Архив публикуется same-volume durable move и
полностью перечитывается вместе с PREPARED и exact entry set до source mutation.
Receipts/summary (если присутствуют) и registry переводятся в retained `.deactivated`
tombstones; registry — последним. Tombstone names/bytes закреплены в terminal state.
После `SOURCE_DEACTIVATED` canonical registry, summary и receipt-directory обязаны
отсутствовать независимо от того, существовали ли summary/receipts в исходном
поколении; любое позднее появление означает fail-closed manual recovery и сохранение
оставшихся locks.
После двух exact status checks global claim снимается первым, а registry lock последним
durable move превращается в terminal proof; после него нет fallible I/O. Automatic
recovery запрещён — неоднозначное состояние и оставшиеся locks требуют manual recovery.

Наличие реализации не означает, что production quarantine выполнялась в этом пакете.

## Проверки

```powershell
$py = "C:\Users\koval\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$env:PYTHONPATH = "src"

& $py -m unittest discover -s tests -v
& $py src\risk_gate.py --plan-check
& $py src\risk_gate.py --capability-scan
```

Production replay (строгий loader используется по умолчанию):

```powershell
& $py src\replay.py --replay <capture-dir> --horizons 0,5,15,60
```

Synthetic mode доступен только программному тестовому API и используется offline-suite;
CLI не позволяет случайно понизить production capture до fixture.

## Preflight

```powershell
& $py src\risk_gate.py --preflight --write-class metadata_registry --run-id <id>
& $py src\risk_gate.py --preflight --write-class official_attestation --run-id <id>
& $py src\risk_gate.py --preflight --write-class registry_quarantine --run-id <id>
```

Preflight не запускает writer автоматически. Реальные refresh, attestation и
quarantine являются отдельными операциями. Capture требует будущего immutable PlanOnly,
который явно добавит `market_data_capture`, плюс отдельное разрешение пользователя на
видимый запуск.

## Immutable lineage

Опубликованы v1–v17. Все прежние планы остаются на диске и проверяются по file SHA,
canonical plan hash и identity. v15 сохранён byte-identical и непосредственно
superseded v16; v16 byte-identical и непосредственно superseded v17. Ни один старый
PlanOnly не переписывался.

См. `AGENTS.md` и `docs/decisions/014-v17-registry-replay-quarantine-remediation.md`.
