# Решение 004: registry v2, PlanOnly v3 и исключение старого capture

Дата: 2026-08-23

## Immutable lineage

Исходный PlanOnly v1 восстановлен байт-в-байт и больше не изменяется:

- path: `docs/plans/premarket-perp-capture-planonly-20260822.json`;
- `plan_id`: `premarket_perp_capture_20260822`;
- canonical hash:
  `aa174438bf457e3a57d94e8f3839ae9a61dbb42504d03f5876825f59a9b2d6c1`;
- file SHA-256:
  `cac4d34cbc6228fd0a7fc7922afb8ce3b1110388a1df860dba5bbd9f40ae2934`.

Новая спецификация выпущена отдельным объектом:

- path: `docs/plans/premarket-perp-capture-planonly-20260822-v2.json`;
- `plan_id`: `premarket_perp_capture_20260822_v2`;
- canonical hash:
  `b7c0543a81b9afa6781f1ca89871d0632405551b3ff51e18e10348da405910d7`;
- file SHA-256:
  `1d990fbfd84cf5d9d06fd927074b50d200e1d49c0f9bc4200020ec43cb4aac57`;
- status: `AWAIT_CAPTURE_IMPLEMENTATION_AUDIT_NO_CAPTURE`;
- explicit `supersedes_plan_hash`: hash v1.

`write_plan()` создаёт новый файл только через `O_CREAT | O_EXCL`: существующий план
никогда не перезаписывается, а несовпадение требует нового versioned path.

Финальный независимый review v2 нашёл три дефекта реализации: capture-disabled статус
не блокировал mint токена, selector принимал непроверенную sequence, а проверенный DNS
ответ не был связан с фактическим TCP peer. Поэтому v2 также оставлен неизменяемым, а
исправления получили новую identity:

- path: `docs/plans/premarket-perp-capture-planonly-20260822-v3.json`;
- `plan_id`: `premarket_perp_capture_20260822_v3`;
- canonical hash:
  `ee5f555f88691e18207ec22231217a73ec2a82f25069402b14e8d85646350627`;
- file SHA-256:
  `d6b67c4f52f05bd6902855bc58b416eaab2ef9e3bd430e948ff77d9c6bdb9f94`;
- status: `AWAIT_CAPTURE_IMPLEMENTATION_AUDIT_NO_CAPTURE`;
- explicit `supersedes_plan_hash`: hash v2.

## Временные потоки

Registry v2 не имеет универсального поля `t0`. Один episode содержит независимые
append-only streams:

- `premarket_contract_launch_ts`;
- `official_spot_t0`;
- `first_trade_ts`;
- `transition_ts`;
- отдельный descriptive `contract_created_ts` для Gate.

`episode_id` зависит от venue, native contract и generation, но не от меняющегося
расписания. Перенос времени создаёт новую ревизию того же stream; официальный конфликт
закрывается как `OFFICIAL_CONFLICT`.

Только `official_spot_t0` с source class `OFFICIAL_ANNOUNCEMENT` и spot instrument role
может стать `ACCEPTANCE_ANCHOR` и попасть в capture selection. Metadata/observed/proxy
потоки всегда `DESCRIPTIVE_ONLY` и не повышаются постфактум.

## Runtime gates

- `metadata_registry` проходит явный production preflight с точной проверкой PlanOnly,
  capability receipt и resolved control paths, но не занимает market-data claim;
- `market_data_capture` по-прежнему требует общий claim, run record и одноразовый
  token в будущем capture-enabled плане; v3 механически возвращает `BLOCK` и не mint'ит
  token;
- registry refresh сначала полностью staging-ит и проверяет все venue, затем под
  отдельным `O_EXCL` registry lock выполняет verify/build/append/fsync/summary;
- pagination cap, cursor loop, отсутствующий/malformed venue возвращают
  `INCOMPLETE_NO_REGISTRY_WRITE`;
- каждая строка имеет глобальные `record_seq`/`previous_record_hash` и независимые
  `stream_revision`/`supersedes_record_hash`; обязательный summary закрепляет tail hash,
  а selector читает только повторно проверенный production snapshot;
- public HTTP принимает только exact HTTPS host/path и неизменяемый набор query keys,
  запрещает redirects и fail-closed отклоняет не-global либо неоднозначное DNS
  разрешение; TCP подключается к одному из уже проверенных IP, сохраняя venue hostname
  для TLS SNI/сертификата/Host.

## Capture boundary

Collector из commit `26da7ae530010057717564103c0470b9ebbc94de` прошёл
отдельный read-only аудит и исключён из v2/v3. Главные причины: generic/proxy `t0`,
неполный evidence set, недостаточная lineage, readiness по timestamps ошибок и
освобождение claim до terminal receipt. Полный receipt:
`docs/audits/capture-26da7ae-audit-20260822.md`.

Ни registry refresh, ни market-data capture в рамках этого решения не запускались.
Следующий collector должен быть новой реализацией под registry v2 и отдельным
capture-enabled immutable PlanOnly v4; v3 разрешает только metadata refresh и offline
descriptive materialization.
