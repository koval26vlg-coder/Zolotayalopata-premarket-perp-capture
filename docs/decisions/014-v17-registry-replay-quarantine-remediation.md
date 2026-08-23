# 014 — v17 registry, replay and quarantine remediation

Дата: 2026-08-23

## Решение

Сохранить v16 byte-identical и выпустить новый immutable PlanOnly v17. Capture остаётся
закрытым: пакет исправляет доказательные границы до первого production capture и не
является активацией, сетевым refresh или торговым разрешением.

## Registry authority

- Production authority использует отдельный schema/path v3.
- Registry v2 не переписывается и закреплён как read-only migration source по exact
  byte SHA, head record hash, summary-content hash и mutation receipt.
- `asset_class`, issuer namespace/id и identity hash — первичные данные. Только
  известный `CRYPTO_TOKEN` может стать capture candidate.
- Bybit Linear PreLaunch, Bybit Linear Trading, OKX SWAP, OKX FUTURES и Gate USDT
  Futures — пять раздельных discovery-поверхностей.
- Каждая поверхность exact-bound к endpoint, query, rows path, native ID, envelope,
  lifecycle types и instrument type. Non-object row, missing/noncanonical или duplicate
  native ID и malformed lifecycle field означают acquisition failure без mutation.
- Bybit требует exact `category=linear`, exact integer/string zero `retCode`, канонический
  string cursor и совпадение каждой строки с запрошенной `PreLaunch`/`Trading`
  поверхностью. Boolean/float zero и cursor с padding запрещены.
- Bybit Trading, OKX SWAP/FUTURES и Gate — обязательные непустые full-universe
  поверхности с per-surface retention guard 50%; только Bybit PreLaunch может быть
  законно пустой.
- Отсутствие tracked identity без explicit terminal evidence — acquisition failure без
  mutation. Только явная отмена/делистинг/transition завершает generation.
- Историческая terminal-строка не создаёт новую local generation и не меняет high-water;
  она принимается только для ранее active/scheduled поколения.
- Bybit v17 опрашивает только `PreLaunch` и `Trading`. Если отменённый/закрытый контракт
  исчез из обеих поверхностей, это остаётся acquisition failure без terminal inference;
  отдельная terminal-status поверхность потребует нового PlanOnly.
- OKX `xperp`/`normal` для tracked generation и explicit `preMktSwTime` — transition
  evidence даже если текущий state уже terminal; exact timestamp имеет приоритет над
  detection-time proxy при одновременных ответах SWAP/FUTURES.
- Gate terminal statuses включают `delisting`, `delisted`, `cancelled`, `canceled`;
  `in_delisting=true` также означает DELISTING; `trading` становится transition только
  для ранее отслеживаемого контракта, который больше не pre-market.
- Gate без явного проверенного `contract_type` остаётся `UNCLASSIFIED` и
  descriptive-only; доступность Gate crypto candidate этим PlanOnly не утверждается.
- Любая причина terminal lifecycle отслеживаемого поколения дописывается в registry
  как append-only observation с `lifecycle_phase`. Terminal ids последнего complete
  refresh читаются ровно на следующем refresh только для cross-surface classification;
  они не являются required/relevant/completeness/active/high-water authority.
- Mutation receipt exact-bound к полному pre-hash record: registry/summary lineage,
  mutation identity, active/high-water state, venue/surface counts, relevant identity
  IDs/hashes и terminal IDs. Поле `mutation_run_id` не подменяется generic `run_id`.

## Event clocks and lineage

`premarket_contract_launch_ts`, `official_spot_t0`, `first_trade_ts`, `transition_ts`
и `contract_created_ts` не взаимозаменяемы. Proxy evidence всегда
`DESCRIPTIVE_ONLY` и не поддерживает acceptance.

Capture lineage содержит точную `t0_precision_sec`, historical registry prefix/tail,
неизменяемую mutation receipt и её `summary_content_sha256`. Текущий mutable summary
hash не является самостоятельной authority и в capture lineage не дублируется.

Official producer v1 имеет точность 60 секунд. Readiness секундной гипотезы требует
точность не хуже одной секунды, поэтому текущее поколение остаётся descriptive-only.
Повышение authority требует нового official producer и нового immutable PlanOnly.

## Venue payload authority

- OKX REST books не возвращает instrument id; exact request URL, params и hash являются
  instrument authority и сохраняются рядом с payload.
- Gate orderbook принимает оба структурно валидных уровня: positional sequence и
  mapping `{p,s}`.
- Gate futures ticker не содержит exchange timestamp. `received_ts` не подменяет его;
  ticker остаётся optional descriptive, а Gate readiness строится по timestamped
  trades и orderbook.

## Replay

Production replay разрешает только preregistered entry `t0-60s` и выходы
`t0/+5s/+15s/+60s`. Пустые, отрицательные, дублированные и изменённые горизонты
fail-closed. Readiness отчёта равна логическому AND sealed capture readiness,
наблюдаемого entry и всех фиксированных exits.

Причинные часы — `received_ts`; выбирается первая валидная BBO-точка на target или
после него в пределах одной каденции. Pre-target fallback, interpolation и bracketing
запрещены. Output — descriptive gross BBO markout, без fill, fees, slippage, funding,
liquidation, net PnL или ACCEPT/REJECT.

Replay принимает capture только из строгого потомка `capture_root`, зафиксированного
тем историческим PlanOnly, который сам авторизовал `market_data_capture`; перенос в
текущий root или ссылка на capture-disabled Plan не повышают authority.

## Safe quarantine

Локальная quarantine требует initial/commit preflight, registry O_EXCL lock, global
market-writer claim, exact operator CAS и same-volume durable archive publication.
До source mutation проверяются PREPARED, manifest, exact archive entry set, raw bytes и
source CAS. Receipts, summary (если есть) и registry переводятся durable move в
детерминированные retained tombstones; registry — последним. Их exact имена/байты
закреплены в `SOURCE_DEACTIVATED` и проверяются до и после release global claim.
После этой границы все три canonical path — registry, summary и receipt-directory —
обязаны отсутствовать независимо от их исходного presence. Их позднее появление не
удаляется и переводит транзакцию в fail-closed manual recovery с сохранением
оставшихся locks.
Registry lock последним durable move превращается в terminal proof; после этого нет
fallible I/O. Любой сбой сохраняет оставшиеся locks и требует manual recovery.
Automatic recovery запрещён. Наличие реализации не означает выполнение production
quarantine.

## Plan authority

- schema: `premarket_perp_capture_planonly_v17`;
- plan_id: `premarket_perp_capture_20260822_v17`;
- predecessor: exact immutable v16;
- status: `REGISTRY_QUARANTINE_HARDENED_NO_CAPTURE`;
- plan hash:
  `56cc373e25d1710e2fbd6fe5ac039ecb1065dfb1fbe0ead53757ae6342fb731b`;
- file SHA-256:
  `748f116c785aa5a9cc694be394eb355e554eceef7cd44a32d746cf673c406209`;
- identity закреплена в `src/frozen_plan_bindings.py` и README.

`market_data_capture` отсутствует в authorization matrix. В этом пакете не запускаются
collector, live registry refresh, capture, scheduler, production replay или quarantine.
