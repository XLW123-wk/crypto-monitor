#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""行情数据层 —— 纯币安（Binance）/ 强制 IPv4 / gzip / 本地缓存

两个关键坑，都已修掉：
  1. 本机 IPv6 路由被黑洞 → 全局强制 AF_INET，否则每个请求先挂 20 秒。
  2. 不请求 gzip → 全市场快照 1.9MB / 7.8 秒；开 gzip 后 281KB / 2.3 秒。

说明：api.binance.com 对本地区返回 HTTP 451（地域封锁），
官方公开行情镜像 data-api.binance.vision 完全可用，行情与主站一致。
"""
import os as _os
_STRAT = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "strategy")

import gzip, io, json, os, socket, time, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

# ---------- 1. 强制 IPv4 ----------
_orig_gai = socket.getaddrinfo


def _gai_v4(host, port, family=0, type=0, proto=0, flags=0):
    try:
        res = _orig_gai(host, port, socket.AF_INET, type, proto, flags)
        if res:
            return res
    except Exception:
        pass
    return _orig_gai(host, port, family, type, proto, flags)


socket.getaddrinfo = _gai_v4
socket.has_ipv6 = False

BINANCE = "https://data-api.binance.vision"
CACHE_DIR = "/tmp/crypto-cache"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")

# ---------- 2. 缓存 ----------
def _cpath(key):
    os.makedirs(CACHE_DIR, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)
    return os.path.join(CACHE_DIR, safe[:120] + ".json")


def cache_get(key, ttl):
    f = _cpath(key)
    try:
        if ttl and time.time() - os.path.getmtime(f) < ttl:
            with open(f) as fh:
                return json.load(fh)
    except Exception:
        pass
    return None


def cache_put(key, val):
    try:
        with open(_cpath(key), "w") as fh:
            json.dump(val, fh)
    except Exception:
        pass


# ---------- 3. HTTP（带 gzip） ----------
STATS = {}


def _bump(src, ok):
    d = STATS.setdefault(src, {"ok": 0, "fail": 0})
    d["ok" if ok else "fail"] += 1


def fetch_raw(url, timeout=25, retry=3, gz=True, headers=None):
    """返回文本；自动 gzip 解压与重试。headers 用于需要 API Key 的私有接口"""
    h = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        h.update(headers)
    if gz:
        h["Accept-Encoding"] = "gzip, deflate"
    last = None
    for a in range(retry):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    body = gzip.GzipFile(fileobj=io.BytesIO(body)).read()
                return body.decode()
        except urllib.error.HTTPError as e:
            try:
                last = f"HTTP {e.code} {e.read().decode()[:140]}"
            except Exception:
                last = f"HTTP {e.code}"
            if e.code in (400, 403, 404, 451):
                break
            if e.code in (418, 429):        # 限速：退避后重试
                time.sleep(3 * (a + 1))
                continue
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        if a < retry - 1:
            time.sleep(0.6 * (a + 1))
    raise RuntimeError(f"请求失败 {url.split('?')[0]} → {last}")


def get_json(url, timeout=25, retry=3, headers=None):
    """抓取任意 URL 的 JSON（同样强制 IPv4 + gzip）"""
    return json.loads(fetch_raw(url, timeout=timeout, retry=retry, headers=headers))


def get(path, timeout=25, retry=3, **params):
    url = BINANCE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return json.loads(fetch_raw(url, timeout, retry))


# ---------- 4. 全市场快照 ----------
def tickers(ttl=300):
    """币安全市场 24h 快照 → {pair: {...}}，pair 形如 BTC_USDT
    耗时约 2-8 秒（746 个 USDT 对），默认缓存 5 分钟。
    """
    c = cache_get("bn_tickers", ttl)
    if c:
        return c
    raw = get("/api/v3/ticker/24hr", timeout=90, retry=3)
    _bump("ticker24hr", True)
    out = {}
    for t in raw:
        sym = t["symbol"]
        if not sym.endswith("USDT") or len(sym) <= 4:
            continue
        base = sym[:-4]
        # 过滤杠杆代币与稳定币对
        if base.endswith(("UP", "DOWN", "BULL", "BEAR")) or base in (
                "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "EURI", "AEUR",
                "USD1", "EUR", "GBP", "TRY", "BRL", "ARS", "ZAR", "UAH", "RUB",
                "PLN", "RON", "CZK", "MXN", "COP", "JPY", "IDRT", "VAI", "USTC"):
            continue
        try:
            out[base + "_USDT"] = {
                "pair": base + "_USDT", "symbol": sym,
                "last": float(t["lastPrice"]),
                "change_percentage": float(t["priceChangePercent"]),
                "quote_volume": float(t["quoteVolume"]),
                "high_24h": float(t["highPrice"]),
                "low_24h": float(t["lowPrice"]),
                "trades": int(t.get("count") or 0),
            }
        except (KeyError, ValueError):
            continue
    cache_put("bn_tickers", out)
    return out


# ---------- 5. K 线 ----------
def klines(pair, interval="4h", limit=200, ttl=900):
    """返回 dict(t,o,h,l,c,v)。v = 计价货币成交额(quoteVolume)"""
    ck = f"bnk_{pair}_{interval}_{limit}"
    c = cache_get(ck, ttl)
    if c:
        return c
    sym = pair.replace("_", "")
    raw = get("/api/v3/klines", symbol=sym, interval=interval, limit=limit,
              timeout=30, retry=2)
    d = {"t": [], "o": [], "h": [], "l": [], "c": [], "v": []}
    for r in raw:
        d["t"].append(int(r[0] // 1000))
        d["o"].append(float(r[1]))
        d["h"].append(float(r[2]))
        d["l"].append(float(r[3]))
        d["c"].append(float(r[4]))
        d["v"].append(float(r[7]))
    _bump("klines", True)
    if d["c"]:
        cache_put(ck, d)
    return d


def klines_map(pairs, interval, limit, workers=10, ttl=300):
    """并发批量取 K 线 → {pair: data}；取不到的币对直接跳过"""
    out = {}
    def w(p):
        try:
            return p, klines(p, interval, limit, ttl)
        except Exception:
            _bump("klines", False)
            return p, None
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for p, d in ex.map(w, pairs):
            if d and d.get("c"):
                out[p] = d
    return out


# ---------- 6. 实时价 ----------
def prices(pairs):
    """批量取价。币安支持一次请求传多个 symbol。"""
    pairs = list(pairs)
    res = {}
    try:
        syms = [p.replace("_", "") for p in pairs]
        q = urllib.parse.quote(json.dumps(syms, separators=(",", ":")))
        raw = json.loads(fetch_raw(f"{BINANCE}/api/v3/ticker/price?symbols={q}",
                                   timeout=25, retry=1))
        m = {r["symbol"]: float(r["price"]) for r in raw}
        for p in pairs:
            if p.replace("_", "") in m:
                res[p] = m[p.replace("_", "")]
        _bump("price", True)
    except Exception:
        _bump("price", False)
    missing = [p for p in pairs if p not in res]
    if missing:
        t = tickers(ttl=120)
        for p in missing:
            if p in t:
                res[p] = t[p]["last"]
    return res


def price(pair):
    return prices([pair]).get(pair)


# ---------- 7. 基准 ----------
def btc_benchmark(ttl=600):
    c = cache_get("btc_bench", ttl)
    if c:
        return c
    d = klines("BTC_USDT", "1d", 60, ttl=600)
    c = d["c"]
    if len(c) < 31:
        return {"chg7d": 0, "chg30d": 0}
    r = {"chg7d": (c[-1] / c[-8] - 1) * 100, "chg30d": (c[-1] / c[-31] - 1) * 100}
    cache_put("btc_bench", r)
    return r


def stats():
    return dict(STATS)


def selftest():
    print("── 币安数据源自检（强制 IPv4 + gzip）──")
    t0 = time.time()
    t = tickers(ttl=0)
    print(f"  ✅ 全市场快照 {len(t)} 个 USDT 币对  ({time.time()-t0:.1f}s)")
    top = sorted(t.values(), key=lambda x: -x["quote_volume"])[:3]
    for x in top:
        print(f"       {x['pair']:12} {x['last']:>12,.4f} {x['change_percentage']:>+7.2f}%")
    t0 = time.time()
    k = klines("BTC_USDT", "4h", 200)
    print(f"  ✅ BTC 4h K线 {len(k['c'])} 根，最新收盘 {k['c'][-1]:,.2f}  ({time.time()-t0:.1f}s)")
    t0 = time.time()
    p = prices(["BTC_USDT", "ETH_USDT", "SOL_USDT", "DOGE_USDT"])
    print(f"  ✅ 批量取价 {p}  ({time.time()-t0:.1f}s)")
    print(f"  ✅ BTC 基准 {btc_benchmark()}")
    print(f"  📊 请求统计 {stats()}")


if __name__ == "__main__":
    selftest()
