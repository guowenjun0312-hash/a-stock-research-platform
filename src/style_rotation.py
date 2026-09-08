#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
市场风格与板块轮动雷达（2026-09-08 起，每日收盘后运行）

每天记录：行业涨幅榜/跌幅榜、行业与概念主力资金 TOP、涨停结构 → 归类到风格桶
（资源周期 / 科技成长 / 消费题材 / 防御红利 / 高端制造 / 其他），并和前一天对比，
输出"风格正在强化 / 切换 / 混沌"判断，供各部门调整权重取向。

用法: python3 tools/style_rotation.py [YYYY-MM-DD]
输出: examples/rotation_history.json（追加历史）+ docs/reports/风格轮动_YYYY-MM-DD.md
"""
import os
import sys
import json
import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HIST = os.path.join(ROOT, "examples", "rotation_history.json")
REPORT_DIR = os.path.join(ROOT, "docs", "reports")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import astock_client as ac  # noqa: E402

STYLE_BUCKETS = {
    "资源周期": ["有色", "金属", "铜", "黄金", "贵金属", "小金属", "稀缺资源",
              "石油", "煤炭", "钢铁", "化工", "农化", "化学", "油服", "工业金属"],
    "科技成长": ["半导体", "元件", "电子", "通信", "计算机", "软件", "芯片",
              "光学", "光模块", "PCB", "存储", "算力", "AI", "游戏", "数字",
              "互联网", "消费电子", "IT", "传媒", "出版", "教育"],
    "消费题材": ["零售", "食品", "饮料", "白酒", "农业", "种植", "养殖", "旅游",
              "酒店", "餐饮", "免税", "影视", "纺织", "医药", "生物", "医美",
              "家电", "汽车整车", "休闲"],
    "防御红利": ["银行", "保险", "证券", "多元金融", "房地产", "公用", "电力",
              "燃气", "水务", "交通", "港口", "高速", "建筑", "红利", "央企",
              "铁路公路", "航运"],
    "高端制造": ["机械", "设备", "电气", "军工", "船舶", "风电", "光伏", "新能源",
              "电池", "电网", "电源", "机器人", "工业母机", "通用设备", "专用设备"],
}


def classify(name):
    for bucket, keys in STYLE_BUCKETS.items():
        if any(k in name for k in keys):
            return bucket
    return "其他"


def style_summary(rows, period_label):
    """按风格桶聚合：出现次数 + 主力净额合计（today 周期才计入净额）。"""
    agg = {}
    for r in rows:
        b = classify(r["name"])
        a = agg.setdefault(b, {"count": 0, "main_net_yi": 0.0, "names": []})
        a["count"] += 1
        a["names"].append(f"{r['name']}({r['change_pct']}%)")
        net = r.get("main_net") or 0
        a["main_net_yi"] += net / 1e8
    return agg


def main():
    today = (sys.argv[1] if len(sys.argv) > 1
             else datetime.date.today().strftime("%Y-%m-%d"))
    ic = ac.industry_comparison(15)
    ind_top = ic["top"]
    ind_bot = ic["bottom"]
    try:
        flow_ind = ac.board_fund_flow("industry", "today", 12)
    except Exception:
        flow_ind = {"rows": []}
    try:
        flow_con = ac.board_fund_flow("concept", "today", 12)
    except Exception:
        flow_con = {"rows": []}

    if flow_ind["rows"]:
        buckets = style_summary(flow_ind["rows"], "today")
        if not buckets:
            label, method = "数据缺失(接口暂不可用)", "无数据"
        else:
            dom = max(buckets.items(), key=lambda kv: kv[1]["main_net_yi"])
            label = dom[0] if dom[1]["main_net_yi"] > 1 else "混沌/多线并行"
            method = "行业主力资金口径"
    else:
        # 资金接口被风控时降级：用涨幅榜的行业归因近似
        buckets = style_summary([{"name": r["name"], "change_pct": r["change_pct"],
                                  "main_net": r["change_pct"] * 1e8}
                                 for r in ind_top[:10]], "approx")
        if not buckets:
            label, method = "数据缺失(接口暂不可用)", "无数据"
        else:
            dom = max(buckets.items(), key=lambda kv: kv[1]["count"])
            label = dom[0] if dom[1]["count"] >= 2 else "混沌/多线并行"
            method = "涨幅榜归因近似（资金接口暂不可用）"

    # 读取历史，对比上一交易日
    history = []
    if os.path.exists(HIST):
        try:
            history = json.load(open(HIST, encoding="utf-8"))
        except Exception:
            history = []
    prev = history[-1] if history else None
    if prev and prev.get("date") != today:
        prev_label = prev.get("style", {}).get("label")
        if prev_label == label:
            note = f"风格延续：{label} 连续主导（≥2 日），做多方向与仓位可跟随主线"
        elif prev_label and prev_label != "混沌/多线并行":
            note = f"风格切换：{prev_label} → {label}，按新主线重配方向，旧主线票降级"
        else:
            note = f"风格变化：{prev_label} → {label}，确认至少 2 日再升级为主方向"
    else:
        note = "首个交易日快照，待积累 2 日以上判断连续性"

    snap = {"date": today, "style": {"label": label, "method": method, "buckets": buckets,
                                     "note": note},
            "industry_top": ind_top[:8], "industry_bottom": ind_bot[:5],
            "flow_industry_top": flow_ind["rows"][:8],
            "flow_concept_top": flow_con["rows"][:8]}
    history = [h for h in history if h.get("date") != today] + [snap]
    history.sort(key=lambda h: h["date"])
    os.makedirs(os.path.dirname(HIST), exist_ok=True)
    json.dump(history, open(HIST, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    lines = [f"# 风格轮动雷达（{today}）", "",
             f"**主导风格：{label}**", "",
             f"- 风格桶资金分布（行业今日主力净额，亿元）："]
    for b, a in sorted(buckets.items(), key=lambda kv: -kv[1]["main_net_yi"]):
        lines.append(f"  - {b}: 上榜 {a['count']} 个，主力净额 {a['main_net_yi']:+.1f} 亿 | "
                     f"{'、'.join(a['names'][:4])}")
    lines += ["", f"**轮动判断：{note}**", "",
              "## 行业涨幅 TOP", "| 行业 | 涨跌% | 领涨 |",
              "|------|-------|------|"]
    for r in ind_top[:8]:
        lines.append(f"| {r['name']} | {r['change_pct']}% | {r['leader']} |")
    lines += ["", "## 行业主力净流入 TOP", "| 行业 | 主力净额 | 净占比% |",
              "|------|----------|--------|"]
    for r in flow_ind["rows"][:8]:
        lines.append(f"| {r['name']} | {r['main_net']/1e8:+.2f}亿 | {r['main_pct']} |")
    lines += ["", "## 概念主力净流入 TOP", "| 概念 | 主力净额 | 净占比% |",
              "|------|----------|--------|"]
    for r in flow_con["rows"][:8]:
        lines.append(f"| {r['name']} | {r['main_net']/1e8:+.2f}亿 | {r['main_pct']} |")
    lines += ["", "## 跌幅居前行业", "| 行业 | 涨跌% |", "|------|-------|"]
    for r in ind_bot[:5]:
        lines.append(f"| {r['name']} | {r['change_pct']}% |")
    lines += ["", "*仅供研究参考，不构成投资建议。*"]
    os.makedirs(REPORT_DIR, exist_ok=True)
    out = os.path.join(REPORT_DIR, f"风格轮动_{today}.md")
    open(out, "w", encoding="utf-8").write("\n".join(lines))
    print("\n".join(lines[:26]))
    print(f"\n✅ 已保存历史 {len(history)} 天: {HIST}")
    print(f"✅ 报告: {out}")


if __name__ == "__main__":
    main()
