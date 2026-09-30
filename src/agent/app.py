# -*- coding: utf-8 -*-
"""WNIDIA Agent 应用层：自然语言 → 选 Skill → 执行 → 回报。

暴露在节点 7000 端口（公网 7026），与看板 8888、API 9000 并列。
鉴权：Bearer Token（与 WNIDIA_TOKEN 同源）。
Skill 执行：调用 skills/<name>/tool.py（CLI + JSON 输出），不重复实现业务逻辑。
"""
import html
import json
import os
import re
import subprocess
import sys
import tempfile
from typing import Optional

import requests
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
TOKEN = os.getenv('WNIDIA_TOKEN', 'changeme')
CTRL = os.getenv('CTRL', 'http://127.0.0.1:9000')
VLLM_BASE = os.getenv('VLLM_BASE', 'http://127.0.0.1:11434/v1')
VLLM_MODEL = os.getenv('VLLM_MODEL', 'stepfun')
LLM_TIMEOUT = int(os.getenv('AGENT_LLM_TIMEOUT', '90'))
GPU_MEM_GB = float(os.getenv('AGENT_GPU_MEM_GB', '128'))   # GB10 统一内存

app = FastAPI(title='WNIDIA Agent', version='1.0.0')

# ---------------------------------------------------------------- 鉴权
def need_token(authorization: Optional[str] = Header(default=None)):
    if authorization != f'Bearer {TOKEN}':
        raise HTTPException(status_code=401, detail='invalid token')
    return True


# ---------------------------------------------------------------- Skill 注册表
SKILLS = {
    'smart-dispatcher': {
        'desc': '给定任务与节点状态，产出可解释的调度决策（准入、目标节点、评分、是否抢占）',
        'keywords': ['派发', '调度', '该给谁', '派给', '发给', '选哪个节点', '哪个节点',
                     '为什么选', '抢占', '能不能接', '拒绝', '路由', 'dispatch', 'schedule'],
    },
    'gpu-slicer': {
        'desc': '按显存与负载给出 GPU 切分方案（MIG / MPS / 多实例）与启动命令',
        'keywords': ['切分', '显存', 'mps', 'mig', '多实例', '开几个', '怎么分', 'slice'],
    },
    'resource-doctor': {
        'desc': '体检集群：利用率、掉线、配额、信誉分异常，给出诊断与处置建议',
        'keywords': ['诊断', '体检', '利用率', '为什么低', '健康', '异常', '掉线', 'doctor'],
    },
    'idle-onboarding': {
        'desc': '纳管闲置/新增节点：探活、注册、初始化画像与配额',
        'keywords': ['纳管', '加入', '接入', '新增节点', '闲置', '注册节点', 'onboard'],
    },
    'engine-selector': {
        'desc': '在 mock/ollama/vllm/tensorfold 之间做引擎选路：'
                '结合任务类型、数据密级、节点档位与实时探活，'
                '给出目标引擎、评分依据、降级链与一致性保证',
        'keywords': ['引擎', '推理引擎', '后端', '换引擎', '哪个引擎', '可复现',
                     '一致性', '逐字节', '可举证', 'token_sha', '降级', '回退',
                     'engine', 'backend', 'tensorfold', 'vllm', 'ollama',
                     '为什么这么慢', '加速'],
    },
    'edge-box-provisioner': {
        'desc': '算力箱预装与纳管方案：NVIDIA 全栈预装清单、纳管命令、'
                '断网验收项与订阅档位建议',
        'keywords': ['算力箱', '边缘箱', '一体机', 'jetson', 'thor', 'orin',
                     '预装', '现场部署', 'edge box'],
    },
    'private-cloud-planner': {
        'desc': '私有云配置与 TCO：按并发与合规等级出节点清单与三年成本，'
                '并把 vGPU 授权隐性成本单列',
        'keywords': ['私有云', '私有化', '并发路数', 'tco', '配置清单',
                     '报价', 'vgpu', '租赁', '托管', '机房'],
    },
    'gate-inspector': {
        'desc': 'GO/NO-GO 门槛体检：五条量化门槛的实测值、目标、样本量与判定；'
                '样本不足时拒绝判 GO',
        'keywords': ['门槛', 'go', 'no-go', '收缩', '扩张', '样本',
                     '达标', 'gate'],
    },
    'cc-auditor': {
        'desc': '机密计算层级与审计链体检：secret→cc 映射、节点能力与证明校验、'
                '审计哈希链完整性检测',
        'keywords': ['机密计算', 'tee', '远程证明', '证明', '密钥', '审计链',
                     '哈希链', '篡改', 'cc-l2', 'cc-l3', '数据不出域',
                     '可用不可见'],
    },
}


