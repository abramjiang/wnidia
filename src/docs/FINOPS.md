# FinOps：计量、计费与分账（BP P10 落地说明）

> 代码入口：`controller/metering.py`（采数与计价）、`controller/settlement.py`（机主结算）。
> 视图：`GET /admin/metering`、`POST/GET /admin/settlement`、`GET /v1/tenant/{t}/usage`。

## 1. 三层职责（对齐 OpenMeter 的建模思路）

| 层 | 函数 | 说明 |
|---|---|---|
| 采数 | `meter_from_usage()` | 优先采信引擎返回的 `usage`；缺失/非法才回退估算，并置 `estimated=True` |
| 计价 | `price()` / `credit_cost()` | 按口径折算金额；配额扣减用整数积分（兼容 v3 口径） |
| 落账 | `record()` | 写 `ledger`（含 tenant / project / department / engine 维度） |

**为什么估算是底线而不是默认**：引擎返回 `usage` 时能拿到真实 prompt/completion token；
只有引擎不返回（或被 mock 引擎替代）时才估算。账单里每一条都带 `estimated` 标记，
`metering_quality()` 会报出真实计量占比——**对客户出账前应先把估算条目消除**。

## 2. 三口径与单价

| 口径 | 单位 | 默认单价 | 环境变量 |
|---|---|---|---|
| `token` | 元 / 百万 token | 2.0 | `WNIDIA_PRICE_TOKEN` |
| `node` | 元 / 节点·小时 | 1.2 | `WNIDIA_PRICE_NODE` |
| `seat` | 元 / 席位·天 | 8.0 | `WNIDIA_PRICE_SEAT` |

默认口径由 `WNIDIA_BILLING_MODE` 决定；单次请求可用 `billing_mode` 字段覆盖。

> 单价是**演示口径**，不代表报价。真实定价应经 POC 与合同标定。

## 3. 分账维度

`summarize()` 输出五个维度的汇总：`by_tenant` / `by_project` / `by_department` /
`by_billing_mode` / `by_engine`，以及总量（条目、tokens、节点小时、席位数、金额、
估算条目数）。

这对应 BP P10「按部门、项目、Token 算账分账，账单与算力一一对应」：
`ledger` 的每一行都能追溯到具体任务、节点、引擎与口径。

## 4. 机主贡献结算（BP P7）

`settlement.compute()` 的口径：

```
有效算力单元 = 实际消耗 token × 稳定性系数 × 在线率系数
稳定性系数   = 0.5 + 0.5 × stability × uptime_ratio      （下限 0.5，上限 1.0）
在线小时     = 观察窗口（默认 24h）× uptime_ratio
机主分成     = 毛收入 × OWNER_SHARE（默认 0.60，环境变量 WNIDIA_OWNER_SHARE）
平台抽佣     = 毛收入 − 机主分成
```

**分成与抽佣比例是规划假设**（BP P7 原文标注「需小规模实测标定*」）。
`compute()/settle()` 的返回值里带 `assumption=True` 与说明文案，
避免把假设当成已定事实。

## 5. 常用命令

```bash
TOK=$WNIDIA_TOKEN
# 计量与分账总览
curl -s -H "Authorization: Bearer $TOK" http://127.0.0.1:9000/admin/metering | python3 -m json.tool

# 跑一次结算（落库）
curl -s -X POST -H "Authorization: Bearer $TOK" \
  'http://127.0.0.1:9000/admin/settlement?persist=true' | python3 -m json.tool

# 某租户的用量与账单
curl -s -H "Authorization: Bearer $TOK" http://127.0.0.1:9000/v1/tenant/demo/usage

# 客户门户（只读，页面内输入 Token）
open http://127.0.0.1:9000/portal
```

## 6. 与 BP 的差距（如实标注）

| BP 要求 | 现状 |
|---|---|
| 按 Token / 节点 / 席位计费 | ✅ 三口径并存 |
| 账单与算力一一对应 | ✅ `ledger` 逐条可追溯 |
| 按部门/项目分账 | ✅ 维度已落库并可汇总 |
| 订阅分档 P0/P1/P2 | ✅ 档位与能力包已声明（`/admin/subscriptions`），**不做强制计费闸门** |
| 真实发票/税务/对账 | ❌ 未实现（属财务系统，不在本项目范围） |
