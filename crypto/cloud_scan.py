#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cloud_scan.py — 云端扫描入口（GitHub Actions 定时运行）

流程：
  1. 拉取币安全市场行情 → 多周期技术分析
  2. 信号筛查 + 自适应决策层（回测权重 × 市场状态 × 消息面）
  3. 出现正期望机会 / 观察名单突破 → 推送到手机（Bark）
  4. 去重状态写入 state/state.json，由 workflow 提交回仓库

环境变量：
  BARK_KEY          必填，Bark 推送密钥
  BARK_SERVER       可选，自建 Bark 服务器地址（默认 https://api.day.app）
  MIN_VOL           可选，最小成交额（默认 2000000）
  BINANCE_API_KEY   可选，填了才监控持仓
  BINANCE_API_SECRET
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import data as D          # noqa: E402
import scan as S          # noqa: E402

STATE_DIR = os.path.join(HERE, "..", "state")
STATE = os.path.join(STATE_DIR, "state.json")

DEDUP_SEC = 3600 * 3      # 同一信号 3 小时内不重复推
LOSS_ALERT = 0.03         # 浮亏超过权益 3% 报警
LIQ_ALERT = 0.20          # 距强平价 <20% 报警


# ---------------- 推送 ----------------
def bark(title, body, level="active"):
    key = os.environ.get("BARK_KEY", "").strip()
    if not key:
        print("[warn] 未设置 BARK_KEY，跳过推送")
        return
    server = os.environ.get("BARK_SERVER", "https://api.day.app").rstrip("/")
    url = (f"{server}/{key}/{urllib.parse.quote(title)}/"
           f"{urllib.parse.quote(body)}?group=crypto&level={level}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "crypto-cloud/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            print(f"[push] {r.status} {r.read()[:120].decode('utf-8', 'replace')}")
    except Exception as e:
        print(f"[push] 失败: {str(e)[:150]}")


# ---------------- 去重状态 ----------------
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
    bark(title, body, level)
    return True


# ---------------- 持仓监控（可选） ----------------
def check_positions(st):
    import hashlib
    import hmac

    key = os.environ.get("BINANCE_API_KEY", "").strip()
    sec = os.environ.get("BINANCE_API_SECRET", "").strip()
    if not key or not sec:
        return

    def priv(path, params=None):
        p = dict(params or {})
        p["timestamp"] = int(time.time() * 1000)
        p.setdefault("recvWindow", 5000)
        qs = urllib.parse.urlencode(sorted(p.items()))
        sig = hmac.new(sec.encode(), qs.encode(), hashlib.sha256).hexdigest()
        url = f"https://fapi.binance.com{path}?{qs}&signature={sig}"
        r = urllib.request.Request(url, headers={"X-MBX-APIKEY": key})
        with urllib.request.urlopen(r, timeout=20) as resp:
            return json.loads(resp.read())

    try:
        pos = priv("/fapi/v2/positionRisk")
        bal = sum(float(b.get("balance", 0)) for b in priv("/fapi/v2/balance")
                  if b.get("asset") in ("USDT", "U"))
    except Exception as e:
        print(f"[pos] 读取失败: {str(e)[:120]}")
        return

    for p in pos:
        amt = float(p.get("positionAmt", 0))
        if amt == 0:
            continue
        sym, mark = p["symbol"], float(p["markPrice"])
        upnl = float(p["unRealizedProfit"])
        lev = p.get("leverage", "?")
        liq = float(p.get("liquidationPrice") or 0)
        d = "多" if amt > 0 else "空"
        if bal > 0 and upnl < 0 and abs(upnl) > bal * LOSS_ALERT:
            alert_once(st, f"loss:{sym}", f"⚠️ {sym} 浮亏扩大",
                       f"{d} {abs(amt):g} 个 · 浮亏 {upnl:+.2f}U（权益 {abs(upnl)/bal*100:.0f}%）"
                       f" · 现价 {mark:.6g} · {lev}x", "timeSensitive")
        if liq > 0 and abs(liq - mark) / mark < LIQ_ALERT:
            alert_once(st, f"liq:{sym}", f"🚨 {sym} 接近强平",
                       f"{d}单强平价 {liq:.6g} · 距现价仅 {abs(liq-mark)/mark*100:.1f}%"
                       f" · {lev}x · 立刻处理", "timeSensitive")


# ---------------- 主扫描 ----------------
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

    # 决策层（消息面拿不到时降级，不中断）
    import advisor
    try:
        opps, meta = advisor.enrich(opps)
    except Exception as e:
        print(f"[warn] 决策层失败({str(e)[:80]})，改用原始信号")
        opps.sort(key=lambda x: -x["score"])
        opps = opps[:5]
        meta = {"regime": "未知", "dropped": 0, "market_notes": [], "news": {}}

    regime = meta.get("regime", "?")
    print(f"🌐 {regime} · 剔除 {meta.get('dropped')} 个 · 机会 {len(opps)} 个")

    # 推送机会
    for o in opps[:3]:
        pl = o["plan"]
        sym = o["pair"].replace("_", "")
        d = "做多" if o["direction"] == "long" else "做空"
        alert_once(
            st, f"sig:{o['pair']}:{o['setup']}",
            f"🎯 {sym} {d}【{o['setup']}】评分 {o['score']}",
            f"现价 {S.fmt(o['price'])} · 挂 {S.fmt(pl['entry_lo'])}~{S.fmt(pl['entry_hi'])}"
            f" · 止损 {S.fmt(pl['stop'])} · 目标 {S.fmt(pl['t1'])}/{S.fmt(pl['t2'])}"
            f" · {regime} · 回 Minis 看完整单",
        )

    # 无机会时 → 观察名单突破检测
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
        # 只对最接近的 3 个做实时突破检测
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
                               f"现价 {S.fmt(cur)} 越过突破位 {S.fmt(w['trigger'])}"
                               f" · RSI {w['rsi4']:.0f} · 回 Minis 看能否出手")
        st["last_watch"] = watch

    st["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
    st["last_regime"] = regime
    st["last_opps"] = len(opps)
    return opps, meta, results


def main():
    print(f"=== 云端扫描 {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
    st = load_state()
    try:
        run_scan(st)
    except Exception as e:
        import traceback
        traceback.print_exc()
        bark("❌ 云端扫描出错", str(e)[:200], "timeSensitive")
    check_positions(st)
    save_state(st)
    print("=== 完成 ===")


if __name__ == "__main__":
    main()