# ---------------------------------------------------------------- 规则路由
def rule_route(text: str):
    t = text.lower()
    best, score = None, 0
    for name, meta in SKILLS.items():
        s = sum(1 for k in meta['keywords'] if k in t)
        if s > score:
            best, score = name, s
    return best if score else None


# ---------------------------------------------------------------- 参数抽取
def num_after(text, unit_hint=('gb', 'g', 'b')):
    m = re.search(r'(\d+(?:\.\d+)?)\s*(?:gb|g)\b', text.lower())
    return float(m.group(1)) if m else None


def extract_ip(text):
    m = re.search(r'\b(\d{1,3}(?:\.\d{1,3}){3})\b', text)
    return m.group(1) if m else None


def build_args(skill: str, text: str) -> dict:
    t = text.lower()
    if skill == 'smart-dispatcher':
        sla = 'sla-3' if ('离线' in t or 'spot' in t or '批处理' in t) else \
              ('sla-1' if ('实时' in t or '高优' in t) else 'sla-2')
        secret = 'L3' if ('机密' in t or '绝密' in t or '敏感' in t or '不出门' in t) \
            else ('L1' if '公开' in t else 'L2')
        m = re.search(r'(\d+)\s*(?:token|tokens|tok)', t)
        tokens = int(m.group(1)) if m else 512
        mem = num_after(t) or 2.0
        return {'task': {'prompt': text, 'task_type': 'chat', 'sla': sla,
                         'secret': secret, 'tokens_in': tokens,
                         'need_mem_gb': round(min(mem, 8), 1)}}
    if skill == 'gpu-slicer':
        # 区分"总显存"与"模型权重"：紧跟着 显存/内存/统一内存 的数字是总量
        gpu_mem = GPU_MEM_GB
        weights = []
        for m in re.finditer(r'(\d+(?:\.\d+)?)\s*(?:gb|g)\b', t):
            tail = t[m.end():m.end() + 6]
            if re.search(r'^\s*(显存|内存|统一内存)', tail) or \
               re.search(r'(显存|统一内存)\s*$', t[max(0, m.start() - 6):m.start()]):
                gpu_mem = float(m.group(1))
            else:
                weights.append(float(m.group(1)))
        weights = weights[:3] or [18.0]
        models = [{'name': f'model-{i}', 'weights_gb': w,
                   'kv_gb': round(w * 0.35, 1), 'concurrency': 4}
                  for i, w in enumerate(weights)]
        mode = 'mps' if 'mps' in t else ('mig' if 'mig' in t else 'auto')
        return {'gpu_mem': gpu_mem, 'workload': {'models': models}, 'mode': mode}
    if skill == 'resource-doctor':
        return {}
    if skill == 'idle-onboarding':
        node = extract_ip(text) or 'new-node'
        tier = 'home' if ('家用' in t or '端侧' in t or '手机' in t or 'iphone' in t) \
            else ('cpu' if 'cpu' in t else 'edge')
        role = 'cpu' if ('cpu' in t or '手机' in t or 'iphone' in t) else 'decode'
        mem = num_after(t) or (2.0 if tier == 'home' else 8.0)
        dry = not ('真的' in t or '确认' in t or '实际注册' in t)
        return {'node': node, 'tier': tier, 'role': role,
                'mem_limit_gb': mem, 'compute_pct': 40 if tier != 'edge' else 60,
                'vllm_port': 8104, 'worker_host': node, 'dry_run': dry}
    if skill == 'engine-selector':
        for name in ('tensorfold', 'vllm', 'ollama', 'mock'):
            if name in t:
                engine_pref = name
                break
        else:
            engine_pref = None
        secret = 'L3' if ('机密' in t or '绝密' in t or '敏感' in t) else 'L1'
        task_type = 'heavy' if ('批量' in t or '长文本' in t or '长文' in t) \
            else 'chat'
        tier = 'cloud' if '云' in t else ('home' if '端侧' in t else 'edge')
        return {'task_type': task_type, 'secret': secret, 'tier': tier,
                'prefer_exact': bool('可复现' in t or '一致' in t
                                     or '举证' in t or '审计' in t),
                'engine_pref': engine_pref}
    if skill == 'edge-box-provisioner':
        gpu = 'orin' if ('orin' in t or '老' in t or '上一代' in t) else 'thor'
        n = re.search(r'(\d+)\s*(?:台|个|箱|nodes?)', t)
        nodes = int(n.group(1)) if n else 1
        cc = 'CC-L2' if ('证明' in t or '机密' in t) else 'CC-L0'
        return {'gpu': gpu, 'nodes': nodes, 'cc_level': cc}
    if skill == 'private-cloud-planner':
        m = re.search(r'(\d+)\s*(?:路|并发)', t)
        conc = int(m.group(1)) if m else 60
        grade = 'L4' if ('绝密' in t or '强主权' in t) else \
            ('L3' if ('机密' in t or '金融' in t or '医疗' in t) else 'L2')
        delivery = 'lease' if '租赁' in t else ('hosted' if '托管' in t
                                            else 'onprem')
        return {'concurrency': conc, 'grade': grade, 'delivery': delivery,
                'use_vgpu': bool('vgpu' in t or '切分授权' in t)}
    if skill == 'gate-inspector':
        return {}
    if skill == 'cc-auditor':
        secret = 'L4' if ('绝密' in t or '强主权' in t) else \
            ('L3' if ('机密' in t or '敏感' in t) else 'L1')
        return {'secret': secret, 'node': extract_ip(t)}
    return {}


