# -*- coding: utf-8 -*-
"""Edge Box Provisioner：算力箱预装清单 + 纳管命令 + 断网验收项（BP P6）。

离线可用：不依赖任何服务即可产出清单；`--ctrl` 时额外查询控制面当前节点状态。
全栈选型均落在 NVIDIA 生态内（Jetson + JetPack + DeepStream + TensorRT + Fleet Command）。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

# 机型规格（来源：BP P6 引用的 Jetson Thor 官方规格口径）
BOX_MODELS = {
    'thor': {
        'label': 'Jetson AGX Thor 开发者套件',
        'fp4_tflops': 2070, 'mem_gb': 128, 'power_w': (40, 130),
        'nics': '2 × 25GbE', 'model_class': '30B 级',
        'note': 'Blackwell GPU；官方定位物理 AI / 人形机器人',
    },
    'orin': {
        'label': 'Jetson AGX Orin 32/64GB',
        'fp4_tflops': 0, 'mem_gb': 64, 'power_w': (15, 60),
        'nics': '1 × 10GbE（可扩展）', 'model_class': '7B–13B 级',
        'note': '成熟量产型号，作为 Thor 的跨代际混部选项',
    },
}

# 预装软件栈（全部 NVIDIA 全栈）
STACK = [
    {'layer': 'OS / 基础', 'items': ['JetPack（含 L4T 与 CUDA）', 'NVIDIA 容器运行时']},
    {'layer': '推理', 'items': ['TensorRT', 'TensorRT-LLM（可选）', 'NVIDIA NIM（可选）']},
    {'layer': '视频/感知', 'items': ['DeepStream', 'Holoscan（实时传感）']},
    {'layer': '机队管理', 'items': ['NVIDIA Fleet Command / NVAIE', 'DCGM 边缘采集']},
    {'layer': 'WNIDIA', 'items': ['edge worker（本包 worker/）', '断网自治模块',
                                  '合规守卫']},
]

# 断网验收清单（BP P13/P14 的现场可用性要求）
OFFLINE_ACCEPTANCE = [
    '拔掉上行网线 / 断开 Wi-Fi 后，现场推理服务仍在 3 秒内响应',
    '断网期间完成的任务进入本地队列，队列长度可在 /offline/status 查询',
    '恢复联网后 60 秒内自动回放批次，控制面 /admin/offline 可见 replayed=1',
    '同一 batch_id 重复上报只计一次（幂等去重）',
    '断网窗口的在线率被折价记录（不谎报 100% 可用）',
]


def build_plan(a):
    m = BOX_MODELS.get(a.gpu, BOX_MODELS['thor'])
    low, high = m['power_w']
    nodes = max(1, int(a.nodes))
    return {
        'node': a.node, 'gpu': a.gpu, 'box': m,
        'nodes': nodes,
        'placement': {
            'power_budget_w': f'{low * nodes}–{high * nodes}',
            'need_dedicated_hvac': False,
            'note': '40–130W 级别无需专用机房空调（BP P6 原文口径）',
        },
        'preinstall': STACK,
        'enroll_command': (
            f"# 在算力箱上执行（控制面地址按现场替换；引擎只绑回环）\n"
            f"NODE={a.node} NODE_ROLE=decode NODE_TIER=edge \\\n"
            f"  NODE_FORM_FACTOR=box NODE_GENERATION={a.gpu} \\\n"
            f"  NODE_CC_LEVEL={a.cc_level} NODE_BANDWIDTH_MBPS={a.bandwidth_mbps} \\\n"
            f"  VLLM_PORT={a.port} CTRL={a.ctrl} \\\n"
            f"  python -m worker.agent"),
        'offline_acceptance': OFFLINE_ACCEPTANCE,
        'task_classes': {
            'local_first': ['产线质检', '园区巡检', '视频结构化',
                            '机器人 / 具身实时推理'],
            'to_center': ['训练与微调', '70B+ 重推理', '含敏感数据的任务'],
        },
        'subscription_hint': {
            'tier': 'P1' if a.cc_level in ('CC-L0', 'CC-L1') else 'P0',
            'note': ('硬件一次性交付 + 边缘纳管与模型运维按节点年订阅'
                     '（BP P6 商业模式）；具体价格见 docs/PITCH.md'),
        },
        'sources': {'box_spec': 'BP P6 引 NVIDIA Jetson Thor 官方规格',
                    'stack': 'NVIDIA 全栈（JetPack / TensorRT / DeepStream / '
                             'Fleet Command）'},
        'assumption': ('机型规格与毛利口径为 BP 与厂商标称值，未做样机 BOM 核实；'
                       '订阅价格区间为规划假设。'),
    }


def main():
    ap = argparse.ArgumentParser(description='算力箱预装与纳管方案（dry-run）')
    ap.add_argument('--node', default='edge-box-1')
    ap.add_argument('--gpu', default='thor', choices=list(BOX_MODELS))
    ap.add_argument('--nodes', type=int, default=1)
    ap.add_argument('--cc-level', default='CC-L0',
                    choices=['CC-L0', 'CC-L1', 'CC-L2', 'CC-L3', 'CC-L4'])
    ap.add_argument('--bandwidth-mbps', type=float, default=1000)
    ap.add_argument('--port', type=int, default=8105)
    ap.add_argument('--ctrl', default=os.getenv('CTRL',
                                                'http://127.0.0.1:9000'))
    ap.add_argument('--live', action='store_true',
                    help='额外查询控制面是否已纳管该节点')
    a = ap.parse_args()

    out = build_plan(a)
    if a.live:
        try:
            import requests
            s = requests.Session(); s.trust_env = False
            r = s.get(f'{a.ctrl}/admin/state',
                      headers={'Authorization':
                               f'Bearer {os.getenv("WNIDIA_TOKEN", "")}'},
                      timeout=8)
            nodes = {n['node']: n for n in r.json().get('nodes', [])}
            out['control_plane'] = {
                'reachable': True,
                'enrolled': a.node in nodes,
                'node': nodes.get(a.node),
            }
        except Exception as e:                        # noqa: BLE001
            out['control_plane'] = {'reachable': False,
                                    'error': str(e)[:120]}
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
