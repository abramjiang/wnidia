# -*- coding: utf-8 -*-
"""统一口径注册表（Single Source of Truth for terminology）。

来源：BP × 代码差距标注文档 §8「建议固定下来的三套口径」。
本模块把三处曾经混乱的表述固化成代码，供 API、看板、Skill、文档共同引用：

    ① 分层口径：对外只讲「云—边—端」三层；内部 tier 四档（多出的 cpu 为兜底占位，不对外）
    ② 密级口径：数据密级 secret=L1–L4 与 机密计算层级 cc=CC-L0–CC-L4 **彻底分离**
    ③ 能力口径：凡「有决定无执行」的能力，一律标注为 decision_only，
                 对外表述为「策略可计算可解释，执行依赖硬件与真机验证」，不说"已支持"

设计原则：只登记事实，不做判断。任何模块要对外描述能力，都应从这里取词，
不允许各写各的。
"""

# ---------------------------------------------------------------- ① 分层口径
# 内部四档 → 对外三层
TIER_TO_LAYER = {
    'cloud': 'center',     # 中心层
    'edge':  'edge',       # 边缘层
    'home':  'device',     # 端侧层
    'cpu':   'internal',   # 兜底占位，不对外
}
LAYER_LABELS = {
    'center': '中心层 · 私有云 / 区域池',
    'edge': '边缘层 · 算力箱',
    'device': '端侧层 · 家庭异构网格',
    'internal': '内部兜底档（不对外披露）',
}
# 对外披露的三层顺序
PUBLIC_LAYERS = ['center', 'edge', 'device']


def public_layer(tier):
    return TIER_TO_LAYER.get(tier, 'internal')


def is_public_tier(tier):
    return public_layer(tier) in PUBLIC_LAYERS


def layer_summary():
    return [{'layer': l, 'label': LAYER_LABELS[l],
             'internal_tiers': [t for t, l2 in TIER_TO_LAYER.items() if l2 == l]}
            for l in PUBLIC_LAYERS]


# ---------------------------------------------------------------- ② 密级口径
# 数据密级（任务侧）：决定任务能去哪些节点
SECRET_LEVELS = {
    'L1': '公开',
    'L2': '内部',
    'L3': '机密',
    'L4': '绝密 / 强主权',
}

# 机密计算层级（节点侧）：决定节点具备哪种可信能力。**与数据密级完全独立**
CC_LEVELS = {
    'CC-L0': '无机密计算能力（明文执行）',
    'CC-L1': '机密 GPU 环境（CPU / GPU / 显存全链路加密，宿主不可见）',
    'CC-L2': '远程证明（任务下发前校验软硬件完整性，不通过即拒绝调度）',
    'CC-L3': '密钥与审计（KMS/HSM 托管密钥，操作全量留痕，可对监管出示）',
    'CC-L4': '部署可选（专属资源池 / 边缘本地交付，数据全程不出客户边界）',
}
CC_RANK = {k: i for i, k in enumerate(CC_LEVELS)}
SECRET_RANK = {k: i + 1 for i, k in enumerate(SECRET_LEVELS)}

# 数据密级 → 要求的最低机密计算层级（准入闸门用）
SECRET_TO_MIN_CC = {
    'L1': 'CC-L0',
    'L2': 'CC-L0',
    'L3': 'CC-L2',   # 机密数据：必须能出证明
    'L4': 'CC-L3',   # 绝密/强主权：还要有密钥托管与可出示审计
}


def required_cc(secret):
    return SECRET_TO_MIN_CC.get(secret, 'CC-L0')


def cc_satisfies(node_cc, required):
    return CC_RANK.get(node_cc, 0) >= CC_RANK.get(required, 0)