# ---------------------------------------------------------------- 执行 Skill
def _write_private_json(obj):
    """把含明文的对象写进 0600 临时文件，返回路径（失败返回 None）。

    为什么不用命令行/环境变量：`ps`、`/proc/<pid>/cmdline`、`/proc/<pid>/environ`
    对同机同用户都可见；shell history 更会长期留存。任务里的 prompt 是明文，
    必须走"文件 + 收权限 + 用完即删"这条路。
    """
    try:
        d = os.path.join(ROOT, 'data', 'tmp')
        os.makedirs(d, mode=0o700, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix='task-', suffix='.json', dir=d)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False)
        return path
    except OSError:
        return None


def run_skill(skill: str, args: dict) -> dict:
    if skill not in SKILLS:
        return {'ok': False, 'error': f'unknown skill {skill}'}
    skill_root = os.path.realpath(os.path.join(ROOT, 'skills'))
    path = os.path.realpath(os.path.join(skill_root, skill, 'tool.py'))
    # 路径穿越防护：skill 名受 SKILLS 白名单约束，但仍做一次规范化校验
    if not path.startswith(skill_root + os.sep) or not os.path.isfile(path):
        return {'ok': False, 'error': f'invalid skill path for {skill}'}
    cmd = [PY, path]
    # Token 不再作为命令行参数（ps/历史可见），改为通过子进程环境变量传递
    child_env = dict(os.environ, WNIDIA_TOKEN=TOKEN, CTRL=CTRL)
    tmp_task = None
    if skill == 'smart-dispatcher':
        # v5：任务里含 **prompt 明文**，不能再走命令行 —— `ps`、`/proc/*/cmdline`
        # 和 shell history 都能看到（v3 只把 Token 移出了 argv，把 prompt 漏了）。
        # 改为写 0600 临时文件，跑完即删。
        tmp_task = _write_private_json(args.get('task', {}))
        if tmp_task:
            cmd += ['--task-file', tmp_task, '--ctrl', CTRL]
        else:
            # 落盘失败时退回命令行，但明确告知：这是降级，不是默认行为
            cmd += ['--task', json.dumps(args.get('task', {}),
                                         ensure_ascii=False), '--ctrl', CTRL]
    elif skill == 'gpu-slicer':
        cmd += ['--gpu-mem', str(args.get('gpu_mem', GPU_MEM_GB)),
                '--workload', json.dumps(args.get('workload', {'models': []}),
                                         ensure_ascii=False),
                '--mode', args.get('mode', 'auto')]
        if args.get('supports_mig'):
            cmd.append('--supports-mig')
    elif skill == 'resource-doctor':
        cmd += ['--ctrl', CTRL]
    elif skill == 'idle-onboarding':
        cmd += ['--node', str(args.get('node', 'new-node')),
                '--tier', args.get('tier', 'edge'),
                '--role', args.get('role', 'decode'),
                '--mem-limit-gb', str(args.get('mem_limit_gb', 8)),
                '--compute-pct', str(args.get('compute_pct', 60)),
                '--vllm-port', str(args.get('vllm_port', 8104)),
                '--worker-host', str(args.get('worker_host', '127.0.0.1')),
                '--engine', str(args.get('engine', 'mock')),
                '--ctrl', CTRL]
        if args.get('dry_run', True):
            cmd.append('--dry-run')
        if args.get('trusted'):
            cmd.append('--trusted')
    elif skill == 'engine-selector':
        cmd += ['--task-type', str(args.get('task_type', 'chat')),
                '--secret', str(args.get('secret', 'L1')),
                '--tier', str(args.get('tier', 'edge')),
                '--ctrl', CTRL, '--live']
        if args.get('prefer_exact'):
            cmd.append('--prefer-exact')
        if args.get('engine_pref'):
            cmd += ['--engine-pref', str(args['engine_pref'])]
    elif skill == 'edge-box-provisioner':
        cmd += ['--gpu', str(args.get('gpu', 'thor')),
                '--nodes', str(int(args.get('nodes', 1) or 1)),
                '--cc-level', str(args.get('cc_level', 'CC-L0')),
                '--ctrl', CTRL, '--live']
    elif skill == 'private-cloud-planner':
        cmd += ['--concurrency', str(int(args.get('concurrency', 60) or 60)),
                '--grade', str(args.get('grade', 'L2')),
                '--delivery', str(args.get('delivery', 'onprem'))]
        if args.get('use_vgpu'):
            cmd.append('--use-vgpu')
    elif skill == 'gate-inspector':
        cmd += ['--ctrl', CTRL, '--live']
    elif skill == 'cc-auditor':
        cmd += ['--secret', str(args.get('secret', 'L1')),
                '--ctrl', CTRL, '--live']
        if args.get('node'):
            cmd += ['--node', str(args['node'])]
    try:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                           timeout=90, env=child_env)
        out = p.stdout.strip()
        data = json.loads(out) if out.startswith('{') or out.startswith('[') \
            else {'raw': out}
        return {'ok': p.returncode == 0, 'data': data, 'stderr': p.stderr[-300:]}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': 'skill timeout'}
    except Exception as e:      # noqa: BLE001
        return {'ok': False, 'error': str(e)[:200]}
    finally:
        # 含明文的临时文件用完即删（不依赖进程正常退出）
        if tmp_task:
            try:
                os.unlink(tmp_task)
            except OSError:
                pass


