---
name: cc-auditor
version: 1.0.0
description: 体检机密计算层级与审计链：把数据密级（secret=L1–L4）翻译成所需机密计算层级（cc=CC-L0–CC-L4），检查节点能力与证明是否达标，并校验审计哈希链是否完整。当用户询问"这个机密任务能不能发给这个节点、证明有效吗、审计记录有没有被改"时使用。
metadata:
  requires:
    bins: ["python"]
  type: operational
---

# CC Auditor（机密计算层级与审计链体检）

## 何时使用
- 高密级任务派发前的准入核对（secret 与 cc 的对应关系）；
- 答辩或客户质疑"你们凭什么说数据不出域"时，出示层级与证明依据；
- 怀疑审计记录被改动，需要做完整性校验。

## 类型化输入 / 输出
输入：`--secret`（数据密级）、`--node`（可选，单节点）、
`--state-file`（离线快照）或 `--ctrl`/`--live`。

输出（JSON）：
- `required_cc`：该密级要求的最低机密计算层级；
- `nodes[]`：每节点的 `cc_level / level_ok / attestation_present / attestation_valid / verdict`；
- `admission_result`：`allow` / `blocked` / `no_nodes`；
- `chain`：审计链长度、是否完整、断链位置；
- `terminology`：两套 L1–L4 的区别说明（防止答辩被问倒）。

## 如何运行
```bash
python tool.py --secret L3 --state-file evals/fixtures/state_cc.json
python tool.py --secret L4 --node cloud-0 --live
```

## 确定性规则
- 映射：secret L1/L2 → CC-L0；L3 → CC-L2；L4 → CC-L3；
- `level_ok=false` 或证明无效 → 该节点 `verdict=blocked`，不参与派发；
- 审计链校验：任一条的 `prev_hash` 或 `chain_hash` 不匹配即报断链位置；
- 输出始终附带两套 L1–L4 的术语说明，禁止裸写 "L3"。

## 边界
- **不检查也不触碰硬件 TEE**；沙盒中的证明为软件签发，真机需 NVTrust / CC 机型；
- 不下结论说"已具备机密计算能力"，只说"层级与证明是否满足当前要求"。
