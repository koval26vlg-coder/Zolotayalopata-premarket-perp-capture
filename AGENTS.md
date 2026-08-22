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

`execution_replay` — офлайн-симуляция по уже лежащим на диске публичным данным.
Полный контракт: `RISK_CONTRACT` в `src/project_config.py`, он же записан в PlanOnly.

## Risk gate

Запуск чего-либо, что пишет данные, проходит только через явный write class:
`python src/risk_gate.py --preflight --write-class <class> --run-id <id>`.
Он блокирует, если хоть что-то из этого не так:

- runtime не совпадает с immutable PlanOnly, или план не тот, что одобрен внешним
  trust-root `src/frozen_plan_bindings.py`;
- capability scan нашёл в `src/` или `tools/` запрещённую возможность или URL вне
  allow-list;
- общий active-run gate закрыт или недоступен;
- canonical shared-gate/writer-claim/capture-root paths отличаются от PlanOnly;
- для `market_data_capture`: общий writer claim занят или собственный capture активен.

`metadata_registry` не занимает market-data claim и не получает токен, но остаётся
PlanOnly/gate/allow-list bound и использует отдельный atomic registry lock.
В текущем capture-disabled PlanOnly v3 `market_data_capture` получает `BLOCK` и токен
не создаётся. Будущий capture-enabled план обязан требовать одноразовый токен; capture
без него невозможен.

## Общий writer

Workspace общий с `ZolotyayLopata`. Этот проект — второй market-data writer в нём,
поэтому берёт **тот же** `active-market-data-writer-claim.json`, а не заводит
параллельный. Новый репозиторий не делает workspace другим.

Протухший claim сообщается, но **никогда не снимается автоматически**.

## Объём

- Только публичные market-data endpoints Bybit/OKX/Gate из `ALLOWED_ENDPOINTS`.
- Хост сам по себе не единица доступа: HTTPS host/path совпадают точно, query keys
  перечислены отдельно, redirects и non-public DNS запрещены. TCP-соединение идёт к
  уже проверенному IP, сохраняя исходное имя venue для TLS SNI, сертификата и Host.
- Расширение доступа = правка `ALLOWED_ENDPOINTS` + перевыпуск PlanOnly + ревью.
  Строка URL, добавленная в коллектор, гейт не пройдёт.

## Границы capture

Непрерывный capture около `t0` — другой класс риска, чем ограниченный тик: он идёт,
пока рынок движется. Отсюда потолки в `capture_bounds` PlanOnly: окно до и после `t0`,
`max_runtime_sec`, `max_requests_per_capture`, один event за capture, один capture
одновременно, видимый терминал.

## Provenance

- Plan hash, file SHA-256 каждого связанного файла и внешний trust-root обязательны.
- Изменение runtime требует тестов и нового versioned PlanOnly identity. Старый план
  остаётся byte-for-byte и связывается с новым через `supersedes_*`.
- `official_spot_t0` отделён от contract launch, first trade и transition. Proxy
  timestamps всегда descriptive-only и не могут поддерживать capture acceptance.
- Непустой production registry без валидного summary receipt считается recovery-state;
  selector принимает только повторно проверенный production snapshot.
- Исключения capability scan объявляются построчно (`# risk-scan: allow <pattern>`),
  считаются и видны в выводе. Молчаливое исключение файла запрещено.

## Статус

Capture ещё **не запускался**. PlanOnly в статусе
`AWAIT_CAPTURE_IMPLEMENTATION_AUDIT_NO_CAPTURE`. Metadata refresh разрешён только
после metadata preflight. Сам статус механически блокирует capture preflight и mint
токена. Первый capture потребует нового immutable PlanOnly и отдельного разрешения
пользователя.
