#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cloud_scan.py — 云端扫描入口（GitHub Actions 定时运行）

流程：
  1. 拉取币安全市场行情 → 多周期技术分析
  2. 信号筛查 + 自适应决策层（回测权重 × 市场状态 × 消息面）
  3. 出现正期望机会 / 观察名单突破 → 推送（微信 PushPlus + Bark）
  4. 去重状态写入 state/state.json，由 workflow 提交回仓库

环境变量：
  PUSHPLUS_TOKEN    → 微信推送（可选）
  BARK_KEY          → iOS 推送（可选）
  WECOM_WEBHOOK     → 企业微信机器人（可选）
  MIN_VOL           最小成交额（默认 2000000）
  两者至少配一个，否则不会推送。
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import data as D          # noqa: E402
import scan as S          # noqa: E402
import notify             # noqa: E402

STATE_DIR = os.path.join(HERE, "..", "state")
STATE = os.path.join(STATE_DIR, "state.json")

DEDUP_SEC = 3600 * 3      # 同一信号 3 小时内不重复推


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            s = json.load(f)
        s.setdefault("alerts", {})
        return s
    except Exception:
        return {"alerts": {}}


def save_state(st):
    os.makedirs(STATE_DIR, exist_ok=True)
    now = time.time()
    st["alerts"] = {k: v for k, v in st.get("alerts", {}).items() if now - v < 86400}
    st["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def alert_once(st, key, title, body, level="active"):
    if time.time() - st["alerts"].get(key, 0) < DEDUP_SEC:
        print(f"[skip] 已推过 {key}")
        return False
    st["alerts"][key] = time.time()
    notify.push(title, body, level=level)
    return True


def run_scan(st):
    minvol = float(os.environ.get("MIN_VOL", 2000000))
    t0 = time.time()
    print("▶ 拉取全市场行情…")
    tick = D.tickers(ttl=0)
    cand = []
    for cp, t in tick.items():
        t = dict(t)
        t["currency_pair"] = cp          # scan.analyze 依赖这个字段
        if not cp.endswith("_USDT"):
            continue
        base = cp[:-5]
        if base in S.EXCLUDE_BASE or base.endswith(("3L", "3S", "5L", "5S")):
            continue
        if float(t.get("quote_volume") or 0) < minvol:
            continue
        cand.append(t)
    cand.sort(key=lambda x: -float(x["quote_volume"]))
    print(f"▶ 候选 {len(cand)} 个，开始多周期分析…")

    try:
        bench = D.btc_benchmark(ttl=0)
    except Exception:
        bench = {"chg7d": 0, "chg30d": 0}

    results, fails = [], 0

    def work(t):
        return S.analyze(t["currency_pair"], t, bench)

    with ThreadPoolExecutor(max_workers=12) as ex:
        for r in ex.map(work, cand):
            if r:
                results.append(r)
            else:
                fails += 1
    print(f"▶ 完成 {len(results)} 个（失败 {fails}），耗时 {time.time()-t0:.0f}s")

    opps = []
    for a in results:
        sigs, pen = S.signals(a)
        for name, direction, reasons in sigs:
            plan = S.trade_plan(a, direction, name)
            if plan["rr"] < 1.5 or plan["risk_pct"] > 18:
                continue
            opps.append({
                "pair": a["pair"], "setup": name, "direction": direction,
                "score": S.score_of(a, name, direction, plan), "price": a["price"],
                "plan": plan, "reasons": reasons, "penalty": pen,
                "ind": {"chg24": a["chg24"], "chg7d": a["chg7d"], "rsi4": a["rsi4"],
                        "vol_ratio": a["vol_ratio"], "pos20": a["pos20"]},
            })
    opps.sort(key=lambda x: -x["score"])

    import advisor
    try:
        opps, meta = advisor.enrich(opps)
    except Exception as e:
        print(f"[warn] 决策层失败({str(e)[:80]})，改用原始信号")
        opps = opps[:5]
        meta = {"regime": "未知", "dropped": 0, "market_notes": [], "news": {}}

    regime = meta.get("regime", "?")
    print(f"🌐 {regime} · 剔除 {meta.get('dropped')} 个 · 机会 {len(opps)} 个")

    for o in opps[:3]:
        pl = o["plan"]
        sym = o["pair"].replace("_", "")
        d = "做多" if o["direction"] == "long" else "做空"
        alert_once(
            st, f"sig:{o['pair']}:{o['setup']}",
            f"🎯 {sym} {d}【{o['setup']}】评分 {o['score']}",
            f"**现价** {S.fmt(o['price'])}\n"
            f"**挂单** {S.fmt(pl['entry_lo'])} ~ {S.fmt(pl['entry_hi'])}\n"
            f"**止损** {S.fmt(pl['stop'])}（-{pl['risk_pct']:.1f}%）\n"
            f"**目标①** {S.fmt(pl['t1'])}  → 卖一半\n"
            f"**目标②** {S.fmt(pl['t2'])}  → 清仓\n"
            f"R:R {pl['rr']:.1f} · {regime}\n\n"
            f"回 Minis 问「看单子」拿仓位建议",
        )

    if not opps:
        watch = []
        for a in results:
            base = a.get("pair", "").split("_")[0]
            if not a.get("bull_stack") or a.get("vol24", 0) < 1e7:
                continue
            if base in ("U", "USDC", "FDUSD", "TUSD", "DAI", "USD1", "USDP", "BUSD"):
                continue
            if not (22 <= (a.get("rsi4") or 0) <= 85):
                continue
            if a.get("chg24", 0) > 35 or a.get("chg24", 0) < -12:
                continue
            p, hi = a.get("price") or 0, a.get("hi20") or 0
            if p <= 0 or hi <= 0:
                continue
            dist = (hi * 1.005 / p - 1) * 100
            if 0 <= dist <= 8:
                watch.append({"pair": a["pair"], "price": p, "trigger": hi * 1.005,
                              "dist_pct": dist, "rsi4": a["rsi4"], "chg24": a["chg24"]})
        watch.sort(key=lambda x: x["dist_pct"])
        watch = watch[:8]
        print(f"👀 观察名单 {len(watch)} 个: " +
              ", ".join(f"{w['pair']}({w['dist_pct']:.1f}%)" for w in watch[:5]))
        if watch:
            try:
                px = D.prices([w["pair"] for w in watch])
            except Exception:
                px = {}
            for w in watch[:3]:
                cur = px.get(w["pair"])
                if cur and cur >= w["trigger"]:
                    alert_once(st, f"brk:{w['pair']}",
                               f"🚀 {w['pair'].replace('_','')} 突破",
                               f"**现价** {S.fmt(cur)} 越过突破位 {S.fmt(w['trigger'])}\n"
                               f"RSI {w['rsi4']:.0f} · 回 Minis 确认能否出手")
        st["last_watch"] = watch

    st["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
    st["last_regime"] = regime
    st["last_opps"] = len(opps)
    return opps, meta, results


def main():
    print(f"=== 云端扫描 {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
    print(f"推送渠道: {notify.configured() or '（无！）'}")
    st = load_state()
    try:
        run_scan(st)
    except Exception as e:
        import traceback
        traceback.print_exc()
        notify.push("❌ 云端扫描出错", str(e)[:300], level="timeSensitive")
    save_state(st)
    print("=== 完成 ===")


if __name__ == "__main__":
    main()