# ---------------------------------------------------------------- LLM（可选）
def llm(messages, max_tokens=400):
    try:
        r = requests.post(f'{VLLM_BASE}/chat/completions', json={
            'model': VLLM_MODEL, 'messages': messages,
            'max_tokens': max_tokens, 'temperature': 0.2}, timeout=LLM_TIMEOUT)
        r.raise_for_status()
        return r.json()['choices'][0]['message']['content']
    except Exception:      # noqa: BLE001
        return None


def llm_route(text: str):
    """让本地模型选 Skill 并抽参数；失败返回 None 交给规则兜底。"""
    menu = '\n'.join(f"- {k}: {v['desc']}" for k, v in SKILLS.items())
    prompt = (f'你是算力调度助手。从下列 Skill 中选一个最匹配的（或返回 chat 表示闲聊），'
              f'并给出参数。只输出 JSON：{{"skill":"<名字或chat>","args":{{}}}}\n\n'
              f'可用 Skill：\n{menu}\n\n用户请求：{text}')
    out = llm([{'role': 'user', 'content': prompt}], 200)
    if not out:
        return None
    m = re.search(r'\{[\s\S]*\}', out)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
        if d.get('skill') in SKILLS or d.get('skill') == 'chat':
            return d
    except Exception:      # noqa: BLE001
        return None
    return None


