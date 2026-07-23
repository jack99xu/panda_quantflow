"""
回测诊断信息 dump — 运行后写入项目目录，git pull 后本地可直接读取
"""
import os
import sys
from datetime import datetime

# 确保能导进 common 模块
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common.config.config import config
from common.connector.mongodb_handler import DatabaseHandler

OUTPUT = os.path.join(os.path.dirname(__file__), "..", "cnb_diagnostics.txt")


def main():
    db = DatabaseHandler(config)

    lines = []
    lines.append("=" * 60)
    lines.append(f"诊断时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 60)

    # stock_info_new
    count = db.mongo_find(config["MONGO_DB"], "stock_info_new", {}, {"symbol": 1, "name": 1, "type": 1})
    if count:
        lines.append(f"\nstock_info_new ({len(count)} 条):")
        for doc in count[:20]:
            lines.append(f"  {doc.get('symbol','?'):12s} type={doc.get('type','?')}  {doc.get('name','?')}")
    else:
        lines.append(f"\nstock_info_new: 空")

    # stock_market
    count = db.mongo_find(config["MONGO_DB"], "stock_market", {}, {"symbol": 1})
    symbols = set(d.get("symbol") for d in count) if count else set()
    lines.append(f"\nstock_market: {len(symbols)} 只股票")

    # index_daily_price
    count = db.mongo_find(config["MONGO_DB"], "index_daily_price", {}, {"symbol": 1})
    symbols = set(d.get("symbol") for d in count) if count else set()
    lines.append(f"index_daily_price: {len(symbols)} 只指数")
    for s in sorted(symbols):
        lines.append(f"  {s}")

    # trade_calendar
    total = db.mongo_find(config["MONGO_DB"], "trade_calendar", {}, {"nature_date": 1})
    if total:
        dates = set(d.get("nature_date") for d in total)
        lines.append(f"\ntrade_calendar: {len(dates)} 个日期")

    # 检查日志中是否有最近的错误
    log_file = "/tmp/quantflow.log"
    if os.path.exists(log_file):
        with open(log_file, "r") as f:
            content = f.read()
        # 只取最后 30 行看错误
        tail = "\n".join(content.strip().splitlines()[-30:])
        lines.append(f"\nquantflow.log (最后 30 行):\n{tail}")

    output = "\n".join(lines) + "\n"
    with open(OUTPUT, "w") as f:
        f.write(output)
    print(output)
    print(f"\n诊断已写入 {OUTPUT}")


if __name__ == "__main__":
    main()