def terminology_map():
    """给答辩页/文档用的一张映射表：两套 L1–L4 的区别。"""
    return {
        'secret': {'prefix': 'secret=', 'means': '数据密级（任务侧）',
                   'levels': SECRET_LEVELS, 'example': 'secret=L3 表示机密级数据'},
        'cc': {'prefix': 'cc=', 'means': '机密计算层级（节点侧）',
               'levels': CC_LEVELS, 'example': 'cc=CC-L2 表示该节点可出远程证明'},
        'note': '两套符号都含 L1–L4，但语义完全不同；对外一律带前缀，禁止裸写 "L3"。',
    }


# ---------------------------------------------------------------- ③ 能力口径
# 成熟度：implemented 已实现 / partial 部分 / decision_only 有决定无执行
#         / simulated 沙盒模拟 / planned 仅规划
CAPABILITY_MATRIX = {
    # —— 调度与纳管 ——
    'dispatch.three_dim_routing': {
        'label': '三维路由（时延 / 成本 / 密级）',
        'status': 'implemented',
        'note': '时延维度于 v4 补齐：节点上报 latency_ms，打分含时延项，支持 latency_budget 约束',
    },
    'dispatch.multi_objective_score': {
        'label': '多目标打分与可解释决策',
        'status': 'implemented',
        'note': '角色亲和 / 利用率 / 显存 / KV / 信誉 / 时延 / 档位偏好，全确定性可复现',
    },
    'dispatch.sla_priority_queue': {
        'label': 'SLA 优先级排队',
        'status': 'implemented',
        'note': 'v4 补齐：出队顺序按 sla-1 → sla-2 → sla-3，同级按创建时间',
    },
    'dispatch.time_slicing': {
        'label': '分时复用',
        'status': 'simulated',
        'note': 'v4 以策略+沙盒模拟呈现（窗口划分与配额），真机 MPS/MIG 执行依赖硬件',
    },
    'dispatch.preempt_checkpoint': {
        'label': '高优抢占 + checkpoint 续算',
        'status': 'implemented',
        'note': '含失败回滚（v3）与隔离期语义',
    },
    'resource.profile': {
        'label': '资源画像与在线率',
        'status': 'implemented',
        'note': 'v4 补齐带宽 / 在线率 / 稳定性维度',
    },
    'resource.gpu_partition': {
        'label': 'GPU 切分（vGPU / MIG / MPS）',
        'status': 'decision_only',
        'note': '策略可计算可解释，执行依赖 MIG 机型；GB10 不支持 MIG',
    },
    'resource.cross_generation': {
        'label': '跨代际混部',
        'status': 'partial',
        'note': '画像已支持代际标签，真机跨代验证待补',
    },
    # —— 引擎与一致性 ——
    'engine.pluggable': {
        'label': '引擎可插拔与降级链',
        'status': 'implemented',
        'note': 'mock / ollama / vllm / tensorfold 四引擎，确定性选路',
    },
    'engine.byte_exact': {
        'label': '逐字节可复现推理',
        'status': 'implemented',
        'note': 'exact 引擎约束 + consistency_satisfied 显式标注，未满足时提示举证风险',
    },
    # —— 计量与商业 ——
    'finops.token_metering': {
        'label': '真实 Token 计量',
        'status': 'implemented',
        'note': 'v4 补齐：优先采信引擎返回 usage，缺失时估算并标 estimated=true',
    },
    'finops.three_billing_modes': {
        'label': '三口径计费（token / node / seat）',
        'status': 'implemented',
        'note': 'v4 补齐：三种口径并存，可按租户配置',
    },
    'finops.split_account': {
        'label': '按租户 / 项目 / 部门分账',
        'status': 'implemented',
        'note': 'v4 补齐：ledger 增维度字段与汇总视图',
    },
    'finops.contribution_settlement': {
        'label': '机主贡献结算与平台抽佣',
        'status': 'implemented',
        'note': 'v4 新增：按有效算力×在线时长结算，含抽佣与分成明细',
    },
    'finops.subscription_tiers': {
        'label': '订阅分档 P0 / P1 / P2',
        'status': 'implemented',
        'note': 'v4 新增档位定义与能力开关（仅声明与展示，不做计费强制）',
    },
    # —— 可信与合规 ——
    'trust.attestation': {
        'label': '远程证明（不通过即拒绝调度）',
        'status': 'simulated',
        'note': 'v4 以软件模拟证明链接入准入闸门；真机接 NVTrust/CC 留接口，不碰驱动',
    },
    'trust.confidential_gpu': {
        'label': '机密 GPU 环境（TEE）',
        'status': 'planned',
        'note': '需 H100/B200 CC 机型，云上租用即可演示',
    },
    'trust.kms_audit': {
        'label': '密钥托管与可出示审计',
        'status': 'implemented',
        'note': 'v4 新增：审计证据包（哈希链）可导出校验，密钥以本地 KMS 占位实现',
    },
    'trust.majority_verdict': {
        'label': '多副本一致性校验与信誉分',
        'status': 'implemented',
        'note': '确定性多数决 + JEV 语义判定，不足 3 份不做惩罚',
    },
    'trust.compliance_guard': {
        'label': '合规守卫（默认拒绝）',
        'status': 'implemented',
        'note': '只有 8888/9000 可绑 0.0.0.0；非回环+弱口令拒启；禁内网探测；凭据脱敏',
    },
    'trust.prompt_privacy': {
        'label': '推理数据（prompt）不留中心明文',
        'status': 'implemented',
        'note': ('v5 新增：prompt **默认不落库明文**，库里只留占位标记 + 摘要 + 长度；'
                 '调度所需副本只在控制面进程内存里，任务终态即释放；'
                 '所有对外出口（/admin/state、租户用量、证据包）字段级脱敏。'
                 '代价如实登记：进程重启后未终态任务不可续跑，会在派发前'
                 '显式判失败（error=prompt_unavailable），绝不拿占位符跑出假结果。'),
    },
    # —— 边缘与端侧 ——
    'edge.offline_autonomy': {
        'label': '边缘断网自治',
        'status': 'implemented',
        'note': 'v4 新增：本地队列 + 断网续跑 + 联网回放 + 计量补偿',
    },
    'edge.box_provision': {
        'label': '算力箱预装与纳管',
        'status': 'implemented',
        'note': 'v4 新增 edge-box-provisioner Skill：预装清单 + 纳管命令 + 断网验收项',
    },
    'device.home_grid': {
        'label': '家庭机主接入与结算',
        'status': 'implemented',
        'note': 'v4 新增极简机主客户端与结算看板（仅软件侧；带宽/断电等真机因素待实测）',
    },
    # —— 中心与交付 ——
    'center.private_cloud_plan': {
        'label': '私有云配置与 TCO 规划',
        'status': 'implemented',
        'note': 'v4 新增 private-cloud-planner Skill：含 vGPU 授权隐性成本，三种交付形态',
    },
    # —— 接入层 ——
    'access.unified_api': {
        'label': '统一 API',
        'status': 'implemented',
        'note': 'OpenAI 兼容网关 + 管理 API，全量鉴权',
    },
    'access.sdk': {
        'label': 'SDK',
        'status': 'implemented',
        'note': 'v4 新增 sdk/wnidia_client.py：零依赖轻客户端 + OpenAPI 导出',
    },
    'access.tenant_console': {
        'label': '多租户控制台 / 运维门户',
        'status': 'implemented',
        'note': 'v4 新增 /portal 只读自助页（用量、账单、门槛、结算）',
    },
    'access.token_gateway': {
        'label': 'Token 计量网关',
        'status': 'implemented',
        'note': 'v4 新增：网关侧统一记账，按租户与口径结算',
    },
    # —— 远期期权 ——
    'future.qpu_abstraction': {
        'label': 'QPU 资源抽象与混合任务接口',
        'status': 'simulated',
        'note': 'v4 以极简 statevector 模拟器承载接口验证；不押技术路线、不引入量子栈',
    },
    'future.pqc': {
        'label': '后量子密码（PQC）预留',
        'status': 'simulated',
        'note': 'v4 仅做签名/校验接口占位与哈希链演示，不代表已具备 PQC 能力',
    },
    'future.robotics_fleet': {
        'label': '具身机队（任务分配 / 模型灰度 / 回传脱敏）',
        'status': 'simulated',
        'note': 'v4 以机队仿真承载；真机需 Isaac/ROS 车体',
    },
    # —— 经营度量 ——
    'ops.gates': {
        'label': 'GO / NO-GO 门槛度量',
        'status': 'implemented',
        'note': 'v4 新增五条量化门槛的实测引擎与 /admin/gates 视图',
    },
    'ops.dcgm_telemetry': {
        'label': 'DCGM 遥测看板',
        'status': 'planned',
        'note': '真机接 DCGM-Exporter + Prometheus + Grafana；沙盒用本地采样替代',
    },
    # —— 明确不做（按 BP 边界）——
    'excluded.multi_vendor': {
        'label': '一云多芯 / 跨厂商通用调度',
        'status': 'excluded',
        'note': '按 BP 边界明确不做；仅保留「CUDA ↔ Apple MLX 同一模型一致性适配」',
    },
    'excluded.cpo': {
        'label': 'CPO 光互联产品化',
        'status': 'excluded',
        'note': '仅做拓扑兼容性证据，不做产品',
    },
    'excluded.token_factory': {
        'label': '自营 Token 工厂 / GPU 云',
        'status': 'excluded',
        'note': '重资产且与客户争利，明确不做',
    },
}

