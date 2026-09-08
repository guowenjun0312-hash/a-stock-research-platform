#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
下载一小批公开主板日线（baostock，前复权）用于演示 factor_learning.py。

用法: python3 src/prepare_demo_data.py 600519 000001 601318 [更多代码...]
数据写入: data/hist/<code>.csv
"""
import os
import sys
import time

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "hist")
DEFAULT = ["600519", "000001", "601318", "000858", "600036", "601899", "000878"]


def main():
    codes = sys.argv[1:] or DEFAULT
    os.makedirs(OUT, exist_ok=True)
    import baostock as bs
    lg = bs.login()
    if lg.error_code != "0":
        raise SystemExit(f"baostock 登录失败: {lg.error_code}")
    ok = 0
    try:
        for code in codes:
            dest = os.path.join(OUT, f"{code}.csv")
            if os.path.exists(dest):
                ok += 1
                continue
            bs_code = ("sh." if code[:2] in ("60", "68", "90") else "sz.") + code
            rs = bs.query_history_k_data_plus(
                bs_code,
                "date,open,high,low,close,volume,amount,turn,tradestatus",
                start_date="2024-01-01", end_date=time.strftime("%Y-%m-%d"),
                frequency="d", adjustflag="2")
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            df = pd.DataFrame(rows, columns=rs.fields)
            if not df.empty:
                df.to_csv(dest, index=False)
                ok += 1
            time.sleep(0.2)
    finally:
        bs.logout()
    print(f"完成：{ok}/{len(codes)} 只，数据目录 {OUT}")
    print("下一步: STOCK_DATA_DIR=data/hist python3 src/factor_learning.py")


if __name__ == "__main__":
    main()
