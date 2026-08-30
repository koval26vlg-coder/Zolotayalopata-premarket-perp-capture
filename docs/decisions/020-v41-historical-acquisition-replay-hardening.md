# 020 — v41 historical hardening rejected before acquisition

Дата: 2026-08-30

## Решение

Сохранить immutable PlanOnly v41 без изменений, но не выполнять под ним historical
acquisition. Первый independent review закрыл известные дефекты v40, после чего v41
прошёл offline tests и Plan/SHA preflight. Второй независимый review до первого тика
показал, что execution evidence envelope оставался self-attested: вызывающий код мог
сам назначить выдуманный manifest hash, пересчитать payload hash и получить
`COMPLETE` для искусственного L2 payload без проверки terminal receipt и lineage.

Дополнительно непустой список полностью невалидных mark/index строк ошибочно скрывал
`liquidation_model_missing`. Поэтому v41 считается выпущенным, но отклонённым до
активации data path. Его файл не переписывается, trust root переводится на новый v42.

## Immutable identity

- plan id: `premarket_perp_capture_20260822_v41`;
- status: `HISTORICAL_ACQUISITION_REPLAY_HARDENED_NO_CAPTURE`;
- plan hash: `137e4c7da1236727cadbba8b22b209a31465b9a7353b06cd916ab7f207a109b2`;
- file SHA-256: `ab568c1656342f33ff6a9ab415129fbf4e0386e9e24112d90861536adbd376d8`;
- supersedes: immutable v40;
- historical acquisition performed: false;
- forward capture performed: false;
- orders: false.

## Почему hash envelope недостаточен

Hash доказывает только, что payload не изменился после вычисления hash. Если и payload,
и hash предоставил один вызывающий код, они не доказывают происхождение данных.
Production execution result требует независимой проверки on-disk capture manifest,
terminal receipt, raw evidence и registry/PlanOnly lineage.

## Следующий checkpoint

Выпустить v42, который сохраняет fixed causal model для explicit synthetic fixtures,
но всегда возвращает `NOT_RUN_TRUSTED_EVIDENCE_LOADER_REQUIRED` для caller-supplied
sealed production input. Будущий event-bound capture переносится на v43.
