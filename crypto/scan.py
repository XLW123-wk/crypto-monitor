#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全市场扫描 · 多周期信号筛查（数据源：币安 Binance）
输出: 交易机会排行榜 -> /var/minis/shared/crypto/reports/scan_YYYYMMDD_HHMM.json + report.html

用法:
  python3 scan.py                 # 扫描币安全部 USDT 币对
  python3 scan.py --min-vol 3000000
  python3 scan.py --top 10
"""
import os as _os
_STRAT = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "strategy")

import json, os, sys, time, math, urllib.request
from concurrent.futures import ThreadPoolExecutor
import data as D

BASE = "https://api.gateio.ws/api/v4"
OUT = "/tmp/crypto-reports"
CACHE = "/tmp/crypto-klines"

# ---------- 数据层 ----------
def get(path, retry=3, **kw):
    u = BASE + path + ("?" + "&".join(f"{k}={v}" for k, v in kw.items()) if kw else "")
    for a in range(retry):
        try:
            r = urllib.request.Request(u, headers={"User-Agent": "minis-scan/1.0"})
            with urllib.request.urlopen(r, timeout=20) as resp:
                return json.loads(resp.read().decode())
        except Exception:
            if a == retry - 1:
                raise
            time.sleep(0.8 * (a + 1))


def candles(pair, interval, limit=200, ttl=300):
    """多源 K 线（币安优先 → Gate 兜底），已带缓存"""
    return D.klines(pair, interval, limit, ttl=ttl)


# ---------- 指标层 ----------
def sma(a, n):
    if len(a) < n:
        return None
    return sum(a[-n:]) / n


def ema_series(a, n):
    if not a:
        return []
    k = 2 / (n + 1)
    out = [a[0]]
    for x in a[1:]:
        out.append(x * k + out[-1] * (1 - k))
    return out


def rsi(a, n=14):
    if len(a) < n + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, n + 1):
        d = a[i] - a[i - 1]
        gains += max(d, 0); losses += max(-d, 0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(a)):
        d = a[i] - a[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def atr(h, l, c, n=14):
    if len(c) < n + 1:
        return None
    trs = []
    for i in range(1, len(c)):
        trs.append(max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1])))
    a = sum(trs[:n]) / n
    for x in trs[n:]:
        a = (a * (n - 1) + x) / n
    return a


def stdev(a):
    if len(a) < 2:
        return 0
    m = sum(a) / len(a)
    return math.sqrt(sum((x - m) ** 2 for x in a) / len(a))


def boll(a, n=20, k=2):
    if len(a) < n:
        return None, None, None
    m = sum(a[-n:]) / n
    s = stdev(a[-n:])
    return m, m + k * s, m - k * s


def pct_rank(a, x):
    if not a:
        return 50
    return sum(1 for v in a if v <= x) / len(a) * 100


def find_swings(l, h, look=3):
    """返回 (swing_low, swing_high) —— 最近的结构性高低点"""
    n = len(l)
    lows, highs = [], []
    for i in range(look, n - look):
        if l[i] == min(l[i-look:i+look+1]):
            lows.append((i, l[i]))
        if h[i] == max(h[i-look:i+look+1]):
            highs.append((i, h[i]))
    return lows, highs


# ---------- 分析 ----------
EXCLUDE_BASE = {"USDC", "USDT", "DAI", "FDUSD", "TUSD", "USDD", "PYUSD", "USDE",
                "USD1", "BUSD", "GUSD", "USDP", "EURT", "XAUT", "PAXG"}


def analyze(pair, tk, bench=None):
    """对单个币对做多周期分析，返回指标字典或 None"""
    try:
        d4 = candles(pair, "4h", 200)
        d1 = candles(pair, "1h", 168)
        dd = candles(pair, "1d", 120)
    except Exception:
        return None
    if len(d4["c"]) < 60 or len(d1["c"]) < 50 or len(dd["c"]) < 30:
        return None

    price = float(tk["last"])
    c4, h4, l4, v4 = d4["c"], d4["h"], d4["l"], d4["v"]
    c1, h1, l1, v1 = d1["c"], d1["h"], d1["l"], d1["v"]
    cd, hd, ld = dd["c"], dd["h"], dd["l"]

    e20_4, e50_4 = ema_series(c4, 20), ema_series(c4, 50)
    e20_1 = ema_series(c1, 20)
    e20d = ema_series(cd, 20)

    a4 = atr(h4, l4, c4, 14) or price * 0.02
    atr_pct = a4 / price * 100

    # 20日区间（日线）
    win20 = 20
    hi20 = max(hd[-win20:]); lo20 = min(ld[-win20:])
    pos20 = (price - lo20) / (hi20 - lo20) * 100 if hi20 > lo20 else 50

    # 24h 区间
    hi24 = float(tk["high_24h"]); lo24 = float(tk["low_24h"])
    pos24 = (price - lo24) / (hi24 - lo24) * 100 if hi24 > lo24 else 50

    vol_avg = sum(v4[-21:-1]) / 20 if len(v4) > 21 else sum(v4) / len(v4)
    vol_ratio = v4[-1] / vol_avg if vol_avg > 0 else 1

    rsi4, rsi1, rsid = rsi(c4), rsi(c1), rsi(cd)
    m, up_b, lo_b = boll(c4, 20, 2)
    bb_pos = (price - lo_b) / (up_b - lo_b) * 100 if up_b and up_b > lo_b else 50

    # 涨跌幅
    chg24 = float(tk["change_percentage"])
    chg7d = (price / cd[-8] - 1) * 100 if len(cd) >= 8 else 0
    chg30d = (price / cd[-31] - 1) * 100 if len(cd) >= 31 else 0
    chg4h = (price / c4[-2] - 1) * 100 if len(c4) >= 2 else 0
    chg1h = (price / c1[-2] - 1) * 100 if len(c1) >= 2 else 0

    # 结构
    lows4, highs4 = find_swings(l4, h4, 3)
    sup = max([x[1] for x in lows4[-4:]], default=lo20)
    res = min([x[1] for x in highs4[-4:] if x[1] > price * 1.005], default=hi20)

    # 均线关系
    above_e20_4 = price > e20_4[-1]
    above_e50_4 = price > e50_4[-1]
    bull_stack = e20_4[-1] > e50_4[-1]
    ema20_slope = (e20_4[-1] / e20_4[-6] - 1) * 100 if len(e20_4) > 6 else 0

    bench = bench or {"chg7d": 0, "chg30d": 0}
    rs = chg30d - bench["chg30d"]          # 相对 BTC 的 30 日超额收益
    return {
        "pair": pair, "price": price, "vol24": float(tk["quote_volume"]),
        "chg24": chg24, "chg7d": chg7d, "chg30d": chg30d, "chg4h": chg4h, "chg1h": chg1h,
        "hi24": hi24, "lo24": lo24, "pos24": pos24,
        "hi20": hi20, "lo20": lo20, "pos20": pos20,
        "rsi4": rsi4, "rsi1": rsi1, "rsid": rsid,
        "bb_pos": bb_pos, "atr_pct": atr_pct, "atr": a4,
        "vol_ratio": vol_ratio, "sup": sup, "res": res,
        "above_e20_4": above_e20_4, "above_e50_4": above_e50_4,
        "bull_stack": bull_stack, "ema20_slope": ema20_slope,
        "e20_4": e20_4[-1], "e50_4": e50_4[-1],
        "e20_1": e20_1[-1],
        "c4": c4, "c1": c1,
        "rs": rs, "btc7d": bench["chg7d"], "btc30d": bench["chg30d"],
    }


# ---------- 信号引擎 ----------
def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


SETUP_BONUS = {"突破启动": 6, "趋势回踩": 5, "破位下行": 4, "强势动量": 3, "超跌反弹": 2}


def signals(a):
    """返回 ([(setup, direction, reasons)], penalty)  —— 评分在 score_of 里统一计算"""
    out = []
    p = a["price"]
    r4, r1 = a["rsi4"] or 50, a["rsi1"] or 50

    # S1 突破启动：突破20日高 + 放量 + 多头排列
    if a["pos20"] >= 92 and a["bull_stack"] and a["vol_ratio"] >= 1.3 and a["ema20_slope"] > 0.5:
        out.append(("突破启动", "long", [
            f"价格处于20日区间 {a['pos20']:.0f}% 分位，站上/贴近前高 {a['hi20']:,.6g}",
            f"4h 成交量 {a['vol_ratio']:.1f}× 20周期均量，资金主动介入",
            f"EMA20>EMA50 多头排列，EMA20 斜率 +{a['ema20_slope']:.2f}%，突破有效性高",
        ]))

    # S2 趋势回踩：多头排列中缩量回踩均线，RSI 回中性
    near_e20 = abs(p - a["e20_4"]) / p * 100 < 3.0
    near_e50 = abs(p - a["e50_4"]) / p * 100 < 4.0
    healthy = (a["chg24"] > -9) and (p > a["e50_4"]) and (a["pos20"] >= 35)
    if a["bull_stack"] and a["ema20_slope"] > 0.3 and 38 <= r4 <= 58 and (near_e20 or near_e50) and healthy:
        lvl = "EMA20" if near_e20 else "EMA50"
        out.append(("趋势回踩", "long", [
            f"中期多头排列，现价回踩 {lvl}（偏离 {(p-a['e20_4'])/p*100:+.2f}%），结构未破",
            f"4h RSI {r4:.0f} 回落至中性，量比 {a['vol_ratio']:.1f}× 缩量整理，抛压衰竭",
            f"30日 {a['chg30d']:+.1f}%，同期 BTC {a['btc30d']:+.1f}%，相对强度 {a['rs']:+.1f}%",
        ]))

    # S3 超跌反弹：短周期严重超卖 + 未破关键支撑
    if r1 <= 32 and r4 <= 45 and a["pos24"] <= 35 and p > a["lo20"] * 1.01:
        out.append(("超跌反弹", "long", [
            f"1h RSI {r1:.0f} 进入超卖区，4h RSI {r4:.0f} 偏低，短线抛压释放",
            f"24h 位于区间 {a['pos24']:.0f}% 低位，现价 {p:,.6g}",
            f"仍站在 20日低点 {a['lo20']:,.6g} 上方，结构未破，适合轻仓抢反弹",
        ]))

    # S4 强势动量：多周期共振，趋势跟随
    if a["above_e20_4"] and a["bull_stack"] and 52 < r4 < 75 and a["chg7d"] > 5 and a["chg30d"] > 5:
        out.append(("强势动量", "long", [
            f"7日 {a['chg7d']:+.1f}% / 30日 {a['chg30d']:+.1f}%，跑赢 BTC {a['rs']:+.1f}%",
            f"站稳 EMA20/EMA50，4h RSI {r4:.0f} 处于强势但未过热区间",
            f"1h 动能 {a['chg1h']:+.2f}%，短线仍在推进，趋势跟随为主",
        ]))

    # S5 破位下行：跌破20日支撑 + 空头排列
    if a["pos20"] <= 8 and not a["bull_stack"] and r4 < 45 and a["ema20_slope"] < -0.3:
        out.append(("破位下行", "short", [
            f"价格跌至20日区间 {a['pos20']:.0f}% 分位，逼近 {a['lo20']:,.6g} 支撑",
            f"EMA20<EMA50 空头排列，斜率 {a['ema20_slope']:.2f}%",
            f"4h RSI {r4:.0f} 弱势，反弹无力，顺势做空",
        ]))

    pen = []
    if a["atr_pct"] > 10:
        pen.append(f"波动极大 ATR {a['atr_pct']:.1f}%，必须缩仓（建议减半）")
    if a["vol24"] < 2e6:
        pen.append(f"成交额仅 {a['vol24']/1e6:.1f}M，滑点风险偏高")
    if r4 > 78:
        pen.append(f"4h RSI {r4:.0f} 已过热，追高风险大")
    if a["rs"] < -10:
        pen.append(f"跑输 BTC {a['rs']:.1f}%，相对弱势")
    return out, pen


def score_of(a, setup, direction, plan):
    """统一 0-100 标准化评分，跨策略可比"""
    S = 0.0
    long = direction == "long"
    # 1. 趋势一致性 25
    if long:
        S += 11 if a["bull_stack"] else 0
        S += 7 if a["above_e20_4"] else 0
        S += clamp(a["ema20_slope"] / 3.0) * 7
    else:
        S += 11 if not a["bull_stack"] else 0
        S += 7 if not a["above_e20_4"] else 0
        S += clamp(-a["ema20_slope"] / 3.0) * 7
    # 2. 相对强度 20
    rs = a["rs"] if long else -a["rs"]
    S += clamp((rs + 5) / 45.0) * 20
    # 3. 量能结构 15（按策略定义健康量能）
    vr = a["vol_ratio"]
    if setup == "突破启动":
        S += clamp(vr / 1.8) * 15
    elif setup == "趋势回踩":
        S += 15 * (1.0 if 0.25 <= vr <= 1.0 else (0.6 if vr <= 1.6 else 0.3))
    elif setup == "超跌反弹":
        S += clamp(vr / 1.5) * 15
    else:
        S += clamp((vr - 0.6) / 1.2) * 15
    # 4. RSI 位置健康度 15
    r4, r1 = a["rsi4"] or 50, a["rsi1"] or 50
    if setup == "趋势回踩":
        S += clamp(1 - abs(r4 - 48) / 25.0) * 15
    elif setup == "突破启动":
        S += (1.0 if 55 <= r4 <= 75 else 0.35) * 15
    elif setup == "超跌反弹":
        S += clamp((45 - r4) / 20.0) * 8 + clamp((32 - r1) / 15.0) * 7
    elif setup == "强势动量":
        S += clamp(1 - abs(r4 - 65) / 30.0) * 15
    else:
        S += clamp((50 - r4) / 25.0) * 15
    # 5. 盈亏比与波动可控 15
    S += clamp((plan["rr"] - 1.5) / 2.5) * 8
    S += clamp((8 - a["atr_pct"]) / 8.0) * 7
    # 6. 流动性 10
    S += clamp(math.log10(max(a["vol24"], 1) / 1e6) / math.log10(50)) * 10
    return round(S + SETUP_BONUS.get(setup, 0), 1)


def _load_exits():
    try:
        with open(_os.path.join(_STRAT, "exits.json")) as f:
            return json.load(f)["by_setup"]
    except Exception:
        return {}


EXITS = _load_exits()


def trade_plan(a, direction, setup):
    p = a["price"]
    at = a["atr"]
    # 出场参数来自 optimize.py 的数据驱动优化（按形态分别取最优）
    ex = (EXITS.get(setup) or {}).get("recommended") or {}
    stop_atr = ex.get("stop_atr", 1.2)
    target_r = ex.get("target_r", 1.5)
    cap = ex.get("stop_cap_pct", 8.0)        # 止损距离上限（数据驱动的最优值）
    floor = ex.get("min_stop_pct", 2.5)      # 下限，太紧会被日常波动扫掉
    if direction == "long":
        entry_lo = max(a["e20_4"], p - 0.6 * at)
        entry_hi = p + 0.25 * at
        stop = entry_lo - stop_atr * at
        # 把止损距离夹在 [下限, 上限] 之间，避免出现 -17% 这种过宽的止损
        stop = max(stop, entry_lo * (1 - cap / 100))
        stop = min(stop, entry_lo * (1 - floor / 100))
        risk = entry_lo - stop
        if risk <= 0:
            risk = stop_atr * at
            stop = entry_lo - risk
        t1 = entry_lo + target_r * 0.6 * risk     # 第一目标：减半仓
        t2 = max(entry_lo + target_r * risk, a["hi20"] * 1.02)   # 第二目标：清仓
    else:
        entry_hi = min(a["e20_4"], p + 0.6 * at)
        entry_lo = p - 0.25 * at
        stop = entry_hi + stop_atr * at
        stop = min(stop, entry_hi * (1 + cap / 100))
        stop = max(stop, entry_hi * (1 + floor / 100))
        risk = stop - entry_hi
        if risk <= 0:
            risk = stop_atr * at
            stop = entry_hi + risk
        t1 = entry_hi - target_r * 0.6 * risk
        t2 = min(entry_hi - target_r * risk, a["lo20"] * 0.98)
    entry_mid = (entry_lo + entry_hi) / 2
    basis = entry_lo if direction == "long" else entry_hi   # 赔率按实际入场基准算
    rr = abs(t2 - basis) / abs(basis - stop) if abs(basis - stop) > 0 else 0
    return {
        "entry_lo": entry_lo, "entry_hi": entry_hi, "entry": entry_mid,
        "stop": stop, "t1": t1, "t2": t2,
        "risk_pct": abs(entry_mid - stop) / entry_mid * 100, "rr": rr,
    }


def fmt(p):
    p = float(p)
    if p >= 1000: return f"{p:,.2f}"
    if p >= 1: return f"{p:,.4f}"
    if p >= 0.01: return f"{p:.6f}"
    return f"{p:.8f}"


def main():
    args = sys.argv[1:]
    minvol = 1e5
    topn = 12
    if "--min-vol" in args:
        minvol = float(args[args.index("--min-vol") + 1])
    if "--top" in args:
        topn = int(args[args.index("--top") + 1])

    t0 = time.time()
    print("▶ 拉取全市场行情…")
    tick = D.tickers()
    cand = []
    for cp, t in tick.items():
        t = dict(t)
        t["currency_pair"] = cp
        cp = t["currency_pair"]
        if not cp.endswith("_USDT"):
            continue
        base = cp[:-5]
        if base in EXCLUDE_BASE or base.endswith("3L") or base.endswith("3S") or base.endswith("5L") or base.endswith("5S"):
            continue
        if float(t.get("quote_volume") or 0) < minvol:
            continue
        cand.append(t)
    cand.sort(key=lambda x: -float(x["quote_volume"]))
    print(f"▶ 候选币对 {len(cand)} 个（成交额 > {minvol/1e6:.2f}M），开始多周期分析…")

    try:
        bench = D.btc_benchmark()
        print(f"▶ 基准 BTC: 7日 {bench['chg7d']:+.1f}% / 30日 {bench['chg30d']:+.1f}%")
    except Exception as e:
        bench = {"chg7d": 0, "chg30d": 0}
        print("基准获取失败", e)

    results, fails = [], 0
    def work(t):
        return analyze(t["currency_pair"], t, bench)
    with ThreadPoolExecutor(max_workers=12) as ex:
        for i, r in enumerate(ex.map(work, cand)):
            if r:
                results.append(r)
            else:
                fails += 1
            if (i + 1) % 25 == 0:
                print(f"   ...{i+1}/{len(cand)}")
    print(f"▶ 完成 {len(results)} 个（失败 {fails}），耗时 {time.time()-t0:.0f}s")

    opps = []
    for a in results:
        sigs, pen = signals(a)
        for name, direction, reasons in sigs:
            plan = trade_plan(a, direction, name)
            if plan["rr"] < 1.5 or plan["risk_pct"] > 18:
                continue
            score = score_of(a, name, direction, plan)
            opps.append({
                "pair": a["pair"], "setup": name, "direction": direction,
                "score": score, "price": a["price"], "plan": plan,
                "reasons": reasons, "penalty": pen, "ind": {
                    "rsi4": a["rsi4"], "rsi1": a["rsi1"], "atr_pct": a["atr_pct"],
                    "vol_ratio": a["vol_ratio"], "pos20": a["pos20"], "pos24": a["pos24"],
                    "chg24": a["chg24"], "chg7d": a["chg7d"], "chg30d": a["chg30d"],
                    "vol24": a["vol24"], "bb_pos": a["bb_pos"],
                },
            })
    opps.sort(key=lambda x: -x["score"])

    # 自适应决策层：叠加回测权重 + 市场状态 + 消息面（必须在存盘前执行）
    import advisor
    opps, meta = advisor.enrich(opps)

    # 决策层若判定「无正期望信号」→ 生成待触发观察名单（上升趋势中贴近 20 周期高点者）
    watch = []
    if not opps:
        for a in results:
            base = a.get("pair", "").split("_")[0]
            if not a.get("bull_stack") or a.get("vol24", 0) < 1e7:
                continue
            if base in ("U", "USDC", "FDUSD", "TUSD", "DAI", "USD1", "USDP", "BUSD", "AEUR"):
                continue
            if not (22 <= (a.get("rsi4") or 0) <= 85):    # 剔除超买过热，避免追高
                continue
            if a.get("chg24", 0) > 35 or a.get("chg24", 0) < -12:   # 剔除抛物线拉升/崩塌
                continue
            p, hi = a.get("price") or 0, a.get("hi20") or 0
            if p <= 0 or hi <= 0:
                continue
            dist = (hi * 1.005 / p - 1) * 100
            if dist < 0 or dist > 8:
                continue
            watch.append({"pair": a["pair"], "price": p, "trigger": hi * 1.005,
                          "dist_pct": dist, "vol24": a["vol24"], "rsi4": a["rsi4"],
                          "atr_pct": a["atr_pct"], "chg24": a["chg24"],
                          "rs": a.get("rs", 0), "sup": a.get("sup")})
        watch.sort(key=lambda x: x["dist_pct"])
        watch = watch[:8]
    meta["watchlist"] = watch

    os.makedirs(OUT, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M")
    path = f"{OUT}/scan_{stamp}.json"
    with open(path, "w") as f:
        json.dump({"ts": stamp, "universe": len(results), "meta": meta,
                   "opportunities": opps, "watchlist": watch, "ok": True},
                  f, ensure_ascii=False)

    # 控制台输出
    print(f"\n🌐 市场状态「{meta['regime']}」 · 消息面情绪 {meta['news'].get('sentiment')}/100 "
          f"· 决策层剔除 {meta['dropped']} 个逆势信号")
    for n in meta["market_notes"]:
        print(f"   · {n}")
    print(f"\n{'='*80}\n🎯 可交易信号 TOP{min(topn,len(opps))}   （共 {len(opps)} 个信号 / {len(results)} 个币对）\n{'='*80}")
    for i, o in enumerate(opps[:topn], 1):
        pl, ind = o["plan"], o["ind"]
        d = "做多 🟢" if o["direction"] == "long" else "做空 🔴"
        print(f"\n{i}. {o['pair']:12} {d}  【{o['setup']}】评分 {o['score']}")
        print(f"   现价 {fmt(o['price'])}   24h {ind['chg24']:+.2f}%   7d {ind['chg7d']:+.1f}%")
        print(f"   入场 {fmt(pl['entry_lo'])} ~ {fmt(pl['entry_hi'])}   止损 {fmt(pl['stop'])} ({-pl['risk_pct']:.1f}%)")
        print(f"   目标1 {fmt(pl['t1'])} (1.5R)   目标2 {fmt(pl['t2'])} ({pl['rr']:.1f}R)")
        print(f"   指标 RSI4h={ind['rsi4']:.0f} RSI1h={ind['rsi1']:.0f} ATR={ind['atr_pct']:.1f}% "
              f"量比={ind['vol_ratio']:.1f}x 20日分位={ind['pos20']:.0f}%")
        for r in o["reasons"]:
            print(f"     · {r}")
        for r in o["penalty"]:
            print(f"     ⚠ {r}")
    if len(opps) < topn:
        print("\n（今日符合条件的高质量信号较少，宁缺毋滥）")
    print(f"\n📁 报告: {path}")
    print("⚠️ 以上为量化信号筛查结果，非投资建议。加密货币波动极大，务必自行判断并严格止损。")


if __name__ == "__main__":
    main()
