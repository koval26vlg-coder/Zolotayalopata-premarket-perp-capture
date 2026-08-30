# 018 — v39 historical acquisition/replay fail-closed

Дата: 2026-08-30

## Решение

Выпустить immutable PlanOnly v39 как отдельный research-only bridge для bounded
исторического сбора публичных данных и offline causal replay. Production capture,
capture token, private API и любые ордера этим планом не разрешаются.

После выпуска capability scan обнаружил, что статическая проверка видит только
префикс runtime-собранного URL Gate archive. Поэтому v39 не активирован и не
переписан. До отказа не создавались historical raw data, arming/proposal, capture
token или market-data capture.

## Immutable identity

- plan id: `premarket_perp_capture_20260822_v39`;
- plan hash: `d3e410f550ccf84c985924120c9970be28c87e3c788dba00ba55cde112406512`;
- file SHA-256: `4e010bc581bd5a2e6fd53e6a44bccc21eb3114d491eb9dc5f18a236d9a52696f`;
- supersedes: immutable v38;
- outcome: `FAIL_CLOSED_CAPABILITY_SCAN_NOT_ACTIVATED`.

## Следствие

Исправление разрешено только новым PlanOnly identity. v39 остаётся на диске
byte-identical как проверяемое свидетельство отказа; active trust root указывает на
последующий v40.
