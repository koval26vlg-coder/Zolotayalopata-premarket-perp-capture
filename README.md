# ZolotyayLopata Pre-market Perpetual Capture

Research-only контур для публичных pre-market perpetual данных Bybit, OKX и Gate.
Гипотеза: LONG до официального spot-листинга и описательные выходы на
`t0`/`+5s`/`+15s`/`+60s`.

Проект наблюдает рынок с плечом, но никогда не берёт плечо: private API, ключи,
подпись запросов, ордера, margin, real capital и переводы запрещены. Capture не
запускался и активным PlanOnly v38 не разрешён. v38 сохраняет bounded-поиск
официальных announcement-кандидатов, добавляет at-most-once локальное уведомление,
no-capture arming точного official `t0` и create-only предложение event-bound v39,
а также fixture-only репетицию и fail-closed recovery зависшего arming-lock/прерванного
proposal;
локальная fail-closed paper simulation остаётся отдельным offline-контуром. Новый
no-model scheduler делает только дешёвый пяти-минутный due-check; фактическая сеть
работает по adaptive cadence 6ч/3ч/1ч/5мин.

## Состояние v38

- immutable PlanOnly: `premarket_perp_capture_20260822_v38`;
- status: `OFFICIAL_T0_ARMING_READY_NO_CAPTURE`;
- разрешены только metadata registry, human official attestation и локальная
  fail-closed registry quarantine, offline paper-readiness и bounded official-index
  discovery без article-body fetch;
- scheduler не использует модель или токены: Windows Task Scheduler просыпается раз в
  пять минут, а `NOT_DUE` не делает preflight, claim, сеть, запись или stdout;
- discovery читает только точные публичные index endpoints Bybit, Bitget и KuCoin;
  время index-записи — publication time, не `official_spot_t0`;
- candidate store отклоняет любое поле вне точной схемы, повторно связывает article URL
  с официальным host/path конкретного `listing_venue` и не допускает повышения
  `identity_authority`, `registry_write` или `human_attestation_required`;
- первый current-кандидат может показать один локальный Windows toast; intent
  fsync-ится до единственного `Show`, а неопределённый результат не пересылается
  автоматически и не становится `t0`;
- arming требует current `CRYPTO_TOKEN`, `OFFICIAL_ANNOUNCEMENT`, точность ровно одну
  секунду, точное contract/spot mapping и не менее полного pre-listing окна; receipt
  append-only и не содержит capture token;
- генератор v39 создаёт только детерминированное предложение, не новый активный план:
  trust root, capture authority и scheduler он не меняет;
- fixture-rehearsal проходит candidate alert, human attestation validation, arming и
  proposal только во временном каталоге; сеть, toast, production writes, capture token,
  capture и ордера отсутствуют; launcher и сам runtime до любой временной записи
  независимо проверяют active Plan/SHA и capability scan;
- conclusively dead same-host arming-lock архивируется lossless и допускает одну
  повторную попытку; live/remote/unknown/malformed lock остаётся fail-closed;
- proposal публикуется только после fsync временного stage через atomic no-replace;
  interrupted stage архивируется, валидный existing final читается идемпотентно, а
  повреждённый или конфликтующий final не изменяется; crash после создания архива
  возобновляется только когда archive и residue являются тем же non-symlink inode;
- тикер в заголовке — только heuristic candidate. Cross-venue связь требует
  отдельной human `SAME_UNDERLYING` аттестации с дословным названием актива;
- точность official `t0` выводится только из дословного времени источника: minute-only
  остаётся descriptive, а acceptance candidate требует явных секунд;
- `event_registry.py --candidate-status` выполняет только read-only проверку и
  повторяет authoritative selector без token, claim, сети или права на capture;
- perpetual venue и venue официального spot-анонса разделены на `venue` и
  обязательный `listing_venue`; обе роли сохраняются под registry lock и входят в
  capture/receipt/replay lineage;
- после смены PlanOnly capture selection требует свежую mutation receipt с точными
  active plan id/hash;
- `market_data_capture` описан как write class, но исключён из status/action
  authorization matrix и не может получить capture token;
- paper model фиксирован до появления события: LONG, 25 USDT, 1x-equivalent,
  вход `t0-60s`, выходы `t0/+5/+15/+60s`, taker-like causal depth;
