#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
a-stock-data V3.8.0 客户端封装（2026-09-08 接入团队平台）

已接入端点（代码忠实取自 SKILL.md 对应章节，走 em_get 限流防封）：
  1.2  tencent_quote()         实时行情：价/涨跌幅/量比/换手/PE/PB/市值/涨跌停价
  1.3  baidu_kline_with_ma()   日K线自带 MA5/10/20
  3.8  board_fund_flow()       行业/概念/地域 × 今日/5日/10日 主力资金流向
  4.5  stock_fund_flow_120d()  个股资金流（日级，近120日）
  8.1  em_zt_pool 等四池       涨停/炸板/跌停/昨涨停
  8.3  limit_up_sentiment()    打板情绪：炸板率/连板高度/连板梯队
  3.5  dragon_tiger_board()    龙虎榜近30日 + 买卖席位 + 机构动向
  3.6  lockup_expiry()         历史解禁 + 未来90天待解禁
  4.1-4.4 margin/block/holder/dividend  两融/大宗/股东户数/分红
  4.6  chip_distribution()     筹码分布（获利比例/成本区间/集中度，本地推演）
  5.2  cls_telegraph()         财联社 7×24 电报（本地签名，零 key）
  2.1-2.2 eastmoney_reports / industry_reports / ths_eps_forecast
                                研报列表 + PDF + 行业研报 + 同花顺一致预期 EPS
  3.1/3.4/3.7/3.9 ths_hot_reason / fund_flow_minute / industry_comparison / daily_dragon_tiger
                                热点题材归因 / 分钟资金流 / 行业排名 / 全市场龙虎榜
  5.1/5.3 eastmoney_stock_news / eastmoney_global_news
                                个股新闻 / 全球资讯 7×24
  10.1-10.2 cninfo_irm / ths_hot_list / em_hot_rank / em_hot_concept
                                互动易问答 / 同花顺热榜 / 东财人气榜 / 概念命中

