# -*- coding: utf-8 -*-
"""进程内开源 Jev 决策模型适配（best-effort，local 后端 / 自建服务共用）。

把 (state, typed-questions) 编码成文本送入 ModernBERT 序列分类头，输出校准概率：
  noul   -> sigmoid(正类 logit) 得到“命题为真”的概率；
  choice -> 逐选项独立打分后 softmax 归一化为概率分布。

默认模型 heman10x/openJev-verdict-2.0（151M ModernBERT，非自回归，RLCD 校准）。

定位说明：
  - 生产首选 http 后端（Laya / vLLM 的 /v1/systemone，或 scripts/serve_jev.py 自建）；
  - 本模块是进程内便利路径，首次调用懒加载 torch/transformers；
  - 缺依赖 / 权重缺失 / 加载失败会抛异常，由 jevclient._dispatch 兜底为 None（不劣化）；
  - 若目标仓库暴露了 Jev 风格的 predict/decide 接口，应优先调用该接口，本文件仅提供
    “ModernBERT 序列分类”这一通用编码路径，具体 head 布局以模型卡为准（真机校验）。
"""
import json
import math

_MODEL = {}   # model_id -> _Encoder 单例


def _shape_conf(vals):
    """由概率分布形状推导置信度（0..1）：越集中越高。"""
    vals = [v for v in vals if v is not None]
    n = len(vals)
    if n == 0:
        return 0.0
    if n == 1:
        return round(abs(2.0 * vals[0] - 1.0), 3)
    if n == 2:
        return round(abs(2.0 * max(vals) - 1.0), 3)
    top = max(vals)
    ent = -sum(p * math.log(p + 1e-9) for p in vals) / math.log(n)
    return round(min(1.0, max(0.0, 0.5 * top + 0.5 * (1 - ent))), 3)


class _Encoder:
    """封装一个本地 ModernBERT 序列分类决策模型。"""

    def __init__(self, model_id):
        import torch
        from transformers import (AutoTokenizer,
                                  AutoModelForSequenceClassification)
        self.torch = torch
        self.tok = AutoTokenizer.from_pretrained(model_id)
        self.model = AutoModelForSequenceClassification.from_pretrained(model_id)
        self.model.eval()
        self.device = 'cuda' if torch.cuda.is_available() else 'cpu'
        self.model.to(self.device)

    def _text(self, instructions, state, option=None):
        parts = [instructions or '', json.dumps(state, ensure_ascii=False)]
        if option is not None:
            parts.insert(1, option)
        return ' [SEP] '.join(parts)

    def _p_yes(self, text):
        """返回“命题为真”的校准概率。"""
        enc = self.tok(text, return_tensors='pt', truncation=True,
                       max_length=512)
        enc = {k: v.to(self.device) for k, v in enc.items()}
        with self.torch.no_grad():
            logits = self.model(**enc).logits
        arr = logits.detach().cpu().numpy()
        if arr.ndim == 2:
            pos = float(arr[0, 1]) if arr.shape[1] >= 2 else float(arr[0].max())
        else:
            pos = float(arr[0])
        return 1.0 / (1.0 + math.exp(-pos))

    def noul(self, state, instructions):
        p = self._p_yes(self._text(instructions, state))
        return {'noul': round(p, 3), 'confidence': round(abs(2 * p - 1), 3)}

    def choice(self, state, instructions, options):
        raw = {o: self._p_yes(self._text(instructions, state, o))
               for o in options}
        total = sum(raw.values()) or 1e-9
        probs = {o: v / total for o, v in raw.items()}
        return {'choice': max(probs, key=probs.get),
                'probabilities': {o: round(v, 3) for o, v in probs.items()},
                'confidence': _shape_conf(list(probs.values()))}


def _get(model_id):
    if model_id not in _MODEL:
        _MODEL[model_id] = _Encoder(model_id)
    return _MODEL[model_id]


def decide(model_id, state, questions):
    """入口：返回与 HTTP 后端 /v1/systemone 相同形状的 answers 字典。"""
    m = _get(model_id)
    answers = {}
    for name, q in questions.items():
        qtype = (q or {}).get('type')
        if qtype == 'noul':
            answers[name] = m.noul(state, q.get('instructions', ''))
        elif qtype == 'choice':
            answers[name] = m.choice(state, q.get('instructions', ''),
                                     q.get('options', []))
        else:
            answers[name] = {'noul': 0.5, 'confidence': 0.0}
    return answers