- пока отсутствуют sealed capture и полный cost model, результатом могут быть только
  `NO_ELIGIBLE_EVENT`, `PAPER_NOT_RUN_NO_CAPTURE_EVIDENCE` или
  `PAPER_NOT_RUN_COST_MODEL_MISSING`; виртуальная позиция и net PnL не создаются;
- replay descriptive-only и не поддерживает ACCEPT/REJECT стратегии.

Активные v38 plan hash и file SHA-256 закреплены во внешнем trust root
`src/frozen_plan_bindings.py`; v37 сохранён byte-identical как непосредственный
предшественник.

## Компоненты

| Файл | Назначение |
|---|---|
| `src/project_config.py` | risk contract, paths, endpoint allow-list, write classes |
| `src/risk_gate.py` | Plan/SHA/capability/gate preflight и capture token boundary |
| `src/public_http.py` | HTTPS allow-list до DNS/connect, без redirects и скрытых retries |
| `src/event_registry.py` | append-only registry v3, asset identity, lifecycle и lineage |
| `src/official_attestation.py` | human-attested official spot `t0` с дословным evidence |
| `src/announcement_discovery.py` | bounded official-index discovery без authority на `t0` |
| `src/announcement_candidate_store.py` | append-only hash-chain непроверенных кандидатов |
| `src/announcement_watch_state.py` | hash-chained attempts, atomic state и dedicated claim |
| `src/announcement_watch_scheduler.py` | adaptive wake-only due-controller без модели |
| `src/candidate_alert.py` | at-most-once alert ledger и локальный toast dispatch |
| `src/official_t0_arming.py` | immutable official-t0 no-capture arming receipt |
| `src/event_bound_plan_proposal.py` | deterministic create-only proposal для v39 |
| `src/fixture_rehearsal.py` | deterministic temporary rehearsal без authority |
| `src/registry_quarantine.py` | локальная CAS-bound quarantine повреждённого поколения |
| `src/capture.py` | bounded collector implementation; активным планом не авторизован |
| `src/replay.py` | строгий offline loader и causal gross BBO markout |
| `src/paper_replay.py` | fail-closed offline paper-readiness и deterministic result hash |
| `tools/start_premarket_perp_paper_only_visible.ps1` | локальный видимый paper-only тик |
| `tools/start_premarket_announcement_discovery_visible.ps1` | один bounded visible discovery tick |
| `tools/start_premarket_announcement_watch_scheduler.ps1` | тихий due-tick/status launcher |
| `tools/install_premarket_announcement_watch_scheduler.ps1` | idempotent hidden Windows task installer |
| `tools/show_premarket_candidate_alert.ps1` | Windows notification sidecar без сети |
| `tools/start_premarket_official_t0_arming_visible.ps1` | visible no-capture arming/status launcher |
| `tools/start_premarket_fixture_rehearsal.ps1` | один offline fixture-only rehearsal |
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
`DESCRIPTIVE_ONLY`; активный план не заявляет доступность Gate capture-кандидатов.

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
Untracked terminal row игнорируется без выделения high-water. Активный план опрашивает
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

Official-attestation producer, сохранённый в v29 и усиленный в v31, выводит точность только из дословно сохранённой
временной формулировки источника: явные секунды дают `1`, а только часы и минуты —
`60`. Minute-only evidence остаётся descriptive-only; candidate selector допускает
событие к будущему capture только при official crypto announcement с точностью не хуже
одной секунды. Сам v29 всё ещё не даёт capture-authority.

REST books OKX не возвращает `instId`, поэтому инструмент привязан к exact URL/query и
их hash, записанным рядом с payload. Gate orderbook поддерживает документированный
формат уровней `{p,s}`. Gate futures ticker не содержит exchange timestamp; такой
payload остаётся optional descriptive и не участвует в causal readiness, а не получает
время приёма как подмену биржевого времени. Для Gate readiness требуются timestamped
trades и orderbook.

Legacy registry v2 остаётся byte-identical migration source и закреплён SHA/head/
mutation receipt в PlanOnly. 28 августа 2026 production v3 был восстановлен под
контрактом v29: один complete public-metadata refresh создал 18 событий (Bybit 5,
Gate 10, OKX 3), без truncation и venue errors. Все они metadata-only и не являются
official seconds-grade capture candidates.

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

