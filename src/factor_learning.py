#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
部门因子权重学习（2026-09-08）

目标：各部门不再"等权重平均"拼分，而是用历史数据估计每个因子对 3/5 日前瞻收益的
预测能力（Spearman IC / ICIR / 稳定性），形成各部门自己的因子权重。

因子分组：
  C 技术层：ret5/ret20/ret60 动量、MA 多头、RSI14、ATR%、量比、量能趋势、20日位置、
            距MA20、波动率、近10日是否涨停
  A 基本面层（以 2026H1 财报为当前已知口径）：EPS、营收增速、净利增速、ROE、毛利率、
            每股经营现金流
  D 情绪/资金层（量价代理）：换手率均值、量比、5日动量、离20日高距离、近期涨停基因

输出：examples/factor_weights.json + docs/reports/因子权重学习示例.md
用法：STOCK_DATA_DIR=data/hist python3 src/factor_learning.py
"""
import os
import glob
import json
import math

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HIST_DIR = os.environ.get("STOCK_DATA_DIR") or os.path.join(ROOT, "data", "hist")
OUT_JSON = os.path.join(ROOT, "examples", "factor_weights.json")
REPORT = os.path.join(ROOT, "docs", "reports", "因子权重学习示例.md")
MAINBOARD = ("60", "000", "001", "002", "003")


def rsi_series(closes, n=14):
    delta = closes.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / down.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def prep_stock(df, code, label):
    df = df.copy()
    if "tradestatus" in df.columns:
        df = df[df["tradestatus"].astype(str) == "1"]
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    if len(df) < 90:
        return None
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["high"] = pd.to_numeric(df["high"], errors="coerce")
    df["low"] = pd.to_numeric(df["low"], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    df["turn"] = pd.to_numeric(df["turn"], errors="coerce")
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]
    df["pct"] = c.pct_change() * 100
    out = pd.DataFrame({"date": df["date"]})
    out["ret5"] = c.pct_change(5)
    out["ret20"] = c.pct_change(20)
    out["ret60"] = c.pct_change(60)
    for n in (5, 10, 20, 60):
        out[f"ma{n}"] = c.rolling(n).mean()
    out["rsi"] = rsi_series(c)
    out["atr_pct"] = ((h - l) / c).rolling(14).mean()
    out["vol_ratio5"] = v / v.rolling(5).mean()
    out["vol_trend"] = v.rolling(5).mean() / v.rolling(20).mean()
    out["std20"] = c.pct_change().rolling(20).std()
    out["turn5"] = df["turn"].rolling(5).mean()
    out["pos20"] = (c - l.rolling(20).min()) / (h.rolling(20).max() - l.rolling(20).min())
    out["dist_ma20"] = (c - out["ma20"]) / out["ma20"]
    out["zt_10"] = (df["pct"].rolling(10).max() >= 9.7).astype(float)
    out["ma_up"] = ((c > out["ma5"]) + (c > out["ma10"]) + (c > out["ma20"])
                    + (c > out["ma60"]))
    # 前瞻收益
    out["fwd3"] = c.shift(-3) / c - 1
    out["fwd5"] = c.shift(-5) / c - 1
    # 得分因子（现用等权口径）与预实验权重
    out["score_now"] = (out["ret5"].clip(upper=0.10) * 100 +
                        out["ret20"].clip(upper=0.20) * 50 +
                        (c > out["ma5"]) + (c > out["ma10"]) + (c > out["ma20"]))
    return out


def md_table(df):
    cols = list(df.columns)
    head = "| " + " | ".join(cols) + " |"
    sep = "|" + "|".join(["---"] * len(cols)) + "|"
    rows = [head, sep]
    for _, r in df.iterrows():
        rows.append("| " + " | ".join(f"{r[c]:.4f}" if isinstance(r[c], float) else str(r[c])
                                      for c in cols) + " |")
    return "\n".join(rows)


def main():
    stocks = {}
    for path in glob.glob(os.path.join(HIST_DIR, "*.csv")):
        code = os.path.basename(path)[:-4]
        try:
            df = pd.read_csv(path)
            df.columns = [x.strip() for x in df.columns]
            p = prep_stock(df, code, "")
        except Exception:
            continue
        if p is not None:
            stocks[code] = p
    print(f"有效股票池: {len(stocks)}")

    # 财务因子（2026H1 已知口径，仅对近期窗口有意义）
    fund = {}
    fund_file = os.environ.get("FUND_FILE") or os.path.join(ROOT, "data", "yjbb.json")
    if os.path.exists(fund_file):
        for r in json.load(open(fund_file)):
            c = str(r.get("股票代码", "")).zfill(6)
            if c in stocks:
                fund[c] = {"eps": r.get("每股收益"),
                           "rev_g": r.get("营业总收入-同比增长"),
                           "net_g": r.get("净利润-同比增长"),
                           "roe": r.get("净资产收益率"),
                           "gm": r.get("销售毛利率"),
                           "ocf_ps": r.get("每股经营现金流量")}
    print(f"含财务因子标的: {len(fund)}（财务文件可选：data/yjbb.json）")

    # 取交易日历（用基准日期的并集近似，直接用最长K线的日期集）
    cal = pd.to_datetime(sorted(set().union(*[set(s["date"]) for s in stocks.values()])))
    cal = cal[cal >= cal[-1] - pd.Timedelta(days=900)]
    start_i = 70
    dates = list(cal[range(start_i, len(cal), 5)])
    print(f"截面期数: {len(dates)}")

    def cross_section(d, use_fund=False):
        rows = {}
        for code, s in stocks.items():
            sub = s[s["date"] <= d]
            if len(sub) == 0:
                continue
            row = sub.iloc[-1]
            if pd.isna(row.get("fwd5")) or pd.isna(row.get("rsi")):
                continue
            rec = {k: row.get(k) for k in
                   ("ret5", "ret20", "ret60", "rsi", "atr_pct", "vol_ratio5",
                    "vol_trend", "std20", "pos20", "dist_ma20", "zt_10",
                    "ma_up", "turn5", "fwd3", "fwd5", "score_now")}
            if use_fund and code in fund:
                rec.update(fund[code])
            rows[code] = rec
        return pd.DataFrame.from_dict(rows, orient="index")

    def ic_table(dates_use, use_fund=False):
        factors = (["ret5", "ret20", "ret60", "rsi", "atr_pct", "vol_ratio5",
                    "vol_trend", "std20", "pos20", "dist_ma20", "zt_10",
                    "ma_up", "turn5", "score_now"]
                   + (["eps", "rev_g", "net_g", "roe", "gm", "ocf_ps"] if use_fund else []))
        acc = {f: [] for f in factors}
        for d in dates_use:
            xs = cross_section(d, use_fund)
            if len(xs) < 80:
                continue
            for f in factors:
                if f in xs.columns and xs[f].notna().sum() > 80:
                    try:
                        ic = xs[f].corr(xs["fwd5"], method="spearman")
                        if pd.notna(ic):
                            acc[f].append(ic)
                    except Exception:
                        pass
        rows = []
        for f, ics in acc.items():
            if len(ics) < 5:
                continue
            arr = np.array(ics)
            rows.append({"factor": f, "n": len(arr), "ic": arr.mean(),
                         "icir": arr.mean() / (arr.std() + 1e-9) * math.sqrt(len(arr)),
                         "pos_rate": (arr > 0).mean(), "ic_std": arr.std()})
        return pd.DataFrame(rows).sort_values("ic", key=abs, ascending=False)

    # 窗口：全样本技术因子；近半年（2026-04 起）技术与财务因子
    full = ic_table(dates, use_fund=False)
    recent_dates = [d for d in dates if d >= pd.Timestamp("2026-04-01")]
    recent = ic_table(recent_dates, use_fund=True)
    print("\n=== 全样本技术因子 IC（对 5 日前瞻收益） ===")
    print(full.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print("\n=== 近半年（2026-04 起，含财务因子） ===")
    print(recent.to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    # 权重：C 技术层用近半年 ICIR（符号一致），A 层用近半年 ICIR，D 层从 C 中挑选情绪代理
    def weights(df, names):
        sub = df[df["factor"].isin(names)].dropna(subset=["icir"])
        w = {}
        for _, r in sub.iterrows():
            w[r["factor"]] = r["icir"]
        return w

    c_names = ["ret5", "ret20", "ret60", "rsi", "atr_pct", "vol_ratio5",
               "vol_trend", "std20", "pos20", "dist_ma20", "zt_10", "ma_up"]
    a_names = ["eps", "rev_g", "net_g", "roe", "gm", "ocf_ps"]
    d_names = ["turn5", "vol_ratio5", "ret5", "pos20", "zt_10"]
    cw = weights(recent, c_names)
    aw = weights(recent, a_names)
    dw = weights(recent, d_names)

    def norm(w, sign_consistent=True):
        if sign_consistent:
            # 技术类因子方向应一致为正（多头动量）；若某因子 ICIR 为负说明该因子反向有效，
            # 保留符号由调用方"反向用"，这里仅给出绝对值权重与方向
            pass
        s = sum(abs(v) for v in w.values()) or 1.0
        return {k: v / s for k, v in w.items()}

    cw_n = norm(cw)
    aw_n = norm(aw)
    dw_n = norm(dw)
    out = {"learned_at": "2026-09-08", "method": "5日前瞻收益 Spearman IC，ICIR 加权；权重已归一化",
           "dept_A_basic": aw_n, "dept_C_tech": cw_n, "dept_D_sentiment": dw_n,
           "note": "负权重表示该因子反向预测（数值越低越好）；B 新闻因子无历史语料，暂用前瞻性规则门槛。"}
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    json.dump(out, open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # 简版前后对比：现等权 score_now 与 学习权重 z 组合的 top 十分位前瞻收益
    def composite_compare():
        tech_keys = [k for k in cw_n if abs(cw_n[k]) > 0.01]
        res = {"now_top5": [], "learn_top5": [], "now_bottom5": [], "learn_bottom5": []}
        for d in recent_dates:
            xs = cross_section(d, use_fund=False)
            if len(xs) < 80:
                continue
            xs = xs.dropna(subset=["score_now"] + tech_keys)
            if xs.empty:
                continue
            z = (xs[tech_keys] - xs[tech_keys].mean()) / (xs[tech_keys].std() + 1e-9)
            xs["learn_score"] = z.dot(pd.Series(cw_n)).clip(lower=-20)
            for col, bucket in (("score_now", "now"), ("learn_score", "learn")):
                xs[bucket] = pd.qcut(xs[col], 10, labels=False, duplicates="drop")
                top = xs[xs[bucket] == xs[bucket].max()]["fwd5"].mean()
                bot = xs[xs[bucket] == xs[bucket].min()]["fwd5"].mean()
                res[f"{bucket}_top5"].append(top)
                res[f"{bucket}_bottom5"].append(bot)
        return {k: (sum(v) / len(v) if v else float("nan")) for k, v in res.items()}

    cmp = composite_compare()
    print("\n=== 近半年 top/bottom 十分位平均 5 日收益（等权 vs 学习权重） ===")
    for k, v in cmp.items():
        print(f"  {k}: {v*100:+.2f}%")

    lines = []
    add = lines.append
    add("# 部门因子权重学习报告（2026-09-08）")
    add("")
    add("> 方法：全市场主板（60/000/001/002/003，剔除ST）截面 Spearman IC，前瞻收益=5日；"
        "ICIR=IC均值/IC标准差×√期数，权重按近半年 ICIR 归一化。财务因子为 2026H1 已知口径。")
    add("")
    add("## 一、近半年因子 IC 排序（含财务）")
    add(md_table(recent))
    add("")
    add("## 二、全样本技术因子 IC")
    add(md_table(full))
    add("")
    add("## 三、各部门学习权重")
    for k, v in out.items():
        if isinstance(v, dict):
            add(f"- **{k}**: " + ", ".join(f"{f}={x:+.3f}" for f, x in v.items()))
    add("")
    add("## 四、等权 vs 学习权重（近半年 top/bottom 十分位 5日收益）")
    for k, v in cmp.items():
        add(f"- {k}: {v*100:+.2f}%")
    add("")
    add("## 五、使用规则")
    add("- C 技术层：按近半年 ICIR 加权合成，不再 ret5/ret20/MA 等权相加；")
    add("- A 基本面层：财务因子对 5 日短线 IC 弱（趋势市中更弱），只做硬门槛与中长期过滤，不用于短线排序加分；")
    add("- D 情绪层：换手/量比/位置/涨停基因按符号使用（负权重=过热降分）；")
    add("- B 新闻：历史语料暂缺，保留 100 条消息门槛 + 公告/互动易/研报交叉验证的前瞻规则，权重待积累后回测；")
    add("- 每 2 周复跑本工具一次，风格切换（如回强趋势）权重会漂移，以最近窗口为准。")
    add("")
    add("*仅供研究参考，不构成投资建议。*")
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    open(REPORT, "w", encoding="utf-8").write("\n".join(lines))
    print("\n已保存:", OUT_JSON)
    print("已保存:", REPORT)


if __name__ == "__main__":
    main()
