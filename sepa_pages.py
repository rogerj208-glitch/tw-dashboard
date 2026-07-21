#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SEPA 市場儀表板（GitHub Pages 靜態版）
每日由 GitHub Actions 執行：抓資料 → 產生 docs/index.html（手機優先）
涵蓋：融資水位分數、維持率、資金流、台指期三大法人 OI、跨市場資金流向、處置股清單
"""
import json, os, time, csv, io
from datetime import date, datetime, timedelta, timezone

import requests

S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0"})

BASE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(BASE, "docs")
os.makedirs(DOCS, exist_ok=True)
CACHE_FILE = os.path.join(DOCS, "cache.json")

TPE = timezone(timedelta(hours=8))
TODAY = datetime.now(TPE).date()
NOW_STR = datetime.now(TPE).strftime("%Y-%m-%d %H:%M")


def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_cache(c):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False)


def to_f(s):
    try:
        return float(str(s).replace(",", ""))
    except Exception:
        return None


# ── 融資餘額 ─────────────────────────────────────────
def fetch_margin_day(ds):
    try:
        r = S.get(f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={ds}&selectType=MS&response=json", timeout=12)
        d = r.json()
        if d.get("stat") != "OK":
            return None
        def walk(o):
            if isinstance(o, list):
                if o and isinstance(o[0], str) and "融資金額" in o[0]:
                    for v in reversed(o):
                        f = to_f(v)
                        if f:
                            return f
                for x in o:
                    rr = walk(x)
                    if rr:
                        return rr
            elif isinstance(o, dict):
                for x in o.values():
                    rr = walk(x)
                    if rr:
                        return rr
        bal = walk(d)
        return round(bal / 100000, 1) if bal else None
    except Exception:
        return None


def fetch_mratio_day(ds, bal_yi):
    try:
        r1 = S.get(f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={ds}&selectType=ALL&response=json", timeout=25)
        d1 = r1.json()
        if d1.get("stat") != "OK":
            return None
        shares = {}
        def walk1(o):
            if isinstance(o, dict):
                fs, da = o.get("fields"), o.get("data")
                if fs and da and any(str(f).strip() in ("代號", "股票代號") for f in fs) and any("今日餘額" in str(f) for f in fs):
                    bi = next(i for i, f in enumerate(fs) if "今日餘額" in str(f))
                    for row in da:
                        try:
                            cd = str(row[0]).strip()
                            if len(cd) == 4 and cd.isdigit():
                                shares[cd] = to_f(row[bi]) or 0
                        except Exception:
                            pass
                for v in o.values():
                    walk1(v)
            elif isinstance(o, list):
                for v in o:
                    walk1(v)
        walk1(d1)
        if not shares:
            return None
        r2 = S.get(f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={ds}&type=ALLBUT0999&response=json", timeout=30)
        d2 = r2.json()
        closes = {}
        def walk2(o):
            if isinstance(o, dict):
                fs, da = o.get("fields"), o.get("data")
                if fs and da and any("收盤價" in str(f) for f in fs) and any("證券代號" in str(f) for f in fs):
                    ci = next(i for i, f in enumerate(fs) if "收盤價" in str(f))
                    for row in da:
                        try:
                            cd = str(row[0]).strip()
                            px = to_f(row[ci])
                            if len(cd) == 4 and cd.isdigit() and px:
                                closes[cd] = px
                        except Exception:
                            pass
                for v in o.values():
                    walk2(v)
            elif isinstance(o, list):
                for v in o:
                    walk2(v)
        walk2(d2)
        if not closes:
            return None
        coll = sum(sh * 1000 * closes[cd] for cd, sh in shares.items() if cd in closes)
        loan = bal_yi * 1e8
        return round(coll / loan * 100, 1) if loan > 0 and coll > 0 else None
    except Exception:
        return None


def fetch_taiex_month(y, m):
    out = {}
    try:
        r = S.get(f"https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK?date={y}{m:02d}01&response=json", timeout=12)
        for row in r.json().get("data", []):
            try:
                p = row[0].split("/")
                k = f"{int(p[0])+1911}{int(p[1]):02d}{int(p[2]):02d}"
                out[k] = to_f(row[4])
            except Exception:
                pass
    except Exception:
        pass
    return out


def fetch_foreign_day(ds):
    try:
        r = S.get(f"https://www.twse.com.tw/rwd/zh/fund/BFI82U?dayDate={ds}&type=day&response=json", timeout=12)
        d = r.json()
        if d.get("stat") != "OK":
            return None
        for row in d.get("data", []):
            if isinstance(row, list) and row and "外資及陸資" in str(row[0]) and "外資自營" not in str(row[0]):
                v = to_f(row[-1])
                return round(v / 1e8, 1) if v is not None else None
    except Exception:
        pass
    return None


def fetch_taifex_oi(sd, ed):
    out = {}
    try:
        r = S.post("https://www.taifex.com.tw/cht/3/futContractsDateDown",
                   data={"queryStartDate": sd, "queryEndDate": ed}, timeout=25)
        text = r.content.decode("big5", errors="ignore")
        rows = list(csv.reader(io.StringIO(text)))
        if not rows:
            return out
        hd = rows[0]
        try:
            ni = next(i for i, h in enumerate(hd) if "多空未平倉口數淨額" in h)
        except StopIteration:
            ni = 13
        rm = {"外資": "foreign", "投信": "trust", "自營商": "dealer"}
        for row in rows[1:]:
            if len(row) <= ni or "臺股期貨" not in (row[1] if len(row) > 1 else ""):
                continue
            role = next((v for k, v in rm.items() if k in (row[2] or "")), None)
            if not role:
                continue
            try:
                out.setdefault(row[0].strip().replace("/", ""), {})[role] = int(str(row[ni]).replace(",", ""))
            except Exception:
                pass
    except Exception as e:
        print("taifex err", e)
    return out


def fetch_disposal():
    out = []
    try:
        r = S.get("https://openapi.twse.com.tw/v1/announcement/punish", timeout=15)
        for it in r.json():
            cd = (it.get("Code") or "").strip()
            if len(cd) == 4 and cd.isdigit():
                out.append({"code": cd, "name": (it.get("Name") or "").strip(),
                            "market": "上市", "period": (it.get("DispositionPeriod") or "").strip()})
    except Exception:
        pass
    try:
        r = S.get("https://www.tpex.org.tw/openapi/v1/tpex_disposal_information", timeout=15)
        for it in r.json():
            cd = (it.get("SecuritiesCompanyCode") or "").strip()
            if len(cd) == 4 and cd.isdigit():
                out.append({"code": cd, "name": (it.get("CompanyName") or "").strip(),
                            "market": "上櫃", "period": (it.get("DispositionPeriod") or "").strip()})
    except Exception:
        pass
    return out


def yf_chg20_and_hist(sym, period="6mo"):
    try:
        import yfinance as yf
        h = yf.Ticker(sym).history(period=period, interval="1d")["Close"].dropna()
        if len(h) > 21:
            cur = float(h.iloc[-1])
            chg = round((cur / float(h.iloc[-21]) - 1) * 100, 2)
            return cur, chg, [round(float(v), 3) for v in h.iloc[-90:]]
    except Exception as e:
        print("yf err", sym, e)
    return None, None, []


# ── 主流程 ─────────────────────────────────────────
def main():
    cache = load_cache()
    mh = cache.get("margin", {})
    fh = cache.get("foreign", {})
    oi = cache.get("taifex", {})

    # 交易日清單（近130個平日）
    wdays = []
    d = TODAY
    while len(wdays) < 130:
        if d.weekday() < 5:
            wdays.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=1)
    wdays.reverse()

    # 融資餘額（補缺）
    for ds in wdays:
        if ds not in mh:
            v = fetch_margin_day(ds)
            if v:
                mh[ds] = {"bal": v}
            time.sleep(0.3)
    # 維持率（近45天補缺，控制執行時間）
    mkeys = sorted(k for k in mh if mh[k].get("bal"))[-45:]
    for ds in mkeys:
        if "mratio" not in mh[ds]:
            mr = fetch_mratio_day(ds, mh[ds]["bal"])
            if mr:
                mh[ds]["mratio"] = mr
            time.sleep(0.4)
    # 大盤指數
    for ym in sorted({k[:6] for k in mh}):
        if any("taiex" not in v for k, v in mh.items() if k[:6] == ym):
            tm = fetch_taiex_month(int(ym[:4]), int(ym[4:6]))
            for k, v in tm.items():
                if k in mh:
                    mh[k]["taiex"] = v
    # 外資買賣超
    for ds in wdays:
        if ds not in fh:
            v = fetch_foreign_day(ds)
            if v is not None:
                fh[ds] = v
            time.sleep(0.3)
    # 台指期 OI（近3個月分段）
    for chunk in range(3):
        ed = TODAY - timedelta(days=chunk * 30)
        sd = ed - timedelta(days=29)
        probe = [(sd + timedelta(days=x)).strftime("%Y%m%d") for x in range(30)]
        missing = [p for p in probe if p not in oi and date(int(p[:4]), int(p[4:6]), int(p[6:8])).weekday() < 5]
        if len(missing) <= 1 and chunk > 0:
            continue
        oi.update(fetch_taifex_oi(sd.strftime("%Y/%m/%d"), ed.strftime("%Y/%m/%d")))
        time.sleep(0.5)

    cache.update({"margin": mh, "foreign": fh, "taifex": oi})
    save_cache(cache)

    # ── 計算 ──
    keys = sorted(k for k in mh if mh[k].get("bal"))[-120:]
    seq = [{"date": k, **mh[k]} for k in keys]
    bals = [x["bal"] for x in seq]
    if len(bals) < 25:
        print(f"ERROR: 融資資料不足（{len(bals)} 天），可能是 TWSE 連線失敗")
        raise SystemExit(1)
    cur = bals[-1]
    lo, hi = min(bals), max(bals)
    pct = round((cur - lo) / (hi - lo) * 100, 1) if hi > lo else 50.0
    g20 = (cur / bals[-21] - 1) * 100 if len(bals) > 21 else 0
    sg = max(0, min(100, (g20 + 3) / 11 * 100))
    tx = [x.get("taiex") for x in seq if x.get("taiex")]
    tg20 = (tx[-1] / tx[-21] - 1) * 100 if len(tx) > 21 else 0
    div = g20 - tg20
    sd_ = max(0, min(100, (div + 4) / 10 * 100))
    score = round(pct * 0.4 + sg * 0.3 + sd_ * 0.3, 1)
    stage = ("極低水位" if score < 20 else "偏低" if score < 40 else "中性" if score < 60 else "偏熱" if score < 80 else "過熱")
    mrs = [x["mratio"] for x in seq if x.get("mratio")]
    mratio = mrs[-1] if mrs else None

    fkeys = sorted(fh)[-120:]
    fseq = [{"date": k, "net": fh[k]} for k in fkeys]
    cum20 = round(sum(x["net"] for x in fseq[-20:]), 0)
    cum_ytd = round(sum(x["net"] for x in fseq if x["date"][:4] == str(TODAY.year)), 0)
    last_net = fseq[-1]["net"] if fseq else None

    twd_cur, twd_chg, _ = yf_chg20_and_hist("TWD=X")
    dxy_cur, dxy_chg, _ = yf_chg20_and_hist("DX-Y.NYB")

    okeys = sorted(oi)[-90:]
    oseq = [{"date": k, **oi[k]} for k in okeys if oi[k].get("foreign") is not None]
    ocur = oseq[-1] if oseq else {}

    mkts_def = [("台灣 🇹🇼", "^TWII", "TWD=X"), ("韓國 🇰🇷", "^KS11", "KRW=X"),
                ("日本 🇯🇵", "^N225", "JPY=X"), ("中國 🇨🇳", "000001.SS", "CNY=X"),
                ("香港 🇭🇰", "^HSI", None), ("印度 🇮🇳", "^NSEI", "INR=X"),
                ("美國 🇺🇸", "^GSPC", None)]
    markets = []
    for nm, isym, fsym in mkts_def:
        _, ic, _ = yf_chg20_and_hist(isym, "3mo")
        fc = None
        if fsym:
            _, fc, _ = yf_chg20_and_hist(fsym, "3mo")
        if ic is None:
            continue
        fscore = ic + (dxy_chg or 0) if nm.startswith("美國") else ic - (fc or 0)
        tend = ("偏流入" if ic > 1 and (fc is None or fc < 0.5) else
                "偏流出" if ic < -1 and (fc is not None and fc > 0.5) else
                "偏弱" if ic < -1 else "偏強" if ic > 1 else "中性")
        markets.append({"name": nm, "idx": ic, "fx": fc, "score": round(fscore, 2), "tend": tend})
    markets.sort(key=lambda x: -x["score"])

    disposal = fetch_disposal()

    # 判讀
    f_sell = cum20 < -300
    twd_weak = (twd_chg or 0) > 0.8
    dxy_up = (dxy_chg or 0) > 1
    if f_sell and twd_weak and dxy_up:
        verdict = "外資賣超且台幣走貶、美元轉強 → 資金傾向真實流出、回流美元資產。"
    elif f_sell and twd_weak:
        verdict = "外資賣超且台幣走貶 → 資金流出台灣，觀察上方雷達哪個市場股匯雙強。"
    elif f_sell:
        verdict = "外資賣超但台幣穩 → 資金多停泊台灣觀望，壓力可控。"
    elif cum20 > 300 and not twd_weak:
        verdict = "外資買超且台幣偏強 → 資金匯入布局，資金面偏多。"
    else:
        verdict = "資金面中性。"
    of = ocur.get("foreign")
    if of is not None and of < -20000:
        verdict = f"外資台指期淨空單 {abs(of):,} 口。" + verdict
    elif of is not None and of > 20000:
        verdict = f"外資台指期淨多單 {of:,} 口。" + verdict

    html = render(score, stage, cur, lo, hi, round(g20, 2), round(tg20, 2), round(div, 2),
                  mratio, seq, last_net, cum20, cum_ytd, fseq, twd_cur, twd_chg,
                  dxy_cur, dxy_chg, oseq, ocur, markets, disposal, verdict)
    with open(os.path.join(DOCS, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    print(f"done. score={score} mratio={mratio} ytd={cum_ytd}")


# ── SVG helpers ─────────────────────────────────────
def svg_lines(series_list, W=680, H=170, P=8):
    """series_list: [(values, color, width, opacity)]"""
    parts = [f'<svg viewBox="0 0 {W} {H}" style="width:100%;height:auto;background:var(--s2);border-radius:10px">']
    for vals, color, sw, op in series_list:
        vv = [v for v in vals if v is not None]
        if len(vv) < 2:
            continue
        mn, mx = min(vv), max(vv)
        pts = []
        n = len(vals)
        for i, v in enumerate(vals):
            if v is None:
                continue
            x = P + i / (n - 1) * (W - 2 * P)
            y = H - P - (v - mn) / (mx - mn or 1) * (H - 2 * P)
            pts.append(f"{x:.1f},{y:.1f}")
        parts.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="{color}" stroke-width="{sw}" opacity="{op}"/>')
    parts.append("</svg>")
    return "".join(parts)


def svg_bars(vals, W=680, H=110, P=6):
    if not vals:
        return ""
    mx = max(abs(v) for v in vals) or 1
    bw = (W - 2 * P) / len(vals)
    parts = [f'<svg viewBox="0 0 {W} {H}" style="width:100%;height:auto;background:var(--s2);border-radius:10px">',
             f'<line x1="0" y1="{H/2}" x2="{W}" y2="{H/2}" stroke="var(--b)" stroke-width="1"/>']
    for i, v in enumerate(vals):
        bh = abs(v) / mx * (H / 2 - P)
        y = H / 2 - bh if v >= 0 else H / 2
        color = "#ef4444" if v >= 0 else "#22c55e"
        parts.append(f'<rect x="{P+i*bw:.1f}" y="{y:.1f}" width="{max(bw-1,1):.1f}" height="{max(bh,1):.1f}" fill="{color}" opacity=".85"/>')
    parts.append("</svg>")
    return "".join(parts)


def render(score, stage, bal, lo, hi, g20, tg20, div, mratio, seq,
           last_net, cum20, cum_ytd, fseq, twd_cur, twd_chg, dxy_cur, dxy_chg,
           oseq, ocur, markets, disposal, verdict):
    color = "#3b82f6" if score < 20 else "#22c55e" if score < 40 else "#eab308" if score < 60 else "#f97316" if score < 80 else "#ef4444"
    mr_color = "#94a3b8" if not mratio else "#ef4444" if mratio < 130 else "#f97316" if mratio < 140 else "#eab308" if mratio < 155 else "#22c55e"

    chart1 = svg_lines([
        ([x.get("taiex") for x in seq[-90:]], "#94a3b8", 1.5, .5),
        ([x["bal"] for x in seq[-90:]], color, 2, 1),
        ([x.get("mratio") for x in seq[-90:]], "#8b5cf6", 1.5, .9),
    ])
    chart2 = svg_bars([x["net"] for x in fseq[-60:]])
    chart3 = svg_lines([
        ([x.get("foreign") for x in oseq], "#ef4444", 2, 1),
        ([x.get("trust") for x in oseq], "#3b82f6", 1.5, .9),
        ([x.get("dealer") for x in oseq], "#eab308", 1.5, .9),
    ], H=140)

    max_abs = max((abs(m["score"]) for m in markets), default=1)
    radar_rows = ""
    for m in markets:
        pos = m["score"] >= 0
        w = abs(m["score"]) / max_abs * 46
        bc = "#ef4444" if pos else "#22c55e"
        tc = "#ef4444" if m["tend"] in ("偏流入", "偏強") else "#22c55e" if m["tend"] in ("偏流出", "偏弱") else "var(--tx3)"
        fx = "" if m["fx"] is None else f'｜幣 {"+" if m["fx"]>=0 else ""}{m["fx"]}%'
        bar_style = f'left:50%;border-radius:0 6px 6px 0' if pos else f'right:50%;border-radius:6px 0 0 6px'
        radar_rows += f'''<div class="mrow">
          <div class="mname">{m["name"]}</div>
          <div class="mbar"><i></i><b style="{bar_style};width:{w:.1f}%;background:{bc}"></b></div>
          <div class="mtag" style="color:{tc}">{m["tend"]}</div>
          <div class="msub">指數 {"+" if m["idx"]>=0 else ""}{m["idx"]}%{fx}</div>
        </div>'''

    disp_rows = "".join(
        f'<div class="drow"><b>{d["code"]} {d["name"]}</b><span>{d["market"]}</span><span class="dp">{d["period"]}</span></div>'
        for d in disposal) or '<div style="color:var(--tx3);font-size:13px;padding:8px 0">目前無處置股 🎉</div>'

    def card(label, val, sub="", vcolor="var(--tx)"):
        return f'''<div class="card"><div class="cl">{label}</div>
        <div class="cv" style="color:{vcolor}">{val}</div><div class="cs">{sub}</div></div>'''

    cards1 = "".join([
        card("融資維持率(估)", f"{mratio}%" if mratio else "—", "130%=斷頭出清線", mr_color),
        card("融資餘額", f"{bal:,.0f} 億", f"區間 {lo:,.0f}~{hi:,.0f}"),
        card("融資20日增速", f"{'+' if g20>=0 else ''}{g20}%", f"大盤同期 {'+' if tg20>=0 else ''}{tg20}%",
             "#ef4444" if g20 >= 0 else "#22c55e"),
        card("融資/大盤背離", f"{'+' if div>=0 else ''}{div}%", "正值=融資漲快於指數"),
    ])
    cards2 = "".join([
        card("外資今日買賣超", f"{'+' if (last_net or 0)>=0 else ''}{last_net} 億" if last_net is not None else "—", "",
             "#ef4444" if (last_net or 0) >= 0 else "#22c55e"),
        card("近20日累計", f"{'+' if cum20>=0 else ''}{cum20:,.0f} 億", "",
             "#ef4444" if cum20 >= 0 else "#22c55e"),
        card("今年累計", f"{'+' if cum_ytd>=0 else ''}{cum_ytd:,.0f} 億", "",
             "#ef4444" if cum_ytd >= 0 else "#22c55e"),
        card("美元/台幣", f"{twd_cur:.2f}" if twd_cur else "—",
             f"20日 {'+' if (twd_chg or 0)>=0 else ''}{twd_chg}%" + ("（台幣貶⚠️）" if (twd_chg or 0) > 0.8 else "")),
        card("美元指數", f"{dxy_cur:.1f}" if dxy_cur else "—",
             f"20日 {'+' if (dxy_chg or 0)>=0 else ''}{dxy_chg}%"),
    ])
    cards3 = "".join([
        card("外資期貨淨OI", f"{'+' if (ocur.get('foreign') or 0)>=0 else ''}{ocur.get('foreign', 0):,} 口", "",
             "#ef4444" if (ocur.get("foreign") or 0) >= 0 else "#22c55e"),
        card("投信淨OI", f"{'+' if (ocur.get('trust') or 0)>=0 else ''}{ocur.get('trust', 0):,} 口"),
        card("自營商淨OI", f"{'+' if (ocur.get('dealer') or 0)>=0 else ''}{ocur.get('dealer', 0):,} 口"),
    ])

    return f'''<!DOCTYPE html>
<html lang="zh-Hant"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>台股資金儀表板</title>
<style>
:root{{--bg:#f7f6f3;--surface:#fff;--s2:#f1f0ec;--b:#e5e3dd;--tx:#1a1a1a;--tx2:#555;--tx3:#999;--r:14px}}
@media(prefers-color-scheme:dark){{:root{{--bg:#111;--surface:#1c1c1e;--s2:#2a2a2c;--b:#333;--tx:#eee;--tx2:#bbb;--tx3:#777}}}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--tx);font-family:-apple-system,"PingFang TC","Noto Sans TC",sans-serif;
  max-width:640px;margin:0 auto;padding:16px 14px 60px}}
h1{{font-size:19px;margin-bottom:2px}}
.sub{{font-size:11px;color:var(--tx3);margin-bottom:16px}}
.sec{{background:var(--surface);border:.5px solid var(--b);border-radius:var(--r);padding:16px;margin-bottom:14px}}
.st{{font-size:14px;font-weight:700;margin-bottom:12px}}
.gauge{{text-align:center;padding:6px 0 14px}}
.gv{{font-size:56px;font-weight:800;line-height:1}}
.gs{{font-size:17px;font-weight:700;margin-top:4px}}
.gbar{{height:8px;border-radius:99px;background:linear-gradient(90deg,#3b82f6,#22c55e,#eab308,#f97316,#ef4444);position:relative;margin:16px 8px 0}}
.gbar i{{position:absolute;top:-4px;width:3px;height:16px;background:var(--tx);border-radius:2px;transform:translateX(-50%)}}
.grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-bottom:12px}}
.card{{background:var(--s2);border-radius:10px;padding:10px;text-align:center}}
.cl{{font-size:10px;color:var(--tx3)}}
.cv{{font-size:17px;font-weight:800;margin:3px 0 1px}}
.cs{{font-size:10px;color:var(--tx3)}}
.legend{{display:flex;gap:12px;flex-wrap:wrap;font-size:10px;color:var(--tx3);margin-top:5px}}
.legend i{{display:inline-block;width:10px;height:3px;vertical-align:middle;margin-right:3px}}
.verdict{{background:var(--s2);border-left:3px solid #3b82f6;border-radius:10px;padding:12px;font-size:13px;line-height:1.75}}
.mrow{{display:grid;grid-template-columns:66px 1fr 52px;grid-template-rows:auto auto;align-items:center;gap:2px 8px;margin-bottom:10px}}
.mname{{font-size:13px;font-weight:700}}
.mbar{{position:relative;height:18px}}
.mbar i{{position:absolute;left:50%;top:0;bottom:0;width:1px;background:var(--b)}}
.mbar b{{position:absolute;top:2px;height:14px;opacity:.85}}
.mtag{{font-size:11px;font-weight:700;text-align:right}}
.msub{{grid-column:2/4;font-size:10px;color:var(--tx3)}}
.drow{{display:flex;gap:8px;align-items:baseline;padding:7px 0;border-bottom:.5px solid var(--b);font-size:13px;flex-wrap:wrap}}
.drow span{{font-size:11px;color:var(--tx3)}}
.dp{{margin-left:auto}}
.foot{{font-size:10px;color:var(--tx3);text-align:center;margin-top:20px;line-height:1.8}}
</style></head><body>
<h1>💧 台股資金儀表板</h1>
<div class="sub">更新：{NOW_STR}（台北時間）｜每交易日自動更新</div>

<div class="sec">
  <div class="st">融資水位</div>
  <div class="gauge"><div class="gv" style="color:{color}">{score}</div>
    <div class="gs" style="color:{color}">{stage}</div>
    <div class="gbar"><i style="left:{score}%"></i></div></div>
  <div class="grid">{cards1}</div>
  {chart1}
  <div class="legend"><span><i style="background:{color}"></i>融資餘額</span>
    <span><i style="background:#94a3b8"></i>加權指數</span>
    <span><i style="background:#8b5cf6"></i>維持率(估)</span></div>
</div>

<div class="sec">
  <div class="st">🌏 市場資金流</div>
  <div class="grid">{cards2}</div>
  {chart2}
  <div class="legend"><span>外資每日買賣超（近60日）</span></div>
  <div style="height:12px"></div>
  <div class="grid" style="grid-template-columns:repeat(3,1fr)">{cards3}</div>
  {chart3}
  <div class="legend"><span><i style="background:#ef4444"></i>外資</span>
    <span><i style="background:#3b82f6"></i>投信</span>
    <span><i style="background:#eab308"></i>自營商</span><span>台指期淨未平倉（近90日）</span></div>
  <div style="height:12px"></div>
  <div class="verdict">🧭 {verdict}</div>
</div>

<div class="sec">
  <div class="st">🌐 資金流向雷達（近20日，股匯雙率代理）</div>
  {radar_rows}
  <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--tx3)">
    <span>← 資金流出</span><span>資金流入 →</span></div>
</div>

<div class="sec">
  <div class="st">⚠️ 目前處置股（全市場 {len(disposal)} 檔）</div>
  {disp_rows}
</div>

<div class="foot">分數 = 水位百分位×40% + 20日增速×30% + 背離×30%｜維持率為估算值（擔保市值÷融資餘額）<br>
資料：證交所 / 櫃買 / 期交所 / Yahoo Finance｜僅供研究參考，非投資建議</div>
</body></html>'''


if __name__ == "__main__":
    main()