28 августа 2026 несовместимое v24-поколение было архивировано recoverable-транзакцией
`20260828T190001Z-ef9e656267-b0d29f75`; terminal state и lock-release proof проверены.
После bootstrap активный production registry возвращает `REGISTRY_OK`.

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

Paper-only readiness tick (без сети, claim, capture token и артефакта при отсутствии
кандидата):

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass `
  -File .\tools\start_premarket_perp_paper_only_visible.ps1 -Json
```

Нормальный текущий результат — `NO_ELIGIBLE_EVENT` и ноль виртуальных позиций.

Один bounded discovery tick (индексы опрашиваются только при свежем активном crypto
pre-market episode; иначе сеть и candidate store не затрагиваются):

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass `
  -File .\tools\start_premarket_announcement_discovery_visible.ps1 `
  -ScheduledTick -Json
```

Установка и read-only статус no-model watcher:

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass `
  -File .\tools\install_premarket_announcement_watch_scheduler.ps1 -Install -Json

pwsh -NoProfile -ExecutionPolicy Bypass `
  -File .\tools\start_premarket_announcement_watch_scheduler.ps1 -Status -Json
```

Задача `\ZolotyayLopata\PremarketAnnouncementWatch` скрыта, просыпается каждые пять
минут и вызывает SHA-bound launcher с закреплённым абсолютным Python runtime.
`RETRY_NEXT_INTERVAL` и `PARTIAL_RETRY_NEXT_INTERVAL` возвращают ненулевой task result
и пишутся в stderr, но задача остаётся включённой. Codex/LLM automation для неё не
включается.

## После candidate alert: refresh → attest → arm

Toast является только приглашением прочитать официальную статью. Сначала скопируйте из
неё дословное предложение со временем, дословный UTC-фрагмент **с секундами** и символ.
Нельзя превращать `10:00 UTC` в `10:00:00 UTC`: minute-only источник останется
descriptive-only. Затем весь участок ниже нужно закончить не позднее 300 секунд после
refresh и минимум за 1 800 секунд до `t0`.

```powershell
$py = "C:\Users\koval\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$pwsh7 = "C:\Program Files\PowerShell\7\pwsh.exe"
$armLauncher = ".\tools\start_premarket_official_t0_arming_visible.ps1"

$perpVenue = "<bybit|okx|gate>"
$listingVenue = "<bybit|okx|gate|binance|bitget|kucoin|upbit>"
$contract = "<native perpetual contract id>"
$spotSymbol = "<spot symbol из официальной статьи>"
$t0Utc = "<YYYY-MM-DDTHH:MM:SSZ>"
$announcementUrl = "<официальный URL из toast>"
$quote = "<дословное предложение со стартом spot>"
$quotedTime = "<дословный UTC-фрагмент с секундами>"
$quotedSymbol = "<дословный символ>"
$operator = "<operator id>"

$metaRun = "metadata_operator_" + [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")
$metadata = (& $py src\event_registry.py --refresh --run-id $metaRun) |
    ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or $metadata.status -ne "REFRESH_COMPLETE" -or
    $metadata.complete -ne $true) {
    throw "metadata refresh incomplete: $($metadata.status)"
}

# Read-only copyable queue: episode_id, lifecycle_generation, contract and article URL.
& $py src\candidate_alert.py --review-status --json
if ($LASTEXITCODE -ne 0) { throw "candidate review queue unavailable" }

$venueState = $metadata.active_lifecycle_generations_by_venue.PSObject.Properties[
    $perpVenue
].Value
$generationProperty = $venueState.PSObject.Properties[$contract]
if ($null -eq $generationProperty) { throw "contract is not current" }
$generation = [int]$generationProperty.Value

$attestRun = "attest_operator_" + [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")
$attestArgs = @(
    "src\official_attestation.py", "--attest", "--run-id", $attestRun,
    "--venue", $perpVenue, "--listing-venue", $listingVenue,
    "--spot-symbol", $spotSymbol, "--premarket-contract-id", $contract,
    "--lifecycle-generation", ([string]$generation),
    "--announced-utc", $t0Utc, "--announcement-url", $announcementUrl,
    "--quote", $quote, "--quoted-time", $quotedTime,
    "--quoted-symbol", $quotedSymbol, "--attested-by", $operator
)
# При cross-venue дополнительно обязательны:
# --same-underlying-decision SAME_UNDERLYING --identity-quote <дословно>
# --quoted-underlying <дословное полное название актива>
$attest = (& $py @attestArgs) | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or $attest.status -notin @("ATTESTED", "ALREADY_RECORDED") -or
    [int]$attest.precision_sec -ne 1) {
    throw "official attestation is not seconds-grade"
}

$current = (& $pwsh7 -NoProfile -ExecutionPolicy Bypass -File $armLauncher `
    -Status -EpisodeId $attest.episode_id -Json) | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw "arming status failed" }