# ---------------------------------------------------------------- 结果解读
def summarize(skill: str, data: dict) -> str:
    """把 Skill 的 JSON 讲成人话；不依赖 LLM，保证离线可用。"""
    try:
        if skill == 'smart-dispatcher':
            ad = data.get('admitted', '?')
            node = data.get('node') or data.get('target') or '—'
            reason = data.get('reason') or data.get('explanation') or ''
            pre = data.get('preempt') or {}
            s = f'调度决策：准入={ad}，目标节点={node}。'
            if reason:
                s += f' 依据：{reason}。'
            if pre.get('needed'):
                s += f" 建议抢占低优任务 {pre.get('spot_task')}。"
            rk = data.get('ranking') or data.get('candidates')
            if rk:
                s += ' 候选排名：' + '、'.join(
                    str(r.get('node', r)) for r in rk[:3])
            return s
        if skill == 'gpu-slicer':
            ins = data.get('instances') or []
            mode = data.get('selected_mode') or data.get('mode') or 'auto'
            s = (f"切分方案（{mode}）：总显存 {data.get('gpu_mem_gb','?')}GB，"
                 f"可用 {data.get('usable_gb','?')}GB，已分配 {data.get('allocated_gb','?')}GB。")
            for i in ins[:4]:
                s += (f"\n· {i.get('name','实例')}：{i.get('mem_gb','?')}GB，"
                      f"gpu_memory_utilization={i.get('gpu_memory_utilization','?')}，"
                      f"端口 {i.get('port','?')}，角色 {i.get('role','?')}")
            if data.get('fallback'):
                s += '\n回退：' + '；'.join(str(x) for x in data['fallback'][:2])
            return s
        if skill == 'resource-doctor':
            issues = data.get('issues') or data.get('findings') or []
            s = '集群体检：'
            if issues:
                s += '；'.join(str(i.get('msg', i)) for i in issues[:5])
            else:
                s += '未发现异常。'
            adv = data.get('advice') or data.get('suggestions')
            if adv:
                s += ' 建议：' + '；'.join(str(a) for a in adv[:3])
            return s
        if skill == 'idle-onboarding':
            prof = data.get('profile', {})
            health = data.get('health', {})
            init = data.get('initial', {})
            if data.get('blocked_by_compliance'):
                return ('纳管被合规拦截：' + str(data.get('error', ''))[:200]
                        + ' 建议：' + '；'.join(data.get('next_steps', [])[:2]))
            s = (f"纳管方案（dry-run={data.get('dry_run', True)}，"
                 f"已纳管={data.get('onboarded', False)}）："
                 f"节点 {data.get('node','?')} / {prof.get('tier','?')} 档 / "
                 f"{prof.get('role','?')}，显存上限 {prof.get('mem_limit_gb','?')}GB，"
                 f"端口 {prof.get('vllm_port','?')}，引擎 {prof.get('engine','?')}。")
            s += (f"探活：{'可达' if health.get('reachable') else '暂不可达'}"
                  f"（{health.get('url','')}）。")
            if init:
                s += (f"初始信誉 {init.get('reputation','?')}、"
                      f"配额 {init.get('quota','?')}、状态 {init.get('status','?')}。")
            # 修复：原实现读 data['next']，而工具实际返回 next_steps，导致
            # "下一步"提示永远不显示。
            steps = data.get('next_steps') or ([data['next']] if data.get('next')
                                               else [])
            if steps:
                s += '下一步：' + '；'.join(str(x) for x in steps[:3])
            return s
        if skill == 'engine-selector':
            sel = data.get('selected')
            if not sel:
                return f"引擎选路失败：{data.get('error', '无可用引擎')}"
            s = (f"引擎选路：建议使用 {sel}"
                 f"（{data.get('selected_label','')}，评分 {data.get('score')}）。")
            if data.get('reason'):
                s += ' 依据：' + '；'.join(str(x) for x in data['reason'][:4]) + '。'
            s += f" 一致性：{data.get('consistency_guarantee','—')}"
            if data.get('consistency_required'):
                s += ('（本任务要求可复现：'
                      + ('已满足' if data.get('consistency_satisfied')
                         else '⚠ 未满足，请注意举证风险') + '）')
            fb = data.get('fallback_chain') or []
            if fb:
                s += ' 降级链：' + ' → '.join(str(x) for x in fb[:3]) + '。'
            if data.get('requested_engine') and not data.get('requested_honored'):
                s += f" 注意：点名的 {data['requested_engine']} 未被采用。"
            return s
        if skill == 'edge-box-provisioner':
            box = data.get('box', {})
            pl = data.get('placement', {})
            s = (f"算力箱方案：{box.get('label','?')} × {data.get('nodes',1)} 台"
                 f"（{box.get('mem_gb','?')}GB 统一内存，"
                 f"{box.get('model_class','?')} 模型，功耗 {pl.get('power_budget_w','?')}W）。")
            s += f" 预装 {len(data.get('preinstall', []))} 类软件栈。"
            s += (f" 订阅建议：{data.get('subscription_hint', {}).get('tier','?')}。"
                  f" 断网验收 {len(data.get('offline_acceptance', []))} 项。")
            return s
        if skill == 'private-cloud-planner':
            t = data.get('topology', {})
            tco = data.get('tco', {})
            hc = data.get('hidden_cost_warning', {})
            s = (f"私有云配置（{data.get('concurrency')} 路并发 / "
                 f"{data.get('grade')} 级）：GPU 节点 {t.get('gpu_nodes')}、"
                 f"CPU 节点 {t.get('cpu_nodes')}、管理节点 "
                 f"{len(data.get('capex', {}).get('items', []))} 类设备，"
                 f"机柜 {t.get('racks')}。")
            total = next((v for k, v in tco.items() if k.startswith('total')), None)
            if total:
                s += f" 三年 TCO 约 {total} 元。"
            if hc.get('per_year_cny'):
                s += (f" ⚠ 隐性成本：{hc.get('item')} {hc.get('per_year_cny')} 元/年，"
                      f"报价必须单列。")
            return s
        if skill == 'gate-inspector':
            counts = data.get('counts', {})
            s = (f"门槛体检结论：{data.get('overall')}"
                 f"（达标 {counts.get('go',0)} / 未达标 {counts.get('no-go',0)} / "
                 f"样本不足 {counts.get('insufficient',0)}）。")
            bad = [g for g in data.get('gates', []) if g.get('verdict') == 'no-go']
            if bad:
                s += ' 需要收缩的线：' + '；'.join(
                    f"{g['label']}（{g['reason']}）" for g in bad[:3]) + '。'
            ins = [g for g in data.get('gates', [])
                   if g.get('verdict') == 'insufficient']
            if ins:
                s += f" 另有 {len(ins)} 条样本不足，不得判 GO。"
            if data.get('policy'):
                s += f" 处置原则：{data['policy']}"
            return s
        if skill == 'cc-auditor':
            if not data.get('nodes_checked'):
                return f"机密计算体检失败：{data.get('error', '无节点数据')}"
            s = (f"机密计算体检：数据密级 {data.get('secret')} → 要求 "
                 f"{data.get('required_cc')}；核验 {data.get('nodes_checked')} 个节点，"
                 f"准入结论 {data.get('admission_result')}。")
            okn = data.get('serviceable_nodes') or []
            bn = data.get('blocked_nodes') or []
            if okn:
                s += ' 可用节点：' + '、'.join(str(x) for x in okn[:4]) + '。'
            if bn:
                s += ' 被拦节点：' + '、'.join(str(x) for x in bn[:4]) + '。'
            ch = data.get('chain') or {}
            if ch.get('ok') is True:
                s += f" 审计链完整（{ch.get('length')} 条）。"
            elif ch.get('ok') is False:
                s += f" ⚠ 审计链异常：{ch.get('broken')}。"
            s += (' 提醒：secret 与 cc 是两套 L1–L4，对外必须带前缀。')
            return s
    except Exception:      # noqa: BLE001
        pass
    return json.dumps(data, ensure_ascii=False)[:400]


