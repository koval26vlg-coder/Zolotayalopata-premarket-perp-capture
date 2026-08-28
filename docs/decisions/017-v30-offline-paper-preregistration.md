# 017 — v30 offline paper preregistration

Дата: 2026-08-28

## Решение

Активировать immutable PlanOnly v30 только для fail-closed локальной paper simulation.
Это не биржевое paper/testnet-исполнение и не разрешение на market-data capture.

Параметры зафиксированы до появления подходящего события: LONG, virtual notional
25 USDT, 1x-equivalent, вход `t0-60s`, выходы `t0/+5/+15/+60s`, taker-like causal
depth. Production CLI не принимает переопределения модели.

## Границы

- private API, ключи, подпись, ордера, live execution, leverage, margin и real capital
  запрещены;
- допустим только `OFFICIAL_ANNOUNCEMENT + CRYPTO_TOKEN + t0_precision_sec=1`;
- metadata и proxy не создают виртуальную позицию;
- без sealed capture результат — `PAPER_NOT_RUN_NO_CAPTURE_EVIDENCE`;
- до нормализации contract units, causal depth, venue fees, slippage, funding,
  mark/index и liquidation parameters результат — `PAPER_NOT_RUN_COST_MODEL_MISSING`;
- во всех v30-ветках создаётся ноль виртуальных позиций, net PnL отсутствует,
  `acceptance_capable=false`;
- `NO_ELIGIBLE_EVENT` — корректный завершённый paper-only тик.

## Следующий checkpoint

Отдельный v31 выпускается только после появления конкретного официального
seconds-grade crypto event. Он должен связать точный event lineage и отдельно
разрешить не более одного bounded visible capture. v30 capture не разрешает.
