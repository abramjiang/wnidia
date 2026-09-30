# -*- coding: utf-8 -*-
"""JEV local-offline 离线评审引擎校准测试（L0 + L1）。

纯单元、确定性、不联网：
  A 归一化（NFKC / 百分比 / 内存 / 质量 / 长度 / 时间 / 中文数字 / 虚词 / 歧义保护）
  B 相似度信号基本性质
  C 标注对准确率（同义 POS 需 >=0.8；硬负例 NEG 需 <0.5）
  D L1 本地 embedding：Embedder 解析与缓存、compare 融合、异常/关闭降级、
    L1 对"数字换位（提升/下降方向）"的补位
  E 可解释 / 置信度字段

用法： python tests/jev_offline_test.py ；退出码 0 表示全部通过。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from controller import jev_offline as J

results = []


def check(name, cond, detail=''):
    results.append((name, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")


R = '已完成调度：该任务按 SLA 与密级匹配到最合适的算力节点。'
POS = [
    ('换序', R, '该任务已完成调度，按密级与SLA匹配到最合适节点。'),
    ('精简', R, '已按SLA和密级把任务匹配到最适合节点，调度完成。'),
    ('百分比', 'GPU 利用率从 40% 提升到 72%。', 'GPU利用率由百分之40提高到72%。'),
    ('内存', '该节点内存为 128GB。', '节点内存128gb。'),
    ('多数决', '任务在三节点间完成多数决校验。', '三个节点对任务做了多数决校验。'),
    ('本地', '模型权重保留在本地，不上传云端。', '模型权重留本地，不向云端上传。'),
]
NEG = [
    ('篡改', R, '【错误结果】该任务无需调度，所有 GPU 均不可用'),
    ('跑题', R, '今天天气不错适合出去玩'),
    ('数字不同', '该节点内存为 128GB。', '该节点内存只有 16GB。'),
    ('部分相关', R, '调度系统需要数据库和前端界面。'),
]


def main():
    # ------------------------------------------------ A 归一化
    check('NFKC 全角字母', J.normalize('ＧＰＵ') == 'gpu')
    check('标点空白删除', J.normalize(' a , b！') == 'ab')
    check('百分比归一', J.normalize('30%') == J.normalize('百分之三十')
          == J.normalize('30％') == '30pct')
    check('内存 GB 大小写', J.normalize('128gb') == J.normalize('128GB'))
    check('内存 TB->GB', J.normalize('0.125tb') == J.normalize('128gb'))
    check('质量 kg/g/千克/公斤',
          J.normalize('1kg') == J.normalize('1000g')
          == J.normalize('1千克') == J.normalize('1公斤'))
    check('长度 km/米', J.normalize('1km') == J.normalize('1000米'))
    check('时间 小时/分钟', J.normalize('1小时') == J.normalize('60分钟'))
    check('时间 min/秒', J.normalize('10min') == J.normalize('600秒'))
    check('歧义保护 500g 是质量非内存',
          J.normalize('500g') == '500g'
          and J.normalize('500g') != '512000mb')
    check('中文数字 三节点', '3节点' in J.normalize('三节点'))
    check('虚词停用', '已' not in J.normalize('已经完成')
          and '的' not in J.normalize('完成的任务'))
    check('同义词 算力节点', '节点' in J.normalize('算力节点'))

    # ------------------------------------------------ B 信号性质
    r_same = J.compare('调度任务已完成', '调度任务已完成')
    check('相同文本 exact/高分',
          r_same['signals']['exact'] == 1.0
          and r_same['equivalent'] >= 0.98)
    check('无关文本低分',
          J.compare('GPU利用率提升', '我喜欢吃苹果')['equivalent'] < 0.2)
    check('lev 相同=1', J.levenshtein_ratio('abc', 'abc') == 1.0)
    check('lev 近似介于', 0.3 < J.levenshtein_ratio('abc', 'abd') < 1.0)
    check('rouge_l 部分匹配', 0 < J.rouge_l('abcde', 'abxde') < 1.0)
    check('数字冲突 number=0',
          J.number_consistency(J.normalize('128gb'),
                               J.normalize('16gb')) == 0.0)
    check('数字一致 number=1',
          J.number_consistency(J.normalize('40%和72%'),
                               J.normalize('72%和40%')) == 1.0)
    check('无数字 number=None',
          J.number_consistency('无数字甲', '无数字乙') is None)

    # ------------------------------------------------ C 标注对准确率
    pos_ok = 0
    for n, a, b in POS:
        p = J.compare(a, b)['equivalent']
        if p >= 0.8:
            pos_ok += 1
        check(f'POS {n} >=0.8', p >= 0.8, str(p))
    neg_ok = 0
    for n, a, b in NEG:
        p = J.compare(a, b)['equivalent']
        if p < 0.5:
            neg_ok += 1
        check(f'NEG {n} <0.5', p < 0.5, str(p))
    print(f'[info] 标注对准确率：POS {pos_ok}/{len(POS)}，'
          f'NEG {neg_ok}/{len(NEG)}')
    # L0 已知局限：数字换位（集合相同、方向相反），L0 会误判
    p_swap = J.compare('GPU 利用率从 40% 提升到 72%',
                       'GPU利用率从72%下降到40%')['equivalent']
    print(f'[info] L0 已知局限-数字换位（需 L1 辨方向）：{p_swap}')

    # ------------------------------------------------ D L1
    # D1 Embedder 解析与缓存
    e = J.Embedder('http://x', 'nomic-embed-text', 1)
    calls = []

    def fp(url, payload):
        calls.append(payload)
        return {'embedding': [1.0, 0.0, 0.0]}
    e._post = fp
    v, v2 = e.embed('hello'), e.embed('hello')
    check('L1 embed 解析', v == [1.0, 0.0, 0.0])
    check('L1 embed 缓存(仅一次请求)', len(calls) == 1 and v2 is v)

    # D2 compare L1 融合
    old_sig = J.embedding_signal
    try:
        J.embedding_signal = lambda a, b: 0.98
        r = J.compare('任务按SLA完成', 'SLA任务完成')
        check('L1 tier=L0+L1',
              r['tier'] == 'L0+L1' and r['signals']['embed'] == 0.98)
        check('L1 evidence 标注', any('L1' in x for x in r['evidence']))

        # D4 L1 补位数字换位：embedding 能区分"提升/下降"方向（余弦低）
        J.embedding_signal = lambda a, b: 0.05
        r_swap = J.compare('GPU 利用率从 40% 提升到 72%',
                           'GPU利用率从72%下降到40%')
        check('L1 纠正数字换位(<0.8 判分歧)',
              r_swap['tier'] == 'L0+L1' and r_swap['equivalent'] < 0.8,
              str(r_swap['equivalent']))
    finally:
        J.embedding_signal = old_sig

    # D3 L1 异常熔断 -> 回退 L0
    class _Dead:
        _dead = False

        def embed(self, t):
            raise RuntimeError('no ollama')

        def mark_dead(self):
            self._dead = True
    J._EMB.update(inst=_Dead(), dead=False)
    check('L1 异常 embedding=None', J.embedding_signal('a', 'b') is None)
    r_fb = J.compare('中文调度任务', '中文调度任务')
    check('L1 失败回退 L0 且不报错',
          r_fb['tier'] == 'L0' and r_fb['equivalent'] >= 0.98)
    J._reset_embedder()

    # D3' 开关关闭
    os.environ['WNIDIA_JEV_EMBED'] = '0'
    J._EMB.update(inst=None, dead=False)
    check('L1 关闭 embedding=None', J.embedding_signal('a', 'b') is None)
    os.environ.pop('WNIDIA_JEV_EMBED', None)
    J._reset_embedder()

    # ------------------------------------------------ E 可解释/置信
    r = J.compare('调度已完成', '调度已完成')
    need = {'equivalent', 'confidence', 'signals', 'tier', 'evidence'}
    check('输出字段完整', need.issubset(set(r)))
    check('evidence 非空列表', isinstance(r['evidence'], list)
          and len(r['evidence']) >= 1)
    check('一致答案置信度高', r['confidence'] >= 0.9, str(r['confidence']))

    failed = [x for x in results if not x[1]]
    print('\n' + '='.ljust(40, '='))
    print(f"通过 {len(results) - len(failed)}/{len(results)}")
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