# ---------------------------------------------------------------- 接口
class ChatReq(BaseModel):
    message: str
    use_llm: bool = True


class ProposeReq(BaseModel):
    task: dict                       # task_type / tokens_in / secret / sla / need_mem_gb
    candidates: list                 # 控制面给的候选节点画像（只能在这里面选）
    recent_vetoes: list = []         # 最近被否理由（闭环回流，Agent 自我修正）


@app.get('/healthz')
def healthz():
    return {'ok': True, 'skills': list(SKILLS),
            'policy': 'propose-ready'}


@app.get('/v1/agent/skills')
def list_skills(authorization: Optional[str] = Header(default=None)):
    if authorization != f'Bearer {TOKEN}':
        raise HTTPException(status_code=401, detail='invalid token')
    return {'skills': {k: v['desc'] for k, v in SKILLS.items()}}


# ---------------- 调度提议（v5.1：Agent 从解说员到决策者的提议出口） ----------------
def _propose_rule(task: dict, candidates: list) -> dict:
    """确定性兜底提议：角色匹配 + 余量优先。

    与控制面内核的打分**刻意不同源**（内核是多目标加权，这里只看两个维度），
    shadow 模式下才能度量出「Agent 路径 vs 内核路径」的真实分歧。
    """
    if not candidates:
        return {}
    want = 'prefill' if (task.get('task_type') == 'heavy'
                         or int(task.get('tokens_in') or 0) > 1500) else 'decode'
    pref = [c for c in candidates if c.get('role') == want] or candidates
    best = max(pref, key=lambda c: (float(c.get('free_mem_gb') or 0),
                                    -float(c.get('util_pct') or 0)))
    return {'target_node': best['node'],
            'reason': f'{want} 角色匹配且空闲显存最大'
                      f'（{best.get("free_mem_gb")}GB）',
            'confidence': 0.5, 'source': 'rule'}