$currentHead = ""
if ($current.status -eq "ARMED_NO_CAPTURE_AUTHORITY") {
    $currentHead = [string]$current.receipt_hash
} elseif ($current.status -ne "NO_ARMING_RECEIPT") {
    throw "unexpected arming state: $($current.status)"
}

$armRun = "arm_operator_" + [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")
$armed = (& $pwsh7 -NoProfile -ExecutionPolicy Bypass -File $armLauncher -Arm `
    -RunId $armRun -EpisodeId $attest.episode_id `
    -OfficialRecordHash $attest.official_record_hash `
    -ExpectedOfficialT0 ([string]$attest.official_spot_t0) `
    -ExpectedContract $contract -ExpectedSpotSymbol $spotSymbol `
    -ExpectedCurrentArmingReceiptHash $currentHead -ArmedBy $operator `
    -AcknowledgeNoCaptureAuthority -Json) | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or $armed.capture_authorized -ne $false -or
    $armed.capture_token_issued -ne $false) { throw "arming failed closed" }

$readback = (& $pwsh7 -NoProfile -ExecutionPolicy Bypass -File $armLauncher `
    -Status -EpisodeId $attest.episode_id -Json) | ConvertFrom-Json
if ($readback.receipt_hash -ne $armed.receipt_hash -or
    $readback.capture_authorized -ne $false) { throw "arming readback mismatch" }
$readback | ConvertTo-Json -Depth 32
```

Каждый writer сам делает initial и commit preflight. Этот маршрут не запускает market
capture, не выдаёт capture token и не создаёт event-bound v39 автоматически.

Fixture-only репетиция всей no-authority цепочки:

```powershell
& $pwsh7 -NoProfile -ExecutionPolicy Bypass `
  -File tools\start_premarket_fixture_rehearsal.ps1 -Json
```

Она удаляет временный workspace до успешного результата и не обращается к сети или
production paths.

## Preflight

```powershell
& $py src\risk_gate.py --preflight --write-class metadata_registry --run-id <id>
& $py src\risk_gate.py --preflight --write-class official_attestation --run-id <id>
& $py src\risk_gate.py --preflight --write-class announcement_discovery --run-id <id>
& $py src\risk_gate.py --preflight --write-class announcement_watch_control --run-id <id>
& $py src\risk_gate.py --preflight --write-class candidate_alert --run-id <id>
& $py src\risk_gate.py --preflight --write-class official_t0_arming --run-id <id>
& $py src\risk_gate.py --preflight --write-class event_bound_plan_proposal --run-id <id>
& $py src\risk_gate.py --preflight --write-class registry_quarantine --run-id <id>
```

Preflight не запускает writer автоматически. Реальные refresh, attestation,
quarantine и discovery являются отдельными операциями. После human official
attestation v38 может создать только no-capture arming receipt и детерминированное
предложение. Реальный event-bound v39 должен быть отдельно проверен, выпущен и явно
одобрен пользователем до ровно одного видимого `market_data_capture`.

## Immutable lineage

Опубликованы v1–v38. Все прежние планы остаются на диске и проверяются по file SHA,
canonical plan hash и identity. v27 и v28 сохранены; v29 сохранён byte-identical;
v30–v37 сохранены byte-identical. v37 зафиксировал первую recovery/rehearsal редакцию,
но полный suite выявил несовместимое расширение production action-list; v38 supersede-ит
его, не переписывая, и отделяет temp-only rehearsal от production write-actions. Ни
один старый PlanOnly не переписывался.

См. `AGENTS.md`, `src/frozen_plan_bindings.py` и решения в `docs/decisions/`.