原则：腾讯/百度不封 IP 优先；东财接口统一走 em_get（间隔≥1s+抖动）。
"""
import random
import socket
import time
import urllib.request
import hashlib
import json
import re
import io
from datetime import date, datetime, timedelta
from contextlib import contextmanager
from typing import Optional

import requests

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# ── 东财防封：会话复用 + 串行节流 ──────────────────────────────
EM_SESSION = requests.Session()
EM_SESSION.headers.update({"User-Agent": UA})
try:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    _adapter = HTTPAdapter(max_retries=Retry(
        total=3, connect=3, backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"]))
    EM_SESSION.mount("https://", _adapter)
except Exception:
    pass

EM_MIN_INTERVAL = 1.2
_em_last = [0.0]


def em_get(url, params=None, headers=None, timeout=15, **kw):
    wait = EM_MIN_INTERVAL - (time.time() - _em_last[0])
    if wait > 0:
        time.sleep(wait + random.uniform(0.1, 0.4))
    try:
        return EM_SESSION.get(url, params=params, headers=headers,
                              timeout=timeout, **kw)
    finally:
        _em_last[0] = time.time()


def eastmoney_datacenter(report_name, columns="ALL", filter_str="", page_size=50,
                         sort_columns="", sort_types="-1"):
    """东财数据中心统一查询（龙虎榜/解禁/两融/大宗/股东户数/分红共用）。"""
    params = {"reportName": report_name, "columns": columns,
              "filter": filter_str, "pageNumber": "1", "pageSize": str(page_size),
              "sortColumns": sort_columns, "sortTypes": sort_types,
              "source": "WEB", "client": "WEB"}
    try:
        d = em_get("https://datacenter-web.eastmoney.com/api/data/v1/get",
                   params=params, timeout=15).json()
    except Exception:
        return []
    return (d.get("result") or {}).get("data") or []


# ── 市场前缀（个股 + 常用指数）─────────────────────────────────
SH_INDEX = {"000300", "000905", "000016", "000688", "000852", "000010"}


def get_prefix(code):
    low = str(code).lower()
    if low.startswith(("sh", "sz", "bj")):
        return low[:2]
    if code.startswith(("92", "4", "8")):
        return "bj"
    if code in SH_INDEX or code.startswith(("5", "6", "9")):
        return "sh"
    return "sz"


def em_market_code(code):
    """东财 secid 市场号：沪=1，深/北=0。"""
    return 1 if get_prefix(code) == "sh" else 0


def em_secid(code):
    return f"{em_market_code(code)}.{code}"


_TICKER_RE = re.compile(r"^(?:(sh|sz|bj)(\d{6})|(\d{6})(?:\.(sh|sz|bj))?)$",
                        re.IGNORECASE)


def _natural_market(digits):
    if digits.startswith(("4", "8", "92")):
        return "bj"
    if digits[0] in ("5", "6", "9"):
        return "sh"
    return "sz"


def norm_ticker(code, stock_only=False):
    raw = str(code).strip()
    m = _TICKER_RE.match(raw)
    if not m:
        raise ValueError(f"无法把 {code!r} 解析为 6 位股票代码")
    digits = m.group(2) or m.group(3)
    market = (m.group(1) or m.group(4) or "").lower()
    if market:
        if digits.startswith("000"):
            if market == "bj":
                raise ValueError(f"{code!r} 市场标识与号段矛盾")
            if stock_only and market == "sh":
                raise ValueError(f"{code!r} 指向沪市指数而非个股")
        else:
            nat = _natural_market(digits)
            if market != nat:
                raise ValueError(f"{code!r} 的市场标识与号段矛盾：{digits} 属 {nat} 市")
    return digits


# ── 1.2 腾讯财经实时行情 ──────────────────────────────────────
def tencent_quote(codes):
    """
    批量实时行情（含指数/ETF）。返回 {code: {name, price, pe_ttm, pb, ...}}。
    上证指数请显式传 "sh000001"，裸 "000001" = 平安银行（深市个股）。
    """
    keys, key_of = {}, {}
    for c in codes:
        low = c.lower()
        if low.startswith(("sh", "sz", "bj")):
            p = low
        elif c.startswith("92") or c.startswith(("4", "8")):
            p = f"bj{c}"
        elif c in SH_INDEX or c.startswith(("5", "6", "9")):
            p = f"sh{c}"
        else:
            p = f"sz{c}"
        keys[p] = c
        key_of[p] = c
    url = "https://qt.gtimg.cn/q=" + ",".join(keys)
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")
    data = urllib.request.urlopen(req, timeout=10).read().decode("gbk")
    out = {}
    for line in data.strip().split(";"):
        if not line.strip() or "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].split("_")[-1]
        vals = line.split('"')[1].split("~")
        if len(vals) < 53:
            continue
        code = key_of.get(key, key[2:])
        f = lambda i: float(vals[i]) if vals[i] else 0.0
        out[code] = {
            "name": vals[1], "price": f(3), "last_close": f(4), "open": f(5),
            "change_pct": f(32), "high": f(33), "low": f(34),
            "amount_wan": f(37), "turnover_pct": f(38), "pe_ttm": f(39),
            "amplitude_pct": f(43), "float_mcap_yi": f(44), "mcap_yi": f(45),
            "pb": f(46), "limit_up": f(47), "limit_down": f(48),
            "vol_ratio": f(49), "pe_static": f(52),
        }
        q = out[code]
        q["is_stale"] = q["amount_wan"] == 0 and q["price"] == q["last_close"] and q["price"] > 0
    return out


# ── 1.3 百度股市通 K线（自带 MA5/10/20）───────────────────────
def baidu_kline_with_ma(code, start_time=""):
    """日K线：字段含 time/open/close/high/low/volume/amount + ma5/10/20avgprice。"""
    params = {
        "all": "1", "isIndex": "false", "isBk": "false", "isBlock": "false",
        "isFutures": "false", "isStock": "true", "newFormat": "1",
        "group": "quotation_kline_ab", "finClientType": "pc",
        "code": code, "start_time": start_time, "ktype": "1",
    }
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/vnd.finance-web.v1+json",
        "Origin": "https://gushitong.baidu.com",
        "Referer": "https://gushitong.baidu.com/",
    }
    r = requests.get("https://finance.pae.baidu.com/selfselect/getstockquotation",
                     params=params, headers=headers, timeout=10)
    md = r.json().get("Result", {}).get("newMarketData", {})
    return {"keys": md.get("keys", []),
            "rows": [x.split(",") for x in md.get("marketData", "").split(";") if x]}


# ── 3.8 板块资金流向 ──────────────────────────────────────────
_BOARD_FS = {"industry": "m:90+t:2", "concept": "m:90+t:3", "region": "m:90+t:1"}
_BOARD_PERIOD = {
    "today": ("f62", "f62", "f184", "f3", "f204"),
    "5d":    ("f164", "f164", "f165", "f109", "f257"),
    "10d":   ("f174", "f174", "f175", "f160", None),
}


def board_fund_flow(board_type="industry", period="today", top_n=15):
    if board_type not in _BOARD_FS:
        raise ValueError(f"board_type 须为 {list(_BOARD_FS)}")
    if period not in _BOARD_PERIOD:
        raise ValueError(f"period 须为 {list(_BOARD_PERIOD)}")
    fid, f_main, f_pct, f_chg, f_leader = _BOARD_PERIOD[period]
    fields = ["f12", "f14", f_chg, f_main, f_pct]
    if f_leader:
        fields.append(f_leader)
    if period == "today":
        fields += ["f66", "f72", "f78", "f84"]
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    base = {"pz": "200", "po": "1", "np": "1", "fltt": "2", "invt": "2",
            "fid": fid, "fs": _BOARD_FS[board_type],
            "fields": ",".join(dict.fromkeys(fields))}

    def _page(pn):
        for attempt in range(3):
            try:
                r = em_get(url, params={**base, "pn": str(pn)},
                           headers={"User-Agent": UA}, timeout=15)
                d = r.json().get("data") or {}
                return (d.get("diff") or []), int(d.get("total") or 0)
            except Exception:
                time.sleep(1.5 * (attempt + 1))
        return [], 0

    items, total = _page(1)
    pn = 2
    while len(items) < top_n:
        if total and len(items) >= total:
            break
        more, _ = _page(pn)
        if not more:
            break
        items += more
        pn += 1
        if len(more) < 200:
            break
    rows = []
    for i, it in enumerate(items[:top_n]):
        row = {"rank": i + 1, "name": it.get("f14", ""), "code": it.get("f12", ""),
               "change_pct": it.get(f_chg, 0), "main_net": it.get(f_main, 0),
               "main_pct": it.get(f_pct, 0),
               "leader": it.get(f_leader, "") if f_leader else ""}
        if period == "today":
            row.update({"super_large_net": it.get("f66", 0), "large_net": it.get("f72", 0),
                        "medium_net": it.get("f78", 0), "small_net": it.get("f84", 0)})
        rows.append(row)
    return {"board_type": board_type, "period": period, "total": total, "rows": rows}


# ── 4.5 个股资金流（近120日，日级）────────────────────────────
def stock_fund_flow_120d(code):
    url = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    params = {"secid": em_secid(code), "fields1": "f1,f2,f3,f7",
              "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
              "lmt": "120"}
    headers = {"User-Agent": UA, "Referer": "https://quote.eastmoney.com/",
               "Origin": "https://quote.eastmoney.com"}
    rows = []
    for attempt in range(2):
        try:
            d = em_get(url, params=params, headers=headers, timeout=15).json()
            klines = (d.get("data") or {}).get("klines") or []
            for line in klines:
                p = line.split(",")
                if len(p) >= 7:
                    f = lambda x: float(x) if x != "-" else 0.0
                    rows.append({"date": p[0], "main_net": f(p[1]), "small_net": f(p[2]),
                                 "mid_net": f(p[3]), "large_net": f(p[4]),
                                 "super_net": f(p[5])})
            if rows:
                break
        except Exception:
            pass
        time.sleep(2.0)
    return rows


def fund_flow_backup(code, days=60):
    """个股资金流备用源（新浪日度，东财 push2his 被 IP 风控时降级）。"""
    pre = ("bj" if code.startswith(("92", "8"))
           else "sh" if code.startswith(("6", "9")) else "sz") + code
    u = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
         f"MoneyFlow.ssl_qsfx_zjlrqs?page=1&num={days}&sort=opendate&asc=0&daima={pre}")
    req = urllib.request.Request(u, headers={"User-Agent": UA,
                                             "Referer": "https://finance.sina.com.cn/"})
    try:
        t = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "ignore")
        arr = json.loads(t[t.index("["):t.rindex("]") + 1])
    except Exception:
        return []
    out = []
    for x in arr:
        try:
            out.append({"date": x.get("opendate"), "close": x.get("trade"),
                        "net_amount": float(x.get("netamount") or 0),
                        "turnover": x.get("turnover")})
        except Exception:
            continue
    return out


# ── 8.1 东财涨停/炸板/跌停/昨涨停 四池 ────────────────────────
ZTB_UT = "7eea3edcaed734bea9cbfc24409ed989"


def _fmt_zt_time(t):
    s = str(t).zfill(6)
    return f"{s[0:2]}:{s[2:4]}:{s[4:6]}"


def _em_zt_api(endpoint, sort, date):
    url = f"https://push2ex.eastmoney.com/{endpoint}"
    params = {"ut": ZTB_UT, "dpt": "wz.ztzt", "Pageindex": 0, "pagesize": 10000,
              "sort": sort, "date": date}
    headers = {"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"}
    try:
        return (em_get(url, params=params, headers=headers, timeout=10)
                .json().get("data") or {}).get("pool") or []
    except Exception:
        return []


def em_zt_pool(date):
    out = []
    for p in _em_zt_api("getTopicZTPool", "fbt:asc", date):
        out.append({"code": p["c"], "name": p["n"], "price": p["p"] / 1000,
                    "pct": round(p["zdp"], 2), "amount": p["amount"],
                    "float_cap": p["ltsz"], "turnover": round(p["hs"], 2),
                    "limit_days": p["lbc"], "first_seal": _fmt_zt_time(p["fbt"]),
                    "last_seal": _fmt_zt_time(p["lbt"]), "seal_fund": p["fund"],
                    "break_times": p["zbc"], "industry": p.get("hybk", ""),
                    "zt_stat": f'{(p.get("zttj") or {}).get("days", "?")}天'
                               f'{(p.get("zttj") or {}).get("ct", "?")}板'})
    return out


def em_zb_pool(date):
    out = []
    for p in _em_zt_api("getTopicZBPool", "fbt:asc", date):
        out.append({"code": p["c"], "name": p["n"], "price": p["p"] / 1000,
                    "limit_price": p["ztp"] / 1000, "pct": round(p["zdp"], 2),
                    "turnover": round(p["hs"], 2), "first_seal": _fmt_zt_time(p["fbt"]),
                    "break_times": p["zbc"], "amplitude": round(p["zf"], 2),
                    "speed": round(p["zs"], 2), "industry": p.get("hybk", ""),
                    "zt_stat": f'{(p.get("zttj") or {}).get("days", "?")}天'
                               f'{(p.get("zttj") or {}).get("ct", "?")}板'})
    return out


def em_dt_pool(date):
    out = []
    for p in _em_zt_api("getTopicDTPool", "fund:asc", date):
        out.append({"code": p["c"], "name": p["n"], "price": p["p"] / 1000,
                    "pct": round(p["zdp"], 2), "turnover": round(p["hs"], 2),
                    "pe": p.get("pe"), "seal_fund": p["fund"],
                    "last_seal": _fmt_zt_time(p["lbt"]), "board_amount": p.get("fba"),
                    "dt_days": p.get("days"), "open_times": p.get("oc"),
                    "industry": p.get("hybk", "")})
    return out


def em_yzt_pool(date):
    out = []
    for p in _em_zt_api("getYesterdayZTPool", "zs:desc", date):
        out.append({"code": p["c"], "name": p["n"], "price": p["p"] / 1000,
                    "pct": round(p["zdp"], 2), "turnover": round(p["hs"], 2),
                    "amplitude": round(p["zf"], 2), "speed": round(p["zs"], 2),
                    "y_first_seal": _fmt_zt_time(p["yfbt"]), "y_limit_days": p["ylbc"],
                    "industry": p.get("hybk", ""),
                    "zt_stat": f'{(p.get("zttj") or {}).get("days", "?")}天'
                               f'{(p.get("zttj") or {}).get("ct", "?")}板'})
    return out


def limit_up_sentiment(date):
    zt, zb, dt = em_zt_pool(date), em_zb_pool(date), em_dt_pool(date)
    ladder = {}
    for s in zt:
        ladder[s["limit_days"]] = ladder.get(s["limit_days"], 0) + 1
    zt_n, zb_n = len(zt), len(zb)
    return {"date": date, "zt_count": zt_n, "zb_count": zb_n, "dt_count": len(dt),
            "break_rate": round(zb_n / (zt_n + zb_n) * 100, 1) if (zt_n + zb_n) else 0,
            "max_height": max((s["limit_days"] for s in zt), default=0),
            "ladder": dict(sorted(ladder.items()))}


# ── 3.5 龙虎榜席位 ────────────────────────────────────────────
def dragon_tiger_board(code, trade_date, look_back=30):
    start = (datetime.strptime(trade_date, "%Y-%m-%d")
             - timedelta(days=look_back)).strftime("%Y-%m-%d")
    records = []
    for row in eastmoney_datacenter(
            "RPT_DAILYBILLBOARD_DETAILSNEW",
            filter_str=f"(TRADE_DATE>='{start}')(TRADE_DATE<='{trade_date}')(SECURITY_CODE=\"{code}\")",
            page_size=50, sort_columns="TRADE_DATE", sort_types="-1"):
        records.append({"date": str(row.get("TRADE_DATE", ""))[:10],
                        "reason": row.get("EXPLANATION", ""),
                        "net_buy": round((row.get("BILLBOARD_NET_AMT") or 0) / 10000, 1),
                        "turnover": round(float(row.get("TURNOVERRATE") or 0), 2)})
    buy_data, sell_data = [], []
    seats = {"buy": [], "sell": []}
    if records:
        latest = records[0]["date"]
        buy_data = eastmoney_datacenter(
            "RPT_BILLBOARD_DAILYDETAILSBUY",
            filter_str=f"(TRADE_DATE='{latest}')(SECURITY_CODE=\"{code}\")",
            page_size=10, sort_columns="BUY", sort_types="-1")
        sell_data = eastmoney_datacenter(
            "RPT_BILLBOARD_DAILYDETAILSSELL",
            filter_str=f"(TRADE_DATE='{latest}')(SECURITY_CODE=\"{code}\")",
            page_size=10, sort_columns="SELL", sort_types="-1")
        for row in buy_data[:5]:
            seats["buy"].append({"name": row.get("OPERATEDEPT_NAME", ""),
                                 "buy_amt": round((row.get("BUY") or 0) / 10000, 1),
                                 "sell_amt": round((row.get("SELL") or 0) / 10000, 1),
                                 "net": round((row.get("NET") or 0) / 10000, 1)})
        for row in sell_data[:5]:
            seats["sell"].append({"name": row.get("OPERATEDEPT_NAME", ""),
                                  "buy_amt": round((row.get("BUY") or 0) / 10000, 1),
                                  "sell_amt": round((row.get("SELL") or 0) / 10000, 1),
                                  "net": round((row.get("NET") or 0) / 10000, 1)})
    inst = {"buy_amt": 0.0, "sell_amt": 0.0, "net_amt": 0.0}
    for detail, side in [(buy_data, "buy"), (sell_data, "sell")]:
        for row in detail:
            if str(row.get("OPERATEDEPT_CODE", "")) == "0":
                amt = (row.get("BUY") or 0) if side == "buy" else (row.get("SELL") or 0)
                inst["buy_amt" if side == "buy" else "sell_amt"] += amt
    inst["buy_amt"] = round(inst["buy_amt"] / 10000, 1)
    inst["sell_amt"] = round(inst["sell_amt"] / 10000, 1)
    inst["net_amt"] = round(inst["buy_amt"] - inst["sell_amt"], 1)
    return {"records": records, "seats": seats, "institution": inst}


# ── 3.6 限售解禁 ──────────────────────────────────────────────
def lockup_expiry(code, trade_date, forward_days=90):
    end = (datetime.strptime(trade_date, "%Y-%m-%d")
           + timedelta(days=forward_days)).strftime("%Y-%m-%d")

    def _rows(filter_str, sort="-1", size=20):
        out = []
        for row in eastmoney_datacenter(
                "RPT_LIFT_STAGE", filter_str=filter_str, page_size=size,
                sort_columns="FREE_DATE", sort_types=sort):
            out.append({"date": str(row.get("FREE_DATE", ""))[:10],
                        "type": row.get("FREE_SHARES_TYPE", ""),
                        "shares": row.get("FREE_SHARES", 0),
                        "able_shares": row.get("ABLE_FREE_SHARES", 0),
                        "ratio": row.get("FREE_RATIO", 0)})
        return out

    history = _rows(f'(SECURITY_CODE="{code}")', sort="-1", size=15)
    upcoming = _rows(f'(SECURITY_CODE="{code}")(FREE_DATE>=\'{trade_date}\')'
                     f'(FREE_DATE<=\'{end}\')', sort="1", size=20)
    return {"history": history, "upcoming": upcoming}


# ── 4.1 融资融券 ──────────────────────────────────────────────
def margin_trading(code, page_size=30):
    rows = []
    for row in eastmoney_datacenter(
            "RPTA_WEB_RZRQ_GGMX", filter_str=f'(SCODE="{code}")',
            page_size=page_size, sort_columns="DATE", sort_types="-1"):
        rows.append({"date": str(row.get("DATE", ""))[:10],
                     "rzye": row.get("RZYE", 0), "rzmre": row.get("RZMRE", 0),
                     "rzche": row.get("RZCHE", 0), "rqye": row.get("RQYE", 0),
                     "rqmcl": row.get("RQMCL", 0), "rqchl": row.get("RQCHL", 0),
                     "rzrqye": row.get("RZRQYE", 0)})
    return rows


# ── 4.2 大宗交易 ──────────────────────────────────────────────
def block_trade(code, page_size=20):
    rows = []
    for row in eastmoney_datacenter(
            "RPT_DATA_BLOCKTRADE", filter_str=f'(SECURITY_CODE="{code}")',
            page_size=page_size, sort_columns="TRADE_DATE", sort_types="-1"):
        close, deal = row.get("CLOSE_PRICE") or 0, row.get("DEAL_PRICE") or 0
        premium = ((deal / close - 1) * 100) if close else 0
        rows.append({"date": str(row.get("TRADE_DATE", ""))[:10], "price": deal,
                     "close": close, "premium_pct": round(premium, 2),
                     "vol": row.get("DEAL_VOLUME", 0), "amount": row.get("DEAL_AMT", 0),
                     "buyer": row.get("BUYER_NAME", ""), "seller": row.get("SELLER_NAME", "")})
    return rows


# ── 4.3 股东户数 ──────────────────────────────────────────────
def holder_num_change(code, page_size=10):
    rows = []
    for row in eastmoney_datacenter(
            "RPT_HOLDERNUMLATEST", filter_str=f'(SECURITY_CODE="{code}")',
            page_size=page_size, sort_columns="END_DATE", sort_types="-1"):
        rows.append({"date": str(row.get("END_DATE", ""))[:10],
                     "holder_num": row.get("HOLDER_NUM", 0),
                     "change_num": row.get("HOLDER_NUM_CHANGE", 0),
                     "change_ratio": row.get("HOLDER_NUM_RATIO", 0),
                     "avg_shares": row.get("AVG_FREE_SHARES", 0)})
    return rows


# ── 4.4 分红送转历史 ──────────────────────────────────────────
def dividend_history(code, page_size=20):
    rows = []
    for row in eastmoney_datacenter(
            "RPT_SHAREBONUS_DET", filter_str=f'(SECURITY_CODE="{code}")',
            page_size=page_size, sort_columns="EX_DIVIDEND_DATE", sort_types="-1"):
        rows.append({"date": str(row.get("EX_DIVIDEND_DATE", ""))[:10],
                     "bonus_rmb": row.get("PRETAX_BONUS_RMB", 0),
                     "transfer_ratio": row.get("TRANSFER_RATIO", 0),
                     "bonus_ratio": row.get("BONUS_RATIO", 0),
                     "plan": row.get("ASSIGN_PROGRESS", "")})
    return rows


# ── 4.6 筹码分布（本地推演，需 pandas/numpy）──────────────────
def chip_distribution(rows, grid_size=300, decay=1.0):
    """rows: [{date,high,low,close,turn(百分数)}] 时间升序；前复权口径最佳。"""
    try:
        import numpy as np
        import pandas as pd
    except Exception as exc:
        raise RuntimeError(f"chip_distribution 需要 pandas/numpy: {exc}")
    df = pd.DataFrame(rows)
    df = df.dropna(subset=["high", "low", "close", "turn"]).copy()
    df = df[df["high"] > 0].sort_values("date").reset_index(drop=True)
    if df.empty:
        raise ValueError("chip_distribution: 无有效行")
    lo, hi = float(df["low"].min()), float(df["high"].max())
    pad = (hi - lo) * 0.02 or max(lo * 0.02, 0.01)
    grid = np.linspace(lo - pad, hi + pad, grid_size)

    def _tri(low, high, avg):
        w = np.zeros_like(grid)
        if not np.isfinite([low, high, avg]).all() or high < low:
            return w
        if high - low < 1e-9:
            w[np.argmin(np.abs(grid - low))] = 1.0
            return w
        avg = min(max(avg, low), high)
        left = (grid >= low) & (grid <= avg)
        right = (grid > avg) & (grid <= high)
        w[left] = (grid[left] - low) / (avg - low) if avg - low > 1e-9 else 1.0
        w[right] = (high - grid[right]) / (high - avg) if high - avg > 1e-9 else 1.0
        total = w.sum()
        if total > 0:
            return w / total
        w[np.argmin(np.abs(grid - avg))] = 1.0
        return w

    chips = None
    for row in df.itertuples(index=False):
        t = min(max(float(row.turn) / 100.0 * decay, 0.0), 1.0)
        avg = (float(row.high) + float(row.low) + float(row.close)) / 3.0
        w = _tri(float(row.low), float(row.high), avg)
        if w.sum() <= 0:
            continue
        chips = w.copy() if chips is None else chips * (1 - t) + w * t
    if chips is None or chips.sum() <= 0:
        raise RuntimeError("chip_distribution: 无法构建分布")
    chips = chips / chips.sum()
    price = float(df["close"].iloc[-1])
    cum = np.cumsum(chips)
    p_at = lambda q: float(np.interp(q, cum, grid))
    p05, p15, p85, p95 = p_at(0.05), p_at(0.15), p_at(0.85), p_at(0.95)
    peak = int(np.argmax(chips))
    return {"price": price,
            "profit_ratio": float(chips[grid <= price].sum()),
            "avg_cost": float((grid * chips).sum()),
            "cost_90": (p05, p95), "cost_70": (p15, p85),
            "concentration_90": float((p95 - p05) / (p95 + p05)) if p95 + p05 else None,
            "concentration_70": float((p85 - p15) / (p85 + p15)) if p85 + p15 else None,
            "peak_price": float(grid[peak])}


# ── 5.2 财联社电报 ────────────────────────────────────────────
def cls_telegraph(page_size=50):
    params = {"appName": "CailianpressWeb", "os": "web", "sv": "7.7.5",
              "last_time": "", "refresh_type": "1", "rn": str(page_size)}
    qs = "&".join(f"{k}={params[k]}" for k in sorted(params))
    sign = hashlib.md5(hashlib.sha1(qs.encode()).hexdigest().encode()).hexdigest()
    url = f"https://www.cls.cn/v1/roll/get_roll_list?{qs}&sign={sign}"
    headers = {"User-Agent": UA, "Referer": "https://www.cls.cn/"}
    try:
        d = requests.get(url, headers=headers, timeout=10).json()
    except Exception:
        return []
    rows = []
    for item in (d.get("data") or {}).get("roll_data") or []:
        ts = item.get("ctime")
        t = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else ""
        rows.append({"title": item.get("title", "") or item.get("brief", ""),
                     "content": item.get("content", "") or item.get("brief", ""),
                     "time": t})
    return rows


# ── 2.1 东财研报（个股 + 行业 + PDF）──────────────────────────
REPORT_API = "https://reportapi.eastmoney.com/report/list"
PDF_TPL = "https://pdf.dfcfw.com/pdf/H3_{info_code}_1.pdf"


def eastmoney_reports(code, max_pages=5):
    code = norm_ticker(code, stock_only=True)
    all_records = []
    for page in range(1, max_pages + 1):
        params = {"industryCode": "*", "pageSize": "100", "industry": "*",
                  "rating": "*", "ratingChange": "*",
                  "beginTime": "2000-01-01", "endTime": "2030-01-01",
                  "pageNo": str(page), "fields": "", "qType": "0",
                  "orgCode": "", "code": code, "rcode": "",
                  "p": str(page), "pageNum": str(page), "pageNumber": str(page)}
        try:
            r = em_get(REPORT_API, params=params,
                       headers={"Referer": "https://data.eastmoney.com/"}, timeout=30)
            d = r.json()
        except Exception:
            break
        rows = d.get("data") or []
        if not rows:
            break
        all_records.extend(rows)
        if page >= (d.get("TotalPage", 1) or 1):
            break
    if not all_records and code[:2] in ("43", "83", "87"):
        raise ValueError(f"{code} 属北交所老号段，请反查 920 代码")
    return all_records


def eastmoney_industry_reports(industry_code="*", max_pages=5, begin=""):
    if not begin:
        begin = (date.today() - timedelta(days=730)).isoformat()
    all_records = []
    for page in range(1, max_pages + 1):
        params = {"industryCode": industry_code, "pageSize": "100", "industry": "*",
                  "rating": "*", "ratingChange": "*", "beginTime": begin,
                  "endTime": "2030-01-01", "pageNo": str(page), "fields": "",
                  "qType": "1"}
        try:
            r = em_get(REPORT_API, params=params,
                       headers={"Referer": "https://data.eastmoney.com/"}, timeout=30)
            d = r.json()
        except Exception:
            break
        rows = d.get("data") or []
        if not rows:
            break
        all_records.extend(rows)
        if page >= (d.get("TotalPage", 1) or 1):
            break
    return all_records


def download_pdf(record, target_dir="./reports"):
    import os
    from pathlib import Path
    info_code = record.get("infoCode", "")
    if not info_code:
        return None
    d = (record.get("publishDate") or "")[:10]
    org = re.sub(r'[\\/:*?"<>|]', "_", record.get("orgSName") or "未知")[:40]
    title = re.sub(r'[\\/:*?"<>|]', "_", record.get("title", ""))[:80]
    target = Path(target_dir) / f"{d}_{org}_{title}.pdf"
    if target.exists():
        return str(target)
    try:
        r = em_get(PDF_TPL.format(info_code=info_code),
                   headers={"Referer": "https://data.eastmoney.com/"}, timeout=60)
        if r.status_code == 200 and len(r.content) >= 1024:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(r.content)
            return str(target)
    except Exception:
        pass
    return None


# ── 2.2 同花顺一致预期 EPS ────────────────────────────────────
def ths_eps_forecast(code):
    code = norm_ticker(code, stock_only=True)
    url = f"https://basic.10jqka.com.cn/new/{code}/worth.html"
    headers = {"User-Agent": UA, "Referer": "https://basic.10jqka.com.cn/"}
    try:
        import pandas as pd
        r = requests.get(url, headers=headers, timeout=15)
        r.encoding = "gbk"
        dfs = pd.read_html(io.StringIO(r.text))
        for df in dfs:
            cols = [str(c) for c in df.columns]
            if any("每股收益" in c or "均值" in c for c in cols):
                return df
        return dfs[0] if dfs else pd.DataFrame()
    except Exception:
        return None


# ── 3.1 同花顺热点题材归因 ────────────────────────────────────
def ths_hot_reason(date_str=None):
    if date_str is None:
        date_str = date.today().strftime("%Y-%m-%d")
    url = (f"http://zx.10jqka.com.cn/event/api/getharden/"
           f"date/{date_str}/orderby/date/orderway/desc/charset/GBK/")
    headers = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "Chrome/117.0.0.0 Safari/537.36")}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        data = r.json()
        rows = data.get("data") or []
    except Exception:
        return []
    return [{"code": x.get("code"), "name": x.get("name"),
             "reason": x.get("reason", ""), "zhangfu": x.get("zhangfu"),
             "huanshou": x.get("huanshou"), "chengjiaoe": x.get("chengjiaoe")}
            for x in rows]


# ── 3.4 东财分钟级资金流 ──────────────────────────────────────
def eastmoney_fund_flow_minute(code):
    url = "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
    params = {"secid": em_secid(code), "klt": 1, "fields1": "f1,f2,f3,f7",
              "fields2": "f51,f52,f53,f54,f55,f56,f57"}
    headers = {"User-Agent": UA, "Referer": "https://quote.eastmoney.com/",
               "Origin": "https://quote.eastmoney.com"}
    try:
        d = em_get(url, params=params, headers=headers, timeout=10).json()
    except Exception:
        return []
    rows = []
    for line in ((d.get("data") or {}).get("klines") or []):
        p = line.split(",")
        if len(p) >= 6:
            rows.append({"time": p[0], "main_net": float(p[1]), "small_net": float(p[2]),
                         "mid_net": float(p[3]), "large_net": float(p[4]),
                         "super_net": float(p[5])})
    return rows


# ── 3.7 行业板块涨跌排名 ──────────────────────────────────────
def industry_comparison(top_n=20):
    params = {"pn": "1", "pz": "100", "po": "1", "np": "1", "fltt": "2",
              "invt": "2", "fid": "f3", "fs": "m:90+t:2",
              "fields": "f2,f3,f4,f12,f13,f14,f104,f105,f128,f136,f140,f141,f207"}
    try:
        d = em_get("https://push2.eastmoney.com/api/qt/clist/get",
                   params=params, headers={"User-Agent": UA}, timeout=15).json()
        items = (d.get("data") or {}).get("diff") or []
    except Exception:
        return {"top": [], "bottom": [], "total": 0}
    if isinstance(items, dict):
        items = list(items.values())
    rows = [{"rank": i + 1, "name": it.get("f14", ""), "change_pct": it.get("f3", 0),
             "code": it.get("f12", ""), "up_count": it.get("f104", 0),
             "down_count": it.get("f105", 0), "leader": it.get("f140", ""),
             "leader_change": it.get("f136", 0)}
            for i, it in enumerate(items)]
    return {"top": rows[:top_n], "bottom": rows[-top_n:], "total": len(rows)}


# ── 3.9 全市场龙虎榜 ──────────────────────────────────────────
def daily_dragon_tiger(trade_date=None, min_net_buy=None):
    if trade_date is None:
        trade_date = datetime.now().strftime("%Y-%m-%d")
    data = eastmoney_datacenter(
        "RPT_DAILYBILLBOARD_DETAILSNEW",
        filter_str=f"(TRADE_DATE>='{trade_date}')(TRADE_DATE<='{trade_date}')",
        page_size=500, sort_columns="BILLBOARD_NET_AMT", sort_types="-1")
    if not data:
        return {"date": trade_date, "total_records": 0, "stocks": [],
                "note": "无数据（非交易日或盘后未更新）"}
    actual = str(data[0].get("TRADE_DATE", ""))[:10]
    stocks = []
    for row in data:
        net = (row.get("BILLBOARD_NET_AMT") or 0) / 10000
        if min_net_buy is not None and net < min_net_buy:
            continue
        stocks.append({"code": row.get("SECURITY_CODE", ""),
                       "name": row.get("SECURITY_NAME_ABBR", ""),
                       "reason": row.get("EXPLANATION", ""),
                       "close": row.get("CLOSE_PRICE") or 0,
                       "change_pct": round(float(row.get("CHANGE_RATE") or 0), 2),
                       "net_buy_wan": round(net, 1),
                       "buy_wan": round((row.get("BILLBOARD_BUY_AMT") or 0) / 10000, 1),
                       "sell_wan": round((row.get("BILLBOARD_SELL_AMT") or 0) / 10000, 1),
                       "turnover_pct": round(float(row.get("TURNOVERRATE") or 0), 2)})
    return {"date": actual, "total_records": len(stocks), "stocks": stocks}


# ── 5.1 东财个股新闻 ──────────────────────────────────────────
def eastmoney_stock_news(code, page_size=20):
    cb = "jQuery_news"
    url = "https://search-api-web.eastmoney.com/search/jsonp"
    inner = json.dumps({"uid": "", "keyword": code, "type": ["cmsArticleWebOld"],
                        "client": "web", "clientType": "web", "clientVersion": "curr",
                        "param": {"cmsArticleWebOld": {"searchScope": "default",
                                 "sort": "default", "pageIndex": 1, "pageSize": page_size,
                                 "preTag": "", "postTag": ""}}}, separators=(",", ":"))
    try:
        r = em_get(url, params={"cb": cb, "param": inner},
                   headers={"User-Agent": UA, "Referer": "https://so.eastmoney.com/"}, timeout=15)
        text = r.text
        d = json.loads(text[text.index("(") + 1:text.rindex(")")])
    except Exception:
        return []
    rows = []
    for a in (d.get("result", {}).get("cmsArticleWebOld", []) or []):
        rows.append({"title": re.sub(r"<[^>]+>", "", a.get("title", "")),
                     "content": re.sub(r"<[^>]+>", "", a.get("content", ""))[:200],
                     "time": a.get("date", ""), "source": a.get("mediaName", ""),
                     "url": a.get("url", "")})
    return rows


# ── 5.3 东财全球资讯 ──────────────────────────────────────────
def eastmoney_global_news(page_size=50):
    import uuid
    params = {"client": "web", "biz": "web_724", "fastColumn": "102",
              "sortEnd": "", "pageSize": str(page_size),
              "req_trace": str(uuid.uuid4())}
    headers = {"User-Agent": UA, "Referer": "https://kuaixun.eastmoney.com/"}
    try:
        d = em_get("https://np-weblist.eastmoney.com/comm/web/getFastNewsList",
                   params=params, headers=headers, timeout=10).json()
    except Exception:
        return []
    rows = []
    for item in (d.get("data") or {}).get("fastNewsList") or []:
        rows.append({"title": item.get("title", ""),
                     "summary": item.get("summary", "")[:200],
                     "time": item.get("showTime", "")})
    return rows


# ── 10.1 互动易问答 ───────────────────────────────────────────
def cninfo_irm(code, page_size=20):
    try:
        r1 = requests.post("https://irm.cninfo.com.cn/newircs/index/queryKeyboardInfo",
                           data={"keyWord": code}, headers={"User-Agent": UA}, timeout=10)
        d1 = r1.json().get("data") or []
        if not d1:
            return []
        org_id = d1[0].get("secid")
        params = {"_t": 1, "stockcode": code, "orgId": org_id,
                  "pageSize": page_size, "pageNum": 1, "keyWord": "",
                  "startDay": "", "endDay": ""}
        r2 = requests.post("https://irm.cninfo.com.cn/newircs/company/question",
                           params=params, headers={"User-Agent": UA}, timeout=10)
        rows = r2.json().get("rows") or []
    except Exception:
        return []
    out = []
    for it in rows:
        pd_ms = it.get("pubDate")
        out.append({"code": it.get("stockCode"), "company": it.get("companyShortName"),
                    "question": it.get("mainContent"), "answer": it.get("attachedContent"),
                    "answerer": it.get("attachedAuthor"),
                    "ask_time": (datetime.fromtimestamp(pd_ms / 1000)
                                 .strftime("%Y-%m-%d %H:%M") if pd_ms else "")})
    return out


# ── 10.2 热榜 / 人气榜 / 概念命中 ─────────────────────────────
_EM_HOT_BODY = {"appId": "appId01", "globalId": "786e4c21-70dc-435a-93bb-38"}


def ths_hot_list(period="hour"):
    try:
        r = requests.get("https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock",
                         params={"stock_type": "a", "type": period, "list_type": "normal"},
                         headers={"User-Agent": UA}, timeout=10)
        lst = (r.json().get("data") or {}).get("stock_list") or []
    except Exception:
        return []
    out = []
    for it in lst:
        tag = it.get("tag") or {}
        out.append({"rank": it.get("order"), "code": it.get("code"),
                    "name": it.get("name"), "heat": it.get("rate"),
                    "pct": it.get("rise_and_fall"), "rank_chg": it.get("hot_rank_chg"),
                    "concepts": tag.get("concept_tag") or [],
                    "tag": tag.get("popularity_tag", "")})
    return out


def em_hot_rank(top=50):
    try:
        r = requests.post("https://emappdata.eastmoney.com/stockrank/getAllCurrentList",
                          json={**_EM_HOT_BODY, "marketType": "", "pageNo": 1,
                                "pageSize": top},
                          headers={"User-Agent": UA}, timeout=10)
        data = r.json().get("data") or []
        secids = [("0." if it["sc"].startswith("SZ") else "1.") + it["sc"][2:]
                  for it in data]
        u = requests.get("https://push2.eastmoney.com/api/qt/ulist.np/get",
                         params={"ut": "f057cbcbce2a86e2866ab8877db1d059", "fltt": 2,
                                 "invt": 2, "fields": "f14,f3,f12,f2",
                                 "secids": ",".join(secids)},
                         headers={"User-Agent": UA,
                                  "Referer": "https://quote.eastmoney.com/"}, timeout=10)
        diff = (u.json().get("data") or {}).get("diff") or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        nm = {x["f12"]: (x.get("f14"), x.get("f2"), x.get("f3")) for x in diff}
    except Exception:
        return []
    out = []
    for it in data:
        code = it["sc"][2:]
        name, price, pct = nm.get(code, ("", None, None))
        out.append({"rank": it["rk"], "code": code, "name": name,
                    "price": price, "pct": pct, "rank_chg": it.get("hisRc")})
    return out


def em_hot_concept(code):
    prefix = get_prefix(code).upper()
    try:
        r = requests.post("https://emappdata.eastmoney.com/stockrank/getHotStockRankList",
                          json={**_EM_HOT_BODY, "srcSecurityCode": prefix + code},
                          headers={"User-Agent": UA}, timeout=10)
        data = r.json().get("data") or []
    except Exception:
        return []
    return [{"concept": x.get("conceptName"), "bk": x.get("conceptId"),
             "hit": x.get("hitCount")} for x in data]


# ── 1.1 mootdx（通达信 TCP）客户端 ────────────────────────────
_TDX_SERVERS = [
    ("119.97.185.59", 7709), ("124.70.133.119", 7709), ("116.205.183.150", 7709),
    ("123.60.73.44", 7709), ("116.205.163.254", 7709), ("121.36.225.169", 7709),
    ("123.60.70.228", 7709), ("124.71.9.153", 7709), ("110.41.147.114", 7709),
    ("124.71.187.122", 7709),
]


def _tdx_probe(ip, port, timeout=2.0):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False


def _tdx_validate(client, market="std"):
    if market != "std":
        return True
    try:
        df = client.bars(symbol="000001", frequency=9, offset=1)
        return df is not None and not df.empty
    except Exception:
        return False


def tdx_client(market="std"):
    """mootdx 客户端：规避 0.11.x BESTIP 空串 bug，逐台真实取数验活。"""
    from mootdx.quotes import Quotes
    for ip, port in _TDX_SERVERS:
        if not _tdx_probe(ip, port):
            continue
        try:
            c = Quotes.factory(market=market, server=(ip, port))
            if _tdx_validate(c, market):
                return c
        except Exception:
            continue
    for kwargs in ({"bestip": True}, {}):
        try:
            c = Quotes.factory(market=market, **kwargs)
            if _tdx_validate(c, market):
                return c
        except Exception:
            continue
    raise RuntimeError("所有 mootdx 服务器均无法取数（TCP 可达但空返回/被 reset）")


def tdx_daily(code, offset=250):
    """通达信日K（不复权）。code: 6位。返回 DataFrame。"""
    client = tdx_client()
    return client.bars(symbol=code, frequency=9, offset=offset)


# ── 6.5/6.6 baostock 估值历史与标的信息 ───────────────────────
@contextmanager
def bs_session():
    import baostock as bs
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"baostock 登录失败: {lg.error_code} {lg.error_msg}")
    try:
        yield bs
    finally:
        bs.logout()


def _bs_rs_to_df(rs):
    import pandas as pd
    if rs.error_code != "0":
        raise RuntimeError(f"baostock 查询失败: {rs.error_code} {rs.error_msg}")
    rows = []
    while rs.next():
        rows.append(rs.get_row_data())
    return pd.DataFrame(rows, columns=rs.fields)


def _bs_code(code):
    code = str(code).zfill(6)
    if code[:2] in ("60", "68", "90"):
        return f"sh.{code}"
    if code[:2] in ("00", "30", "20"):
        return f"sz.{code}"
    raise ValueError(f"baostock 不支持该代码: {code}（北交所请用腾讯当日估值快照）")


def baostock_valuation_history(code, start_date, end_date):
    """日频 PE/PB/PS/PCF + 换手 + 停牌 + ST（baostock，不支持北交所）。"""
    import pandas as pd
    bs_code = _bs_code(code)
    with bs_session() as bs:
        rs = bs.query_history_k_data_plus(
            bs_code, "date,code,close,peTTM,pbMRQ,psTTM,pcfNcfTTM,turn,tradestatus,isST",
            start_date=start_date, end_date=end_date, frequency="d", adjustflag="3")
        df = _bs_rs_to_df(rs)
    for c in ("close", "peTTM", "pbMRQ", "psTTM", "pcfNcfTTM", "turn"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def baostock_stock_basic(code):
    bs_code = _bs_code(code)
    with bs_session() as bs:
        df = _bs_rs_to_df(bs.query_stock_basic(code=bs_code))
    return df.iloc[0].to_dict() if not df.empty else {}
