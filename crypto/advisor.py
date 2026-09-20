#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自适应决策层：把「回测权重 + 市场状态 + 实盘账本 + 消息面」叠加到原始信号上"""
import os as _os
_STRAT = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "strategy")

import json, os
import news as N

WDIR = _STRAT


def load_weights():
    try:
        with open(os.path.join(WDIR, "weights.json")) as fh:
            return json.load(fh)
    except Exception:
        return {"regime": None, "matrix": {}, "setup": {}}


def load_exits():
    try:
        with open(_os.path.join(_STRAT, "exits.json")) as f:
            return json.load(f)["by_setup"]
    except Exception:
        return {}


def enrich(opps, news_data=None):
    w = load_weights()
    regime = w.get("regime") or "未知"
    nd = news_data or N.snapshot()
    madj, mnotes = N.market_adjust(nd)
    exits = load_exits()
    out = []
    for o in opps:
        # 出场参数优化没找到稳健组合的形态 → 一律不做
        if exits and o["setup"] in exits and not exits[o["setup"]].get("recommended"):
            continue
        setup = o["setup"]
        sb = (w.get("setup") or {}).get(setup) or {}
        cell = (w.get("matrix") or {}).get(f"{setup}|{regime}") or {}
        mult = cell.get("mult", sb.get("mult", 1.0))
        exp = cell.get("exp_final", sb.get("exp_final", 0.0))
        nsamp = cell.get("n", 0)
        if mult <= 0 or cell.get("drop"):   # 该形态在当前行情下期望为负 → 直接剔除
            continue
        sym = o["pair"].split("_")[0]
        ca, cnotes = N.coin_sentiment(sym, nd)
        adj = madj + ca
        o["score_raw"] = o["score"]
        o["score"] = o["score"] + (mult - 1.0) * 25 + adj
        o["regime"] = regime
        o["mult"] = round(mult, 2)
        o["expected_r"] = round(exp, 2)
        o["news_adj"] = round(adj, 1)
        extra = []
        if cell:
            extra.append(f"历史统计：「{setup}」在「{regime}」行情下期望 {exp:+.2f}R（{nsamp} 个样本）")
        extra += cnotes
        # ── 分级 + 仓位自适应：实测期望越高，允许的仓位越大 ──
        if nsamp < 30:
            grade, smul = "C", 0.5          # 样本不足，只能试仓
            extra.append(f"⚠️ 该组合仅 {nsamp} 个历史样本，统计置信度低，只宜试仓")
        elif mult <= 0 or exp <= -0.05:
            continue
        elif exp >= 0.25:
            grade, smul = "A", 1.30
        elif exp >= 0.10:
            grade, smul = "B", 1.00
        elif exp >= 0:
            grade, smul = "C", 0.60
        else:
            grade, smul = "D", 0.00
        o["grade"] = grade
        o["size_mult"] = smul
        o["reasons"] = list(o.get("reasons") or []) + extra
        out.append(o)
    out.sort(key=lambda x: -x["score"])
    best = min([o["grade"] for o in out]) if out else "-"
    if not out or best in ("C", "D"):
        verdict = ("当前行情下没有正期望的形态在发出信号。历史统计显示此时出手是"
                   "负收益来源，建议**观望**；若一定要做，只用半仓以内的试仓。")
    elif best == "B":
        verdict = "出现中等优势机会，可按标准仓位执行。"
    else:
        verdict = "出现高优势机会，可适当放大仓位。"
    return out, {"regime": regime, "market_adj": round(madj, 1),
                 "market_notes": mnotes, "news": nd, "verdict": verdict,
                 "best_grade": best,
                 "weights_ts": w.get("ts"), "dropped": len(opps) - len(out)}


if __name__ == "__main__":
    w = load_weights()
    print("市场状态:", w.get("regime"), "| 权重生成:", w.get("ts"))
    for k, v in (w.get("matrix") or {}).items():
        if v.get("mult") == 0:
            print(f"  🚫 禁用 {k}  期望 {v.get('exp_final', 0):+.2f}R / {v.get('n', 0)} 样本")
