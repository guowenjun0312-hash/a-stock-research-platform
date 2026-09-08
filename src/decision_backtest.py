#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
部门决策账本 · 决策级回测（2026-09-07）

把每次“团队最终决定”作为一笔样本回测：
  - BUY   ：按参考价入场，执行止盈止损/持有期规则，判 exit_ret>0 为胜
  - SELL  ：执行减仓后，后续走弱（5日收益<0）为胜
  - AVOID/CASH：回避后标的走弱/大盘走弱（5日收益<=0）为正确
  - HOLD  ：按持仓规则执行，exit_ret>0 为胜

用法: STOCK_DATA_DIR=data/hist python3 src/decision_backtest.py
"""
import os, json, glob
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("STOCK_DATA_DIR") or os.path.join(ROOT, "data", "hist")
LEDGER = os.path.join(ROOT, "examples", "decision_ledger_sample.json")
REPORT = os.path.join(ROOT, "docs", "reports", "决策回测结果示例.md")


def load_kline(code):
    hits = glob.glob(os.path.join(DATA_DIR, f"{code}_*.csv"))
    if not hits:
        hits = glob.glob(os.path.join(DATA_DIR, f"{code}.csv"))
    if not hits:
        return None
    df = pd.read_csv(hits[0])
    df.columns = [c.strip() for c in df.columns]
    df["date"] = df["date"].astype(str)
    df = df.sort_values("date").reset_index(drop=True)
    return df


def evaluate(dec):
    """返回一笔决策的核验结果 dict。"""
    code = dec["code"]
    date = dec["date"]
    action = dec["action"]
    hold = int(dec.get("hold_days", 5))
    df = load_kline(code)
    if df is None:
        return {**dec, "ok": False, "reason": "无K线数据"}
    dates = list(df["date"])
    if date not in dates:
        return {**dec, "ok": False, "reason": f"决策日 {date} 不在数据内"}
    i = dates.index(date)
    seg = df.iloc[i + 1: i + 1 + hold]
    if len(seg) == 0:
        return {**dec, "ok": False, "reason": "决策日之后无数据（未到核验期）"}
    entry = float(dec["ref_px"]) if dec.get("ref_px") else float(df.iloc[i]["close"])
    stop = dec.get("stop")
    t_ret = {}
    for off in (1, 3, 5):
        row = df.iloc[i + off] if i + off < len(df) else None
        t_ret[f"T{off}"] = (float(row["close"]) / entry - 1) if row is not None else None
    # 规则退出：先查止损（low 触及即按止损价离场），否则持有期末收盘离场
    exit_ret = None
    exit_reason = "hold"
    if stop:
        hit = seg[seg["low"] <= float(stop)]
        if len(hit) > 0:
            exit_ret = float(stop) / entry - 1
            exit_reason = "stop"
    if exit_ret is None:
        last = seg.iloc[-1]
        exit_ret = float(last["close"]) / entry - 1
    # 判定
    verdict = None
    if action == "BUY":
        verdict = "WIN" if exit_ret > 0 else "LOSS"
    elif action == "SELL":
        ref = t_ret.get("T5") if t_ret.get("T5") is not None else exit_ret
        verdict = "WIN" if ref < 0 else "LOSS"
    elif action in ("AVOID", "CASH"):
        ref = t_ret.get("T5") if t_ret.get("T5") is not None else exit_ret
        verdict = "WIN" if ref <= 0 else "LOSS"
    elif action == "HOLD":
        verdict = "WIN" if exit_ret > 0 else "LOSS"
    return {**dec, "ok": True, "entry": entry, "exit_ret": exit_ret,
            "exit_reason": exit_reason, "t": t_ret, "verdict": verdict}


def main():
    ledger = json.load(open(LEDGER, encoding="utf-8"))
    decs = ledger["decisions"]
    rows = [evaluate(d) for d in decs]
    print("=" * 100)
    print("部门最终决策 · 决策级回测（数据截至 2026-09-04 收盘）")
    print("=" * 100)
    print(f"{'ID':<5}{'日期':<11}{'代码':<7}{'名称':<14}{'方向':<7}{'参考价':>8}{'止损':>7}  "
          f"{'T1':>7}{'T3':>7}{'T5':>8}  {'规则结果':>8} {'判定':<5} 备注")
    for r in rows:
        if not r.get("ok"):
            print(f"{r['id']:<5}{r['date']:<11}{r['code']:<7}{r['name']:<14}{r['action']:<7}"
                  f"{'—':>8}{'—':>7}  {'—':>7}{'—':>7}{'—':>8}  {'—':>8} {r['reason']}")
            continue
        fmt = lambda x: f"{x*100:+.1f}%" if x is not None else "  —  "
        sr = f"{r['exit_ret']*100:+.1f}%({r['exit_reason']})"
        print(f"{r['id']:<5}{r['date']:<11}{r['code']:<7}{r['name']:<14}{r['action']:<7}"
              f"{r['entry']:>8.2f}{r['stop'] if r.get('stop') else '—':>7}  "
              f"{fmt(r['t']['T1']):>7}{fmt(r['t']['T3']):>7}{fmt(r['t']['T5']):>8}  "
              f"{sr:>10} {r['verdict']:<5} {r['note']}")
    # 汇总
    closed = [r for r in rows if r.get("ok") and r.get("verdict")]
    print("\n=== 按方向汇总（已核验） ===")
    for act in ("BUY", "SELL", "HOLD", "AVOID", "CASH"):
        sub = [r for r in closed if r["action"] == act]
        if not sub:
            continue
        wins = [r for r in sub if r["verdict"] == "WIN"]
        avg = sum(r["exit_ret"] for r in sub) / len(sub)
        print(f"{act:<6} 决策 {len(sub):>2} 笔 | 正确/命中 {len(wins):>2} | "
              f"胜率 {len(wins)/len(sub)*100:5.1f}% | 平均规则收益 {avg*100:+6.1f}%")
    buys = [r for r in closed if r["action"] == "BUY"]
    if buys:
        print(f"\n进攻类 BUY 合计 {len(buys)} 笔，命中 {sum(r['verdict']=='WIN' for r in buys)} 笔，"
              f"胜率 {sum(r['verdict']=='WIN' for r in buys)/len(buys)*100:.1f}%")
    defs = [r for r in closed if r["action"] in ("SELL", "AVOID", "CASH")]
    if defs:
        print(f"防守类 SELL/AVOID/CASH 合计 {len(defs)} 笔，正确 "
              f"{sum(r['verdict']=='WIN' for r in defs)} 笔，"
              f"正确率 {sum(r['verdict']=='WIN' for r in defs)/len(defs)*100:.1f}%")
    # 生成报告
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("# 部门决策级回测（2026-09-07）\n\n")
        f.write("> 方法：把每次部门最终决定（日期/标的/方向/参考价/止损）作为样本，"
                "按各自纪律核验 T+1/T+3/T+5 与止损触发，统计我们自己决策的胜率。\n")
        f.write(f"> 数据截至 2026-09-04 收盘；样本来自工作区落档记录，见 `数据中心/_量化/decision_ledger.json`。\n\n")
        f.write("| ID | 日期 | 代码 | 名称 | 方向 | 参考价 | 止损 | T1 | T3 | T5 | 规则结果 | 判定 | 备注 |\n")
        f.write("|----|------|------|------|------|--------|------|-----|-----|-----|----------|------|------|\n")
        for r in rows:
            if not r.get("ok"):
                f.write(f"| {r['id']} | {r['date']} | {r['code']} | {r['name']} | {r['action']} | — | — | — | — | — | — | {r['reason']} |\n")
                continue
            fmt = lambda x: f"{x*100:+.1f}%" if x is not None else "—"
            f.write(f"| {r['id']} | {r['date']} | {r['code']} | {r['name']} | {r['action']} | "
                    f"{r['entry']:.2f} | {r['stop'] if r.get('stop') else '—'} | {fmt(r['t']['T1'])} | "
                    f"{fmt(r['t']['T3'])} | {fmt(r['t']['T5'])} | {r['exit_ret']*100:+.1f}%({r['exit_reason']}) | "
                    f"{r['verdict'] or '待核验'} | {r['note']} |\n")
        f.write("\n## 按方向汇总\n\n")
        for act in ("BUY", "SELL", "HOLD", "AVOID", "CASH"):
            sub = [r for r in closed if r["action"] == act]
            if not sub:
                continue
            wins = [r for r in sub if r["verdict"] == "WIN"]
            avg = sum(r["exit_ret"] for r in sub) / len(sub)
            f.write(f"- **{act}**：{len(sub)} 笔，命中 {len(wins)}，"
                    f"胜率 {len(wins)/len(sub)*100:.1f}%，平均规则收益 {avg*100:+.1f}%\n")
        f.write("\n*仅供研究参考，不构成投资建议。*\n")
    print(f"\n报告已写入: {REPORT}")


if __name__ == "__main__":
    main()