@app.post('/v1/agent/propose')
def propose(req: ProposeReq, authorization: Optional[str] = Header(default=None)):
    """给控制面的调度提议：只在候选集内选，返回结构化 JSON。

    约束（与 agent_policy.adjudicate 配套）：
    - target_node 必须取自 candidates，集外选择会被控制面判 invalid；
    - LLM 不可用/坏 JSON 时回落 _propose_rule，并如实标注 source；
    - recent_vetoes 回流进提示词，让 Agent 避开历史 veto 的坑。
    """
    if authorization != f'Bearer {TOKEN}':
        raise HTTPException(status_code=401, detail='invalid token')
    cands = req.candidates or []
    if not cands:
        return {'target_node': None, 'reason': '无候选节点',
                'confidence': 0, 'source': 'rule'}
    names = '、'.join(f"{c['node']}"
                      for c in cands[:8])
    veto_note = ''
    if req.recent_vetoes:
        veto_note = '\n最近被裁决否决的原因（务必避开）：\n' + '\n'.join(
            f"- {v['veto']}（{v['count']}次）" for v in req.recent_vetoes)
    t = req.task or {}
    prompt = (
        '你是异构算力集群的调度 Agent。为下面这个任务从候选节点中选一个最合适的，'
        '只输出 JSON，不要解释其他内容：\n'
        '{"target_node":"<节点名>","reason":"<一句话依据>",'
        '"confidence":<0到1>}\n\n'
        f'任务：类型={t.get("task_type")}，输入={t.get("tokens_in")}tokens，'
        f'密级={t.get("secret")}，SLA={t.get("sla")}，'
        f'需显存={t.get("need_mem_gb")}GB，'
        f'时延预算={t.get("latency_budget_ms") or "无"}ms\n'
        f'候选节点（只能从中选）：{names}\n'
        f'{veto_note}\n'
        '选型依据：decode 类看低时延与显存余量；heavy/batch 类看低利用率与余量；'
        '高密级任务优先信誉分。')
    out = llm([{'role': 'user', 'content': prompt}], 120)
    if out:
        m = re.search(r'\{[\s\S]*\}', out)
        if m:
            try:
                d = json.loads(m.group(0))
                node = d.get('target_node')
                if node and any(c.get('node') == node for c in cands):
                    try:
                        conf = max(0.0, min(1.0, float(d.get('confidence', 0.7))))
                    except (TypeError, ValueError):
                        conf = 0.7
                    return {'target_node': node,
                            'reason': str(d.get('reason', ''))[:120],
                            'confidence': conf, 'source': 'llm'}
            except Exception:          # noqa: BLE001  坏 JSON → 规则兜底
                pass
    rule = _propose_rule(t, cands)
    if not rule:
        return {'target_node': None, 'reason': '无候选节点',
                'confidence': 0, 'source': 'rule'}
    return rule


@app.post('/v1/agent/chat')
def chat(req: ChatReq, authorization: Optional[str] = Header(default=None)):
    if authorization != f'Bearer {TOKEN}':
        raise HTTPException(status_code=401, detail='invalid token')
    text = req.message.strip()
    route = None
    if req.use_llm:
        route = llm_route(text)
    skill = route.get('skill') if route else None
    args = route.get('args') if route and isinstance(route.get('args'), dict) else None
    source = 'llm'
    if not skill or skill == 'chat':
        skill = rule_route(text) or 'chat'
        source = 'rule'
    if skill == 'chat':
        ans = llm([{'role': 'user',
                    'content': '你是 WNIDIA 算力调度助手，用中文简洁回答：' + text}])
        return {'skill': 'chat', 'source': source, 'ok': True,
                'answer': ans or '我是 WNIDIA 调度助手：可以问我"这个任务该发给谁""显存怎么切"'
                                 '"集群为什么利用率低""怎么纳管一台闲置设备"。'}
    if not args:
        args = build_args(skill, text)
        source += '+rule-args'
    res = run_skill(skill, args)
    if not res.get('ok'):
        return {'skill': skill, 'source': source, 'ok': False,
                'args': args, 'error': res.get('error') or res.get('stderr'),
                'answer': f'执行 {skill} 失败：{res.get("error") or res.get("stderr", "未知错误")}'}
    data = res['data']
    brief = summarize(skill, data)
    final = llm([{'role': 'user', 'content':
                  f'用一句通顺的中文把下面的调度结论复述给用户，不要编造新信息：\n{brief}'}])
    return {'skill': skill, 'source': source, 'ok': True, 'args': args,
            'raw': data, 'answer': final or brief}


@app.post('/v1/agent/run')
def run(skill: str, args: Optional[dict] = None,
        authorization: Optional[str] = Header(default=None)):
    if authorization != f'Bearer {TOKEN}':
        raise HTTPException(status_code=401, detail='invalid token')
    res = run_skill(skill, (args or {}) or build_args(skill, ''))
    return {'skill': skill, **res}


