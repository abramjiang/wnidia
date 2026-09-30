# -*- coding: utf-8 -*-
"""Private Cloud Planner：私有云配置与 TCO 规划（BP P8）。

按并发路数、合规等级与交付形态产出节点清单与三年 TCO，
并**显式列出 vGPU / 切分软件授权**这一隐性成本（BP P8 的成本诚实提示）。
"""
import argparse
import json
import math
import os
import sys

# 参考单价（元；演示口径，可用环境变量覆盖）
UNIT = {
    'mgmt_node': float(os.getenv('WNIDIA_PC_MGMT', '60000')),
    'cpu_node': float(os.getenv('WNIDIA_PC_CPU', '120000')),
    'gpu_node': float(os.getenv('WNIDIA_PC_GPU', '450000')),
    'storage_node': float(os.getenv('WNIDIA_PC_STORAGE', '180000')),
    'backup_node': float(os.getenv('WNIDIA_PC_BACKUP', '90000')),
    'switch_25g': float(os.getenv('WNIDIA_PC_SWITCH', '35000')),
    'facility_per_rack': float(os.getenv('WNIDIA_PC_FACILITY', '150000')),
    'vgpu_license_per_gpu_year': float(os.getenv('WNIDIA_PC_VGPU', '24000')),
    'ops_per_node_year': float(os.getenv('WNIDIA_PC_OPS', '20000')),
}

DELIVERY = {
    'lease': '整机租赁（我方持有资产，按月收租）',
    'hosted': '托管代运营（客户场地 + 我方运维）',
    'onprem': '客户场地部署（客户持有资产，我方交付与订阅）',
}

GRADES = {
    'L1': {'redundancy': '单路', 'backup': 0, 'mgmt': 1},
    'L2': {'redundancy': '双路', 'backup': 1, 'mgmt': 2},
    'L3': {'redundancy': '双路 + 独立备份', 'backup': 1, 'mgmt': 2},
    'L4': {'redundancy': '双路 + 独立备份 + 专属池', 'backup': 1, 'mgmt': 2},
}


def plan(a):
    g = GRADES.get(a.grade, GRADES['L2'])
    conc = int(a.concurrency)
    if conc < 1:
        raise ValueError('concurrency 必须为正整数')

    # 经验配比：每 25 路并发 1 张 GPU 节点；CPU 节点做预处理与编排
    gpu_nodes = max(2, math.ceil(conc / 25.0))
    cpu_nodes = max(3, min(6, math.ceil(conc / 20.0)))
    storage_nodes = 2 if conc <= 60 else 3
    racks = max(1, math.ceil((gpu_nodes + cpu_nodes + storage_nodes) / 6.0))

    items = [
        {'name': '管理节点（HA 控制面）', 'count': g['mgmt'],
         'unit_cny': UNIT['mgmt_node']},
        {'name': 'CPU 计算节点', 'count': cpu_nodes,
         'unit_cny': UNIT['cpu_node']},
        {'name': 'GPU 节点（vGPU / MIG）', 'count': gpu_nodes,
         'unit_cny': UNIT['gpu_node']},
        {'name': '全闪存储节点', 'count': storage_nodes,
         'unit_cny': UNIT['storage_node']},
    ]
    if g['backup']:
        items.append({'name': '独立备份节点', 'count': g['backup'],
                      'unit_cny': UNIT['backup_node']})
    items.append({'name': '25G 堆叠交换机', 'count': max(2, racks),
                  'unit_cny': UNIT['switch_25g']})
    items.append({'name': '机房配套（UPS / 精密空调 / 门禁）',
                  'count': racks, 'unit_cny': UNIT['facility_per_rack']})

    capex_items = [dict(it, subtotal_cny=it['count'] * it['unit_cny'])
                   for it in items]
    capex = sum(it['subtotal_cny'] for it in capex_items)

    # 隐性成本：vGPU / 切分软件授权（按 GPU 张数 × 年）
    gpus = gpu_nodes * int(a.gpus_per_node)
    vgpu_year = gpus * UNIT['vgpu_license_per_gpu_year'] if a.use_vgpu else 0.0
    ops_year = (gpu_nodes + cpu_nodes + storage_nodes + g['mgmt']
                + g['backup']) * UNIT['ops_per_node_year']

    years = int(a.years)
    license_3y = vgpu_year * years
    ops_3y = ops_year * years
    tco_3y = capex + license_3y + ops_3y

    return {
        'grade': a.grade, 'concurrency': conc,
        'delivery': {'mode': a.delivery, 'label': DELIVERY.get(a.delivery, '')},
        'topology': {'redundancy': g['redundancy'],
                     'gpu_nodes': gpu_nodes, 'cpu_nodes': cpu_nodes,
                     'storage_nodes': storage_nodes,
                     'backup_nodes': g['backup'], 'racks': racks,
                     'interconnect': '25G 堆叠组网，支持横向扩展'},
        'capex': {'items': capex_items, 'total_cny': capex},
        'opex': {
            'vgpu_license_cny_per_year': vgpu_year,
            f'vgpu_license_cny_{years}y': license_3y,
            'ops_cny_per_year': ops_year,
            f'ops_cny_{years}y': ops_3y,
        },
        'tco': {f'total_cny_{years}y': tco_3y,
                'per_gpu_cny': round(tco_3y / max(gpus, 1), 2),
                'assumption': ('设备与运维单价为演示口径（可用环境变量覆盖），'
                               '非真实报价；机房与土建成本未计入。')},
        # 成本诚实提示（BP P8 原文要求）
        'hidden_cost_warning': {
            'item': 'vGPU / 切分软件授权',
            'per_year_cny': vgpu_year,
            'note': ('这是此类项目最大的隐性成本。本方案把它单列，'
                     '不以硬件折扣掩盖后续年费；若客户接受 MIG / 时间片等'
                     '免授权切分，可把该项降到 0。'),
            'advice': '报价单必须单列授权项，并在 POC 阶段即计入 TCO 模型。',
        },
        'boundary': ('本 Skill 只产出配置与 TCO 规划，不代表已完成集成；'
                     '实际配置按客户负载、机房条件与合规等级核定。'),
    }


def main():
    ap = argparse.ArgumentParser(description='私有云配置与 TCO 规划')
    ap.add_argument('--concurrency', type=int, default=60,
                    help='并发路数（典型 50–100）')
    ap.add_argument('--grade', default='L2', choices=list(GRADES))
    ap.add_argument('--gpus-per-node', type=int, default=4)
    ap.add_argument('--use-vgpu', action='store_true',
                    help='是否使用 vGPU/切分授权（会显著提高 TCO）')
    ap.add_argument('--delivery', default='onprem', choices=list(DELIVERY))
    ap.add_argument('--years', type=int, default=3)
    a = ap.parse_args()
    try:
        print(json.dumps(plan(a), ensure_ascii=False, indent=2))
    except ValueError as e:
        print(json.dumps({'error': str(e)}, ensure_ascii=False, indent=2))
        sys.exit(1)


if __name__ == '__main__':
    main()
