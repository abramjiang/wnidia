# -*- coding: utf-8 -*-
"""F3 · 合规策略可配置化

现状问题：调度器里的合规约束是**硬编码**的
（`rank_of(t.secret) >= SECRET_RANK['L2']` → 主权域检查；
 `>= L3` → trusted 与 cc_level 检查），无法按部署环境调整。

本模块把合规约束外置为**可配置的策略**：
    secret_rank -> {require_trusted, min_cc_level, allowed_regions, require_tags, deny_tags}

这样不同部署环境可按自身合规要求配置，无需改代码。

默认策略 `DEFAULT_POLICY` **等价于现有硬编码行为**，保证迁移不回退。
"""

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional


# 机密计算层级顺序（与 capabilities.CC_RANK 保持一致的语义）
CC_ORDER = ['CC-L0', 'CC-L1', 'CC-L2', 'CC-L3']


def cc_rank(cc: str) -> int:
    return CC_ORDER.index(cc) if cc in CC_ORDER else 0


@dataclass
class Rule:
    """某一密级对应的合规约束。"""
    require_trusted: bool = False
    min_cc_level: str = 'CC-L0'
    allowed_regions: List[str] = field(default_factory=lambda: ['local'])
    require_tags: List[str] = field(default_factory=list)
    deny_tags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class CompliancePolicy:
    """合规策略：密级 -> 约束。"""
    name: str = 'default'
    # 键为密级（L0/L1/L2/L3），值为该密级的约束
    rules: Dict[str, Rule] = field(default_factory=dict)

    def rule_for(self, rank: str) -> Rule:
        return self.rules.get(str(rank).upper(), Rule())

    def to_dict(self) -> Dict:
        return {'name': self.name,
                'rules': {k: v.to_dict() for k, v in self.rules.items()}}

    @staticmethod
    def from_dict(d: Dict) -> 'CompliancePolicy':
        rules = {}
        for k, v in (d.get('rules') or {}).items():
            rules[str(k).upper()] = Rule(
                require_trusted=bool(v.get('require_trusted', False)),
                min_cc_level=str(v.get('min_cc_level', 'CC-L0')),
                allowed_regions=list(v.get('allowed_regions') or ['local']),
                require_tags=list(v.get('require_tags') or []),
                deny_tags=list(v.get('deny_tags') or []),
            )
        return CompliancePolicy(name=str(d.get('name', 'custom')), rules=rules)


# ---- 默认策略：等价于现有硬编码行为 ----
DEFAULT_POLICY = CompliancePolicy(name='default', rules={
    'L0': Rule(),
    'L1': Rule(),
    'L2': Rule(allowed_regions=['local']),                       # 主权域必须一致
    'L3': Rule(require_trusted=True, min_cc_level='CC-L2',       # 可信 + 机密算力
               allowed_regions=['local']),
})


def evaluate(node: Dict, task: Dict, policy: CompliancePolicy = None) -> Dict:
    """评估某节点是否满足任务密级的合规约束。

    node: {trusted, cc_level, region, tags}
    task: {secret_rank, region}
    返回 {'allowed': bool, 'reasons': [...]}
    """
    pol = policy or DEFAULT_POLICY
    rank = str(task.get('secret_rank') or 'L1').upper()
    rule = pol.rule_for(rank)
    reasons: List[str] = []

    if rule.require_trusted and not bool(node.get('trusted')):
        reasons.append('密级 %s 要求 trusted 节点' % rank)

    need_cc = rule.min_cc_level or 'CC-L0'
    if cc_rank(str(node.get('cc_level') or 'CC-L0')) < cc_rank(need_cc):
        reasons.append('机密算力层级不足：需 %s，实为 %s'
                       % (need_cc, node.get('cc_level') or 'CC-L0'))

    node_region = str(node.get('region') or 'local')
    allowed = rule.allowed_regions or ['local']
    if allowed and node_region not in allowed:
        reasons.append('主权域不匹配：任务域 %s，节点域 %s'
                       % (task.get('region') or 'local', node_region))

    tags = set(node.get('tags') or [])
    for t in rule.require_tags:
        if t not in tags:
            reasons.append('缺少必需标签: %s' % t)
    for t in rule.deny_tags:
        if t in tags:
            reasons.append('命中禁止标签: %s' % t)

    return {'allowed': not reasons, 'reasons': reasons, 'rank': rank,
            'policy': pol.name}


def _self_test() -> int:
    ok = True

    def check(cond, msg):
        nonlocal ok
        print('  %s %s' % ('PASS' if cond else 'FAIL', msg))
        ok = ok and cond

    trusted = {'trusted': True, 'cc_level': 'CC-L3', 'region': 'local', 'tags': ['secure']}
    plain = {'trusted': False, 'cc_level': 'CC-L0', 'region': 'local', 'tags': []}
    foreign = {'trusted': True, 'cc_level': 'CC-L3', 'region': 'overseas', 'tags': []}

    r = evaluate(plain, {'secret_rank': 'L1'})
    check(r['allowed'], 'L1 在普通节点允许（等价现有行为）')

    r = evaluate(plain, {'secret_rank': 'L3'})
    check(not r['allowed'] and 'trusted' in r['reasons'][0], 'L3 在不可信节点被拒')

    r = evaluate(trusted, {'secret_rank': 'L3'})
    check(r['allowed'], 'L3 在可信+CC-L3 节点允许')

    r = evaluate({'trusted': True, 'cc_level': 'CC-L1', 'region': 'local'},
                 {'secret_rank': 'L3'})
    check(not r['allowed'] and '层级不足' in r['reasons'][0], 'CC 层级不足被拒')

    r = evaluate(foreign, {'secret_rank': 'L2'})
    check(not r['allowed'] and '主权域' in r['reasons'][0], '主权域不匹配被拒')

    # 自定义策略（可配置化验证）
    custom = CompliancePolicy.from_dict({
        'name': 'finance',
        'rules': {'L2': {'require_trusted': True, 'require_tags': ['finance-approved']}}})
    r = evaluate(trusted, {'secret_rank': 'L2'}, custom)
    check(not r['allowed'] and 'finance-approved' in r['reasons'][0],
          '自定义策略生效：缺 finance-approved 标签被拒')
    r = evaluate({**trusted, 'tags': ['finance-approved']}, {'secret_rank': 'L2'}, custom)
    check(r['allowed'], '自定义策略：带正确标签则允许')

    # 序列化往返
    rt = CompliancePolicy.from_dict(DEFAULT_POLICY.to_dict())
    check(rt.rule_for('L3').require_trusted is True, '策略序列化往返正确')

    # 未知密级不应崩溃
    r = evaluate(trusted, {'secret_rank': 'L9'})
    check(r['allowed'], '未知密级回落默认规则，不崩溃')

    print('  自检:', 'ALL PASS' if ok else 'HAS FAILURE')
    return 0 if ok else 1


if __name__ == '__main__':
    import sys
    sys.exit(_self_test())
