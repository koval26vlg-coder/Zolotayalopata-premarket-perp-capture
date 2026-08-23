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
- paper- или live-исполнение;
- private API, ключи, подпись запросов;
- взятие или изменение плеча и маржи;
- вывод и переводы средств;
- решения ACCEPT/REJECT по захваченным данным.

`execution_replay` — офлайн-анализ уже лежащих на диске публичных данных. Текущий
replay v2 публикует только gross BBO markout и не моделирует fill, очередь, комиссии,
funding или net PnL.
Полный контракт: `RISK_CONTRACT` в `src/project_config.py`, он же записан в PlanOnly.

## Risk gate

Запуск чего-либо, что пишет данные, проходит только через явный write-class:
`python src/risk_gate.py --preflight --write-class <class> --run-id <id>`. Он
блокирует, если хоть что-то из этого не так:

- runtime не совпадает с immutable PlanOnly, или план не тот, что одобрен внешним
  trust-root `src/frozen_plan_bindings.py`;
- capability scan нашёл в `src/` или `tools/` запрещённую возможность или URL вне
  allow-list;
- общий active-run gate закрыт или недоступен;
- для `market_data_capture`: общий market-data writer claim занят либо собственный
  предыдущий capture не завершён.

Только `market_data_capture` может получить одноразовый capture-токен. Metadata refresh,
human official attestation и локальная registry quarantine имеют отдельные действия и
не заимствуют capture-authority. Capture без токена невозможен — флага «я подтверждаю»
здесь нет по замыслу.

## Общий writer

Workspace общий с `ZolotyayLopata`. Этот проект — второй market-data writer в нём,
поэтому берёт **тот же** `active-market-data-writer-claim.json`, а не заводит
параллельный. Новый репозиторий не делает workspace другим.

Протухший claim сообщается, но **никогда не снимается автоматически**.

## Объём

- Только публичные market-data endpoints Bybit/OKX/Gate из `ALLOWED_ENDPOINTS`.
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
- Текущий официальный producer фиксирует `t0` с точностью 60 секунд. Поэтому данные
  этого поколения могут быть только descriptive для секундной гипотезы; readiness
  требует отдельного producer с точностью не хуже одной секунды и нового PlanOnly.

## Статус

Capture ещё **не запускался**. PlanOnly в статусе
`REGISTRY_QUARANTINE_HARDENED_NO_CAPTURE`; активный immutable план — v17.
`market_data_capture` этим статусом не авторизован. Первый capture требует отдельного
нового PlanOnly/checkpoint и отдельного разрешения пользователя на видимый запуск.
