---
name: edge-box-provisioner
version: 1.0.0
description: 为 Jetson 边缘算力箱产出预装清单（NVIDIA 全栈）、纳管命令与断网验收项，并给出订阅档位建议。当用户询问"算力箱怎么配、预装什么、怎么纳管、断网还能不能用"时使用。
metadata:
  requires:
    bins: ["python"]
  type: operational
---

# Edge Box Provisioner（算力箱预装与纳管）

## 何时使用
- 现场交付前确认一体机预装内容与纳管命令；
- 需要向客户说明"本地优先、断网可运行"如何验收；
- 需要判断该算力箱应挂 P0 还是 P1 订阅档。

## 类型化输入 / 输出
输入：
- `gpu`（string）：`thor` / `orin`；
- `nodes`（int）：级联台数；
- `cc-level`（string）：该箱要支持的机密计算层级（CC-L0–CC-L4）；
- `bandwidth-mbps` / `port` / `ctrl`；`--live` 时额外核对控制面是否已纳管。

输出（JSON）：
- `box`（object）：机型规格（FP4 TFLOPS、统一内存、功耗、组网）；
- `placement`（object）：功耗预算与是否需专用机房空调；
- `preinstall`（array）：分层预装清单（JetPack / TensorRT / DeepStream / Fleet Command / WNIDIA）；
- `enroll_command`（string）：可直接执行的纳管命令；
- `offline_acceptance`（array）：断网验收检查项；
- `task_classes`（object）：本地优先任务 vs 必须回中心的分类；
- `subscription_hint`（object）：订阅档位建议。

## 如何运行
```bash
python tool.py --gpu thor --nodes 2 --cc-level CC-L2 --bandwidth-mbps 2500
python tool.py --gpu orin --live --ctrl http://127.0.0.1:9000
```

## 确定性规则
- 机型规格取自 BP P6 引用的官方口径，不臆造；
- 功耗预算按台数线性折算；`need_dedicated_hvac=false`（40–130W 级别）；
- `cc-level` ≥ CC-L2 时订阅建议升至 P0；
- 所有输出显式带 `assumption`，标注哪些是规划假设。

## 边界
- 只产出方案，不执行安装；硬件 BOM 与毛利需样机核实；
- 不触碰节点系统配置（驱动、固件、BIOS）。