# ---------------------------------------------------------------- 简易对话页
GATE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>WNIDIA Agent · 登录</title>
<style>body{margin:0;background:#f7f8fa;color:#1f2328;
font:14px/1.6 -apple-system,"PingFang SC",sans-serif;display:flex;
align-items:center;justify-content:center;height:100vh}
.box{background:#fff;border:1px solid #e3e6ea;border-radius:10px;padding:22px;width:340px}
input{width:100%;border:1px solid #e3e6ea;border-radius:8px;padding:8px;margin:10px 0}
button{width:100%;border:0;background:#2563eb;color:#fff;border-radius:8px;padding:8px;cursor:pointer}
p{color:#6b7280;font-size:12px;margin:6px 0}</style></head><body>
<div class="box"><h3 style="margin:0 0 4px">WNIDIA Agent</h3>
<p>请输入访问 Token（与 API 相同）</p>
<input id="t" placeholder="Token"><button onclick="go()">进入</button>
<p>本页与所有接口均需鉴权，未携带 Token 不会返回任何集群数据。</p></div>
<script>function go(){var v=document.getElementById('t').value.trim();
if(v) location.href='/?token='+encodeURIComponent(v);}</script>
</body></html>"""


@app.get('/', response_class=HTMLResponse)
def ui(token: str = ''):
    # 未携带有效 Token 只给登录页，不返回任何集群数据（公网服务鉴权要求）
    if token != TOKEN:
        return HTMLResponse(GATE)
    # Token 会被回显到 HTML 里：先做转义，避免含引号/尖括号的 Token 破坏页面结构
    safe = html.escape(token, quote=True)
    return HTMLResponse(PAGE.replace('__TOKEN__', safe))


PAGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>WNIDIA Agent</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{margin:0;background:#f7f8fa;color:#1f2328;font:14px/1.6 -apple-system,"PingFang SC",sans-serif}
.wrap{max-width:760px;margin:0 auto;padding:18px}
h1{font-size:17px;margin:0 0 2px}.sub{color:#6b7280;font-size:12px;margin-bottom:14px}
.box{background:#fff;border:1px solid #e3e6ea;border-radius:10px;padding:12px}
#log{height:420px;overflow:auto;background:#fff;border:1px solid #e3e6ea;border-radius:10px;padding:12px}
.msg{margin-bottom:12px}.who{font-size:12px;color:#6b7280}
.me{color:#2563eb}.ai{color:#16a34a}
.tag{font-size:11px;padding:1px 7px;border-radius:20px;background:#eff5ff;color:#2563eb;border:1px solid #bfd4ff;margin-left:6px}
pre{background:#f7f8fa;border:1px solid #e3e6ea;border-radius:8px;padding:8px;overflow:auto;font-size:11.5px}
.row{display:flex;gap:8px;margin-top:10px}
input,textarea{border:1px solid #e3e6ea;border-radius:8px;padding:8px;font-size:13px;width:100%}
button{border:1px solid #2563eb;background:#2563eb;color:#fff;border-radius:8px;padding:8px 14px;cursor:pointer}
.ex{font-size:12px;color:#6b7280;margin-top:8px}
.ex b{color:#2563eb;cursor:pointer;font-weight:400}
</style></head><body><div class="wrap">
<h1>WNIDIA 算力调度 Agent</h1>
<div class="sub">自然语言进 → 自动选 Skill → 真实执行 → 中文回报</div>
<div class="box"><div class="who">Token</div>
<input id="tok" value="__TOKEN__" placeholder="Bearer Token"></div>
<div id="log" style="margin-top:10px"></div>
<div class="row"><textarea id="q" rows="2" placeholder="例如：现在有一个机密的长文本任务，该发给谁？"></textarea>
<button onclick="ask()">发送</button></div>
<div class="ex">试试：
<b onclick="fill('这个高优实时任务该派给哪个节点？')">高优任务该派给谁</b> ·
<b onclick="fill('128G 显存要同时跑 27B 和 3B 两个模型，怎么切分？')">显存怎么切分</b> ·
<b onclick="fill('帮我体检一下集群，为什么利用率上不去')">集群体检</b> ·
<b onclick="fill('把家里那台旧 Mac 纳管进来')">纳管闲置设备</b> ·
<b onclick="fill('这个机密任务要求结论可举证，该用哪个推理引擎？')">该用哪个引擎</b></div>
</div><script>
function fill(t){document.getElementById('q').value=t}
function add(who,cls,html){var d=document.createElement('div');d.className='msg';
 d.innerHTML='<div class="who '+cls+'">'+who+'</div>'+html;
 var l=document.getElementById('log');l.appendChild(d);l.scrollTop=l.scrollHeight}
async function ask(){
 var q=document.getElementById('q').value.trim(); if(!q) return;
 var tok=document.getElementById('tok').value.trim();
 add('你','me','<div>'+q+'</div>'); document.getElementById('q').value='';
 add('Agent','ai','<div style="color:#6b7280">思考中…</div>');
 var log=document.getElementById('log'); var last=log.lastChild;
 try{
  var r=await fetch('/v1/agent/chat',{method:'POST',
   headers:{'Content-Type':'application/json','Authorization':'Bearer '+tok},
   body:JSON.stringify({message:q})});
  var j=await r.json();
  var tag=j.skill&&j.skill!=='chat'?'<span class="tag">Skill: '+j.skill+'</span>':'';
  var raw=j.raw?'<details><summary>原始 JSON</summary><pre>'+
    JSON.stringify(j.raw,null,1)+'</pre></details>':'';
  last.innerHTML='<div>'+(j.answer||j.error||'?')+tag+raw+'</div>';
 }catch(e){ last.innerHTML='<div style="color:#dc2626">请求失败：'+e+'</div>'; }
}
</script></body></html>"""