STATUS_LABELS = {
    'implemented': '已实现',
    'partial': '部分实现',
    'decision_only': '有决定无执行',
    'simulated': '沙盒模拟',
    'planned': '仅规划',
    'excluded': '明确不做',
}

# 对外统一措辞：不把 decision_only / simulated 说成"已支持"
OUTWARD_PHRASING = {
    'implemented': '已实现并通过沙盒验证',
    'partial': '已实现主干，真机细节待验证',
    'decision_only': '策略可计算、可解释；执行依赖硬件与真机验证',
    'simulated': '接口与流程已在沙盒中验证；真机能力待接入',
    'planned': '已列入规划，尚未实现',
    'excluded': '按战略边界明确不做',
}


def capability(key):
    c = CAPABILITY_MATRIX.get(key)
    if not c:
        return None
    return dict(c, key=key, status_label=STATUS_LABELS.get(c['status'], c['status']),
                outward=OUTWARD_PHRASING.get(c['status'], ''))


def capability_board():
    """看板 / 答辩页用的能力总表。"""
    counts = {}
    rows = []
    for k, c in CAPABILITY_MATRIX.items():
        counts[c['status']] = counts.get(c['status'], 0) + 1
        rows.append(capability(k))
    return {'counts': counts, 'status_labels': STATUS_LABELS,
            'outward_phrasing': OUTWARD_PHRASING, 'capabilities': rows}


# ---------------------------------------------------------------- 订阅分档
SUBSCRIPTION_TIERS = {
    'P0': {'label': '私有化版', 'price_cny_per_node_year': (8, 12),
           'includes': ['全量能力', '机密计算与审计证据包', '私有云规划', '专属交付'],
           'cc_required': 'CC-L3'},
    'P1': {'label': '标准版', 'price_cny_per_node_year': (5, 8),
           'includes': ['调度基座', 'FinOps 计量与分账', '多租户控制台', '门槛度量'],
           'cc_required': 'CC-L1'},
    'P2': {'label': '轻量版', 'price_cny_per_node_year': (3, 5),
           'includes': ['调度基座', '基础计量', '只读门户'],
           'cc_required': 'CC-L0'},
}


def subscription_catalog():
    return SUBSCRIPTION_TIERS
