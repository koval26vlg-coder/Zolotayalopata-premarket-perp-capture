# Project Rules

## Что это и почему отдельно

Проект наблюдает **бессрочные фьючерсы** (perpetual futures) вокруг момента листинга
`t0` на Bybit, OKX и Gate, чтобы офлайн переиграть гипотезу «вход до листинга, выход
на `t0`/+5с/+15с/+60с».

Это отдельный репозиторий, а не ветка `listing-momentum-monitor`, по одной причине:
тот проект запрещает `leverage or margin` и не приближается к ним, а этот **смотрит на
рынок с плечом**. Разница между «наблюдать инструмент с плечом» и «брать плечо» —
единственное, на чём держится безопасность этой работы, поэтому она проверяется
механически, а не декларируется.

## Риск-контракт

Наблюдаемый класс инструментов: `crypto_perpetual_futures`.
Что делает сам проект — ничего из перечисленного, никогда:

- ордера любого вида, на любой площадке, в любом режиме;
- биржевое paper/testnet- или live-исполнение;
- private API, ключи, подпись запросов;
- взятие или изменение плеча и маржи;
- вывод и переводы средств;
- решения ACCEPT/REJECT по захваченным данным.

`execution_replay` — офлайн-анализ уже лежащих на диске публичных данных. v34
сохраняет fail-closed offline paper simulation, bounded official
announcement-index discovery: без подходящего official event,
sealed capture и полного cost model она создаёт ноль виртуальных позиций и не публикует
net PnL. Discovery-кандидат не является `t0` и не даёт capture-authority. Биржевое
paper execution остаётся запрещённым. Candidate store принимает только exact schema,
фиксированные non-authority значения и официальный URL, связанный с listing venue.
No-model watcher просыпается локально каждые пять минут, но сеть и исследовательские
записи разрешены только при наступлении adaptive due; `NOT_DUE` ничего не пишет.
Полный контракт: `RISK_CONTRACT` в `src/project_config.py`, он же записан в PlanOnly.

## Risk gate

Запуск чего-либо, что пишет данные, проходит только через явный write-class:
`python src/risk_gate.py --preflight --write-class <class> --run-id <id>`. Он
блокирует, если хоть что-то из этого не так:

- runtime не совпадает с immutable PlanOnly, или план не тот, что одобрен внешним
  trust-root `src/frozen_plan_bindings.py`;
- capability scan нашёл в `src/` или `tools/` запрещённую возможность или URL вне
  allow-list;
- общий active-run gate закрыт или недоступен для research/data write-class;
- для `market_data_capture`: общий market-data writer claim занят либо собственный
  предыдущий capture не завершён.

Только `market_data_capture` может получить одноразовый capture-токен. Metadata refresh,
human official attestation, bounded announcement discovery и локальная registry
quarantine имеют отдельные действия и не заимствуют capture-authority. Capture без
токена невозможен — флага «я подтверждаю» здесь нет по замыслу.

Исключение только одно: `announcement_watch_control` пишет локальные state/ledger/claim
после PlanOnly/capability preflight, но не требует shared gate. Это нужно, чтобы
зафиксировать закрытый gate и отложить retry до следующего interval. Этот класс не
имеет endpoints, capture token или global market-data claim.
Если сам PlanOnly/capability preflight не прошёл, control-write запрещён: wake
завершается до claim, сети и записи с ненулевым кодом и проверяется снова только на
следующем пятиминутном wake. Подделывать backoff-файл без проверенного плана нельзя.

## Общий writer

Workspace общий с `ZolotyayLopata`. Этот проект — второй market-data writer в нём,
поэтому берёт **тот же** `active-market-data-writer-claim.json`, а не заводит
параллельный. Новый репозиторий не делает workspace другим.

Протухший claim сообщается, но **никогда не снимается автоматически**.

## Объём

- Только публичные market-data endpoints Bybit/OKX/Gate и точные public announcement
  index endpoints Bybit/Bitget/KuCoin из `ALLOWED_ENDPOINTS`; article bodies не читаются.
- Хост сам по себе не единица доступа: путь объявляется отдельно.
- Расширение доступа = правка `ALLOWED_ENDPOINTS` + перевыпуск PlanOnly + ревью.
  Строка URL, добавленная в коллектор, гейт не пройдёт.

## Границы capture

Непрерывный capture около `t0` — другой класс риска, чем ограниченный тик: он идёт,
пока рынок движется. Отсюда потолки в `capture_bounds` PlanOnly: окно до и после `t0`,
`max_runtime_sec`, `max_requests_per_capture`, один event за capture, один capture
одновременно, видимый терминал.

## Provenance

- Plan hash, file SHA-256 каждого связанного файла и внешний trust-root обязательны.
- Изменение runtime требует тестов и **перевыпуска** PlanOnly: план не переписывается
  на месте, он заменяется новым с явным `supersedes`. Это проверяется механически:
  версия входит в имя файла и `plan_id`, superseded-планы остаются на диске, и
  `--plan-check` роняет проверку, если хоть один из них удалён или изменён.
- Класс записи (`WRITE_CLASSES`) исполняется вызовом, а не описывается: refresh реестра
  проверяет план и capability scan перед первой записью.
- Исключения capability scan объявляются построчно (`# risk-scan: allow <pattern>`),
  считаются и видны в выводе. Молчаливое исключение файла запрещено.
- Production registry v3 хранит явный `asset_class` и issuer identity. Только
  `CRYPTO_TOKEN` может стать capture-кандидатом; equity, tokenized equity, другой
  TradFi и неразрешённая идентичность остаются `DESCRIPTIVE_ONLY`.
- Bybit Linear PreLaunch, Bybit Linear Trading, OKX SWAP, OKX FUTURES и Gate USDT
  Futures — пять отдельных discovery-поверхностей. Полнота и relevant identity set
  фиксируются по каждой поверхности; Bybit Trading используется для явного
  cross-surface transition уже отслеживаемого PreLaunch-контракта.
- Replay production evidence проверяется по историческому префиксу реестра и точной
  mutation receipt, а не по текущему head.
- Текущий human-attested producer выводит точность из дословного времени источника.
  Minute-only источник остаётся descriptive; только явный `HH:MM:SS` может дать
  seconds-grade candidate, но v34 всё равно не авторизует capture.

## Статус

Capture ещё **не запускался**. PlanOnly в статусе
`ANNOUNCEMENT_WATCH_SCHEDULED_NO_CAPTURE`; активный immutable план — v34.
`market_data_capture` этим статусом не авторизован. Discovery сохраняет только
`UNVERIFIED_ANNOUNCEMENT_DISCOVERY`; index publication time и ticker match не могут
стать official `t0`. После human attestation нужен отдельный arming/checkpoint, а затем
event-bound PlanOnly и отдельное разрешение пользователя на видимый capture. Текущий
paper-only launcher выполняет только проверку готовности и детерминированный offline
тик; `NO_ELIGIBLE_EVENT` является нормальным нулевым результатом, а не сделкой.
