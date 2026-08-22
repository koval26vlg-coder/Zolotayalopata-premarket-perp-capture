# Registry v3 control-plane readiness

Дата: 2026-08-23

Вердикт: **READY для capture-disabled control-plane package**

Область verdict: immutable PlanOnly lineage, lifecycle timestamp registry, proxy
separation, write-class preflight, registry locking/verification и public HTTP boundary.
Market-data capture, replay и acceptance в эту область не входят и не запускались.

## PlanOnly lineage

| version | canonical plan hash | file SHA-256 |
|---|---|---|
| v1 | `aa174438bf457e3a57d94e8f3839ae9a61dbb42504d03f5876825f59a9b2d6c1` | `cac4d34cbc6228fd0a7fc7922afb8ce3b1110388a1df860dba5bbd9f40ae2934` |
| v2 | `b7c0543a81b9afa6781f1ca89871d0632405551b3ff51e18e10348da405910d7` | `1d990fbfd84cf5d9d06fd927074b50d200e1d49c0f9bc4200020ec43cb4aac57` |
| v3 | `ee5f555f88691e18207ec22231217a73ec2a82f25069402b14e8d85646350627` | `d6b67c4f52f05bd6902855bc58b416eaab2ef9e3bd430e948ff77d9c6bdb9f94` |

Рабочий blob v1 совпадает с исходным blob commit `cf4b702`:
`c3fd7d41f077f236bee797d132faccd5e7bbec2a`. v2 сохранён без изменений; v3 явно
supersedes v2. `risk_gate --plan-check` возвращает `PLAN_OK` для v3.

## Закрытые findings повторного review

1. Статус `AWAIT_CAPTURE_IMPLEMENTATION_AUDIT_NO_CAPTURE` теперь связан с точной
   матрицей actions/write classes. `market_data_capture` возвращает `BLOCK`, не вызывает
   mint и не создаёт capture-token; фактический CLI preflight подтвердил exit code 1 и
   отсутствие token до/после.
2. `events_for_capture()` не принимает direct sequence либо non-production path.
   Selector проверяет production lineage и обязательный summary receipt, затем
   materialize-ит именно тот snapshot, который был проверен. Missing/incomplete/
   mismatched receipt закрывается recovery-state.
3. Каждая HTTP-попытка заново получает и fail-closed проверяет полный DNS answer set,
   но TCP открывает к выбранному уже проверенному IP literal. Исходное venue name
   сохраняется в connection host и TLS `server_hostname`; redirects и authority drift
   запрещены.

## Verification

- 161/161 offline tests: PASS;
- capability scan: CLEAN;
- PlanOnly check: `PLAN_OK`, v3;
- compileall: PASS;
- no worktree-specific absolute binding strings;
- no venue network, registry refresh, capture or replay executed.

CRLF старого immutable v1 при обычном `git diff --check` отображается как whitespace
из-за репозиторного `* -text`; проверка с `core.whitespace=cr-at-eol` чистая. Менять
байты v1 ради cosmetic diff запрещено.

## Обязательный future gate

Перед capture-enabled PlanOnly v4 token schema/consume должны дополнительно закрепить
и повторно проверить `plan_id`, `plan_hash`, status/write-class authorization, event
revision/hash и gate snapshot. Это не открывает текущий контур: v3 не mint'ит token и
не разрешает capture. Collector `26da7ae` остаётся исключённым; его отдельный audit —
`docs/audits/capture-26da7ae-audit-20260822.md`.
