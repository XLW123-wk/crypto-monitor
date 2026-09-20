#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""消息面 / 情绪面数据层
覆盖：恐惧贪婪指数、资金费率、多空持仓比、交易所热搜、新闻标题情绪、全局市值
输出：/var/minis/shared/crypto/news/latest.json
"""
import os as _os
_STRAT = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "strategy")

import json, os, re, time, xml.etree.ElementTree as ET
import data as D

OUT_DIR = "/tmp/crypto-news"

# ── 情绪词典（英文新闻为主，兼收中文）──────────────────────────
BULL = ["surge", "soar", "rally", "jump", "spike", "record", "high", "bullish", "bull",
        "approval", "approved", "etf", "inflow", "adoption", "partnership", "upgrade",
        "breakout", "gain", "gains", "rebound", "recover", "buyback", "burn", "halving",
        "accumulate", "institutional", "mainstream", "support", "optimistic", "boost",
        "launch", "integration", "listing", "上", "涨", "利好", "突破", "合作", "获批"]
BEAR = ["crash", "plunge", "dump", "slump", "tumble", "fall", "drop", "selloff", "sell-off",
        "bearish", "bear", "hack", "hacked", "exploit", "stolen", "lawsuit", "sue", "sued",
        "sec charges", "ban", "banned", "crackdown", "liquidation", "liquidated", "outflow",
        "delist", "fraud", "scam", "bankruptcy", "investigation", "warning", "risk", "fear",
        "capitulation", "下调", "跌", "崩", "利空", "黑客", "诉讼", "禁止", "清算"]

COIN_MAP = {
    "BTC": ["btc", "bitcoin"], "ETH": ["eth", "ethereum", "ether"],
    "SOL": ["sol", "solana"], "XRP": ["xrp", "ripple"], "BNB": ["bnb", "binance coin"],
    "DOGE": ["doge", "dogecoin"], "ADA": ["ada", "cardano"], "AVAX": ["avax", "avalanche"],
    "LINK": ["link", "chainlink"], "DOT": ["dot", "polkadot"], "TRX": ["trx", "tron"],
    "LTC": ["ltc", "litecoin"], "TON": ["toncoin", "ton "], "SHIB": ["shib", "shiba"],
    "PEPE": ["pepe"], "UNI": ["uni ", "uniswap"], "ATOM": ["atom", "cosmos"],
    "NEAR": ["near protocol", "near "], "APT": ["aptos", "apt "], "ARB": ["arbitrum"],
    "OP": ["optimism"], "SUI": ["sui "], "ZEC": ["zcash", "zec"], "XLM": ["stellar", "xlm"],
    "ALGO": ["algorand", "algo "], "VET": ["vechain", "vet "], "FIL": ["filecoin"],
    "TRUMP": ["trump coin", "$trump"], "HYPE": ["hyperliquid", "hype"],
    "AAVE": ["aave"], "ETC": ["ethereum classic", "etc "], "BCH": ["bitcoin cash", "bch"],
    "HBAR": ["hedera", "hbar"], "ICP": ["internet computer", "icp "], "ZEN": ["horizen"],
    "ASTER": ["aster"], "ARK": ["ark "], "SOMI": ["somnia"], "REEF": ["reef"],
}


def _get(url, timeout=15, retry=2):
    """JSON 接口"""
    return D.get_json(url, timeout=timeout, retry=retry)


def _text(url, timeout=15, retry=2):
    """RSS / XML 等纯文本"""
    return D.fetch_raw(url, timeout=timeout, retry=retry)


def fear_greed():
    """恐惧贪婪指数：0 极度恐惧 ~ 100 极度贪婪"""
    try:
        d = _get("https://api.alternative.me/fng/?limit=8")
        arr = d.get("data", [])
        return {"now": int(arr[0]["value"]), "label": arr[0]["value_classification"],
                "prev7": [int(x["value"]) for x in arr[1:]], "ts": int(arr[0]["timestamp"])}
    except Exception as e:
        return {"now": None, "err": str(e)}


def global_stat():
    """全局市值、BTC 占比"""
    try:
        d = _get("https://api.coingecko.com/api/v3/global")["data"]
        return {"mcap_usd": d["total_market_cap"]["usd"],
                "mcap_chg24": d["market_cap_change_percentage_24h_usd"],
                "btc_dom": d["market_cap_percentage"]["btc"],
                "eth_dom": d["market_cap_percentage"]["eth"]}
    except Exception as e:
        return {"err": str(e)}


def funding_and_ratio(coins=("BTC", "ETH", "SOL", "XRP", "DOGE")):
    """资金费率 + 多空持仓比（OKX 公开接口）
    资金费率持续为正 = 多头拥挤（潜在回落风险）；为负 = 空头拥挤（易被轧空）
    """
    from concurrent.futures import ThreadPoolExecutor
    out = {}

    def _one(c):
        rec = {}
        try:
            d = _get(f"https://www.okx.com/api/v5/public/funding-rate?instId={c}-USDT-SWAP")["data"][0]
            rec["funding"] = float(d["fundingRate"])
            rec["next_funding"] = float(d.get("nextFundingRate") or 0)
        except Exception:
            pass
        try:
            d = _get(f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?ccy={c}&period=1H")
            arr = d["data"]
            rec["ls_ratio"] = float(arr[0][1])
            rec["ls_ratio_24h_ago"] = float(arr[23][1]) if len(arr) > 23 else None
        except Exception:
            pass
        return c, rec

    with ThreadPoolExecutor(max_workers=5) as ex:
        for c, rec in ex.map(_one, coins):
            if rec:
                out[c] = rec
    return out


def trending():
    """交易所热搜 —— 散户注意力集中度"""
    try:
        d = _get("https://api.coingecko.com/api/v3/search/trending")
        return [x["item"]["symbol"].upper() for x in d.get("coins", [])][:15]
    except Exception:
        return []


FEEDS = ["https://cointelegraph.com/rss", "https://decrypt.co/feed",
         "https://www.coindesk.com/arc/outboundfeeds/rss/", "https://bitcoinmagazine.com/feed"]


def headlines(limit=60):
    """抓取新闻标题 + 情绪打分"""
    from concurrent.futures import ThreadPoolExecutor

    def _feed(url):
        got = []
        try:
            raw = _text(url, timeout=15, retry=1)
            if isinstance(raw, (dict, list)):
                return got
            txt = raw if isinstance(raw, str) else str(raw)
            root = ET.fromstring(txt)
            for it in root.iter("item"):
                t = (it.findtext("title") or "").strip()
                desc = (it.findtext("description") or "")[:200]
                if not t:
                    continue
                low = (t + " " + desc).lower()
                b = sum(1 for w in BULL if w in low)
                s = sum(1 for w in BEAR if w in low)
                got.append({"title": t, "score": b - s, "bull": b, "bear": s,
                            "src": url.split("/")[2]})
        except Exception:
            pass
        return got

    items = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        for g in ex.map(_feed, FEEDS):
            items.extend(g)
    # 去重
    seen, uniq = set(), []
    for it in items:
        k = it["title"][:60].lower()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(it)
    return uniq[:limit]


def coin_mentions(items):
    """把新闻标题映射到具体币种，统计提及次数与情绪"""
    agg = {}
    for it in items:
        low = it["title"].lower()
        for sym, keys in COIN_MAP.items():
            if any(k in low for k in keys):
                a = agg.setdefault(sym, {"n": 0, "score": 0, "titles": []})
                a["n"] += 1
                a["score"] += it["score"]
                if len(a["titles"]) < 2:
                    a["titles"].append(it["title"][:95])
    return agg


def sentiment_score(fg, hl, fund, trend_syms, gstat):
    """市场情绪综合分 0-100（<30 恐慌，>70 贪婪）"""
    parts, weights = [], []
    if fg.get("now") is not None:
        parts.append(fg["now"]); weights.append(0.30)
    if hl:
        pos = sum(1 for h in hl if h["score"] > 0)
        neg = sum(1 for h in hl if h["score"] < 0)
        tot = pos + neg
        if tot:
            parts.append(pos / tot * 100); weights.append(0.25)
    fr = [fund[c]["funding"] for c in fund if "funding" in fund.get(c, {})]
    if fr:
        avg = sum(fr) / len(fr)
        # 资金费率 0.01%/8h 为常态中性；年化后映射到 0-100
        parts.append(max(0, min(100, 50 + avg * 100 * 12))); weights.append(0.20)
    if gstat.get("mcap_chg24") is not None:
        parts.append(max(0, min(100, 50 + gstat["mcap_chg24"] * 8))); weights.append(0.15)
    if trend_syms:
        parts.append(min(100, 40 + len(trend_syms) * 2)); weights.append(0.10)
    if not parts:
        return None
    return round(sum(p * w for p, w in zip(parts, weights)) / sum(weights), 1)


def collect():
    """并发抓取 5 路消息面数据（串行需 45s，并发约 8s）"""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=5) as ex:
        f_fg = ex.submit(fear_greed)
        f_g = ex.submit(global_stat)
        f_fund = ex.submit(funding_and_ratio)
        f_tr = ex.submit(trending)
        f_hl = ex.submit(headlines)
        fg = f_fg.result() if True else None
        gstat = f_g.result()
        fund = f_fund.result()
        tr = f_tr.result()
        hl = f_hl.result()
    men = coin_mentions(hl)
    sc = sentiment_score(fg, hl, fund, tr, gstat)
    res = {
        "ts": int(time.time()),
        "time": time.strftime("%Y-%m-%d %H:%M"),
        "sentiment": sc,
        "fear_greed": fg,
        "global": gstat,
        "funding": fund,
        "trending": tr,
        "news": {"count": len(hl), "bull": sum(1 for h in hl if h["score"] > 0),
                 "bear": sum(1 for h in hl if h["score"] < 0),
                 "top": sorted(hl, key=lambda x: -abs(x["score"]))[:12]},
        "mentions": men,
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "latest.json"), "w") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    return res


def coin_sentiment(sym, data):
    """单个币的消息面加减分（用于扫描器微调评分）"""
    adj, notes = 0.0, []
    m = data.get("mentions", {}).get(sym)
    if m:
        net = m["score"]
        adj += max(-8, min(8, net * 2.5))
        if abs(net) >= 2:
            notes.append(f"消息面 {m['n']} 条，净情绪 {net:+d}")
    f = data.get("funding", {}).get(sym)
    if f and "funding" in f:
        fr = f["funding"]
        if fr > 0.0005:
            adj -= 5; notes.append(f"资金费率 {fr*100:.3f}% 偏高，多头拥挤")
        elif fr < -0.0003:
            adj += 4; notes.append(f"资金费率 {fr*100:.3f}% 为负，空头拥挤易轧空")
    if sym in (data.get("trending") or []):
        adj += 3; notes.append("交易所热搜榜在榜，散户关注度高")
    ls = (data.get("funding", {}).get(sym) or {}).get("ls_ratio")
    if ls:
        if ls > 2.0:
            adj -= 4; notes.append(f"多空持仓比 {ls:.2f}，散户多头过度")
        elif ls < 0.8:
            adj += 3; notes.append(f"多空持仓比 {ls:.2f}，空头占优")
    return adj, notes


if __name__ == "__main__":
    d = collect()
    print(f"⏱ {d['time']}   市场情绪综合分 {d['sentiment']}/100")
    print(f"   恐惧贪婪 {d['fear_greed'].get('now')}（{d['fear_greed'].get('label')}）"
          f"  近7日 {d['fear_greed'].get('prev7')}")
    g = d["global"]
    print(f"   总市值 {g.get('mcap_usd',0)/1e12:.2f} 万亿U（24h {g.get('mcap_chg24',0):+.2f}%）"
          f"  BTC 占比 {g.get('btc_dom',0):.1f}%")
    print(f"   新闻 {d['news']['count']} 条：看多 {d['news']['bull']} / 看空 {d['news']['bear']}")
    for h in d["news"]["top"][:5]:
        print(f"     [{h['score']:+d}] {h['title'][:74]}")
    print("   资金费率:", {k: round(v.get('funding', 0) * 100, 4) for k, v in d["funding"].items()})
    print("   热搜:", ", ".join(d["trending"][:10]))


NEWS_DIR = "/tmp/crypto-news"


def snapshot(ttl=1800):
    """消息面快照（默认缓存 15 分钟，避免频繁抓新闻）"""
    f = os.path.join(NEWS_DIR, "latest.json")
    try:
        if time.time() - os.path.getmtime(f) < ttl:
            with open(f) as fh:
                return json.load(fh)
    except Exception:
        pass
    d = collect()
    try:
        os.makedirs(NEWS_DIR, exist_ok=True)
        with open(f, "w") as fh:
            json.dump(d, fh, ensure_ascii=False)
    except Exception:
        pass
    return d


def market_adjust(data):
    """市场情绪对整体做多评分的加减分（逆向思维：极端情绪反向操作）"""
    d, notes = 0.0, []
    fg = (data.get("fear_greed") or {}).get("now")
    if fg is not None:
        if fg >= 80:
            d -= 7; notes.append(f"恐慌贪婪指数 {fg}（极度贪婪）—— 追多有接盘风险")
        elif fg >= 65:
            d -= 2; notes.append(f"恐慌贪婪指数 {fg}（偏贪婪）")
        elif fg <= 20:
            d += 7; notes.append(f"恐慌贪婪指数 {fg}（极度恐慌）—— 历史上常是底部区")
        elif fg <= 35:
            d += 3; notes.append(f"恐慌贪婪指数 {fg}（偏恐慌）")
    g = data.get("global") or {}
    mc = g.get("mcap_chg24")
    if mc is not None:
        if mc < -5:
            d -= 3; notes.append(f"全市场市值 24h {mc:.1f}%，处于急跌中，抄底要分批")
        elif mc > 5:
            d -= 2; notes.append(f"全市场市值 24h {mc:+.1f}%，涨速过快需防回调")
    fund = [v.get("funding") for v in (data.get("funding") or {}).values()
            if isinstance(v, dict) and v.get("funding") is not None]
    if fund:
        avg = sum(fund) / len(fund)
        if avg > 0.04:
            d -= 3; notes.append(f"主流币资金费率均值 +{avg:.3f}%，多头拥挤（易被插针）")
        elif avg < -0.02:
            d += 3; notes.append(f"主流币资金费率均值 {avg:+.3f}%，空头拥挤（易逼空）")
    return d, notes


if __name__ == "__main__" and True:
    import sys as _s
    _d = collect()
    print(json.dumps({"sentiment": _d["sentiment"], "btc_dominance": (_d.get("global") or {}).get("btc_dominance")}, ensure_ascii=False))
