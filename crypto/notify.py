#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""notify.py — 多渠道消息推送（交易信号 / 持仓警报）

支持的渠道（全部从环境变量读取，配了哪个用哪个，可同时配多个）：

  BARK_KEY            Bark（iOS，现有）
  PUSHPLUS_TOKEN      PushPlus 推送加（→ 微信）
  SERVERCHAN_KEY      Server酱 方糖（→ 微信服务号）
  WECOM_WEBHOOK       企业微信机器人（→ 企业微信/微信）
  WXPUSHER_APPTOKEN   WxPusher（→ 微信）
  WXPUSHER_UID        WxPusher 接收者 UID

用法：
  python3 notify.py --title "标题" --body "内容"
  python3 notify.py --test
  import notify; notify.push("标题", "内容")
"""
import os as _os
_STRAT = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "strategy")

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def _post(url, data, is_json=True, timeout=20):
    body = json.dumps(data).encode() if is_json else urllib.parse.urlencode(data).encode()
    hdr = {"User-Agent": "minis-notify/1.0"}
    hdr["Content-Type"] = "application/json" if is_json else "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, method="POST", headers=hdr)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()[:300].decode("utf-8", "replace")


# ---------------- 各渠道实现 ----------------
def send_bark(title, body, level="active"):
    key = os.environ.get("BARK_KEY", "").strip()
    if not key:
        return None
    url = (f"https://api.day.app/{key}/{urllib.parse.quote(title, safe='')}/"
           f"{urllib.parse.quote(body, safe='')}?group=crypto&level={level}")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "minis/1.0"}),
                                    timeout=20) as r:
            return "bark", r.status, r.read()[:120].decode("utf-8", "replace")
    except Exception as e:
        return "bark", 0, str(e)[:120]


def send_pushplus(title, body):
    tok = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    if not tok:
        return None
    try:
        s, t = _post("https://www.pushplus.plus/send",
                     {"token": tok, "title": title, "content": body, "template": "markdown"})
        return "pushplus", s, t
    except Exception as e:
        return "pushplus", 0, str(e)[:120]


def send_serverchan(title, body):
    key = os.environ.get("SERVERCHAN_KEY", "").strip()
    if not key:
        return None
    try:
        s, t = _post(f"https://sctapi.ftqq.com/{key}.send",
                     {"title": title, "desp": body}, is_json=False)
        return "serverchan", s, t
    except Exception as e:
        return "serverchan", 0, str(e)[:120]


def send_wecom(title, body):
    hook = os.environ.get("WECOM_WEBHOOK", "").strip()
    if not hook:
        return None
    try:
        s, t = _post(hook, {"msgtype": "markdown",
                            "markdown": {"content": f"**{title}**\n{body}"}})
        return "wecom", s, t
    except Exception as e:
        return "wecom", 0, str(e)[:120]


def send_wxpusher(title, body):
    tok = os.environ.get("WXPUSHER_APPTOKEN", "").strip()
    uid = os.environ.get("WXPUSHER_UID", "").strip()
    if not (tok and uid):
        return None
    try:
        s, t = _post("https://wxpusher.zjiecode.com/api/send/message",
                     {"appToken": tok, "content": body, "summary": title[:99],
                      "contentType": 3, "uids": [uid]})
        return "wxpusher", s, t
    except Exception as e:
        return "wxpusher", 0, str(e)[:120]


CHANNELS = [send_bark, send_pushplus, send_wecom, send_wxpusher, send_serverchan]


def push(title, body, level="active", verbose=True):
    """向所有已配置的渠道推送，返回结果列表"""
    out = []
    for fn in CHANNELS:
        try:
            r = fn(title, body) if fn is not send_bark else fn(title, body, level)
        except TypeError:
            r = fn(title, body)
        except Exception as e:
            r = (fn.__name__, 0, str(e)[:100])
        if r:
            out.append(r)
            if verbose:
                ok = "✅" if r[1] == 200 else "❌"
                print(f"  {ok} {r[0]:<11} {r[1]}  {r[2][:90]}")
    if not out:
        print("  ⚠️ 没有任何推送渠道已配置")
    return out


def configured():
    m = {"bark": "BARK_KEY", "pushplus": "PUSHPLUS_TOKEN", "serverchan": "SERVERCHAN_KEY",
         "wecom": "WECOM_WEBHOOK", "wxpusher": "WXPUSHER_APPTOKEN"}
    return [k for k, v in m.items() if os.environ.get(v, "").strip()]


def main():
    a = sys.argv[1:]
    if "--test" in a:
        print("已配置的渠道:", configured() or "（无）")
        print()
        push("✅ 推送测试", "如果你在微信/手机上看到这条，说明渠道配好了。",
             level="timeSensitive")
        return 0
    t = a[a.index("--title") + 1] if "--title" in a else "通知"
    b = a[a.index("--body") + 1] if "--body" in a else ""
    push(t, b)
    return 0


if __name__ == "__main__":
    sys.exit(main())
