"""
开机自动补齐回测行情数据到 MongoDB
1. 从 `stock_info_new` 读取已有股票/指数列表（从完整数据库恢复后已有数千只标的）
2. 检查每只标的的日线数据是否已到目标日期
3. 对缺失部分，用 baostock 增量下载补齐
"""

import baostock as bs
import pymongo
import os
import sys
import time
import traceback

# MongoDB 配置
MONGO_HOST = os.getenv("MONGO_URI", "127.0.0.1:27017")
MONGO_USER = os.getenv("MONGO_USER", "panda")
MONGO_PASS = os.getenv("MONGO_PASSWORD", "panda")
MONGO_AUTH = os.getenv("MONGO_AUTH_DB", "admin")
MONGO_DB = os.getenv("MONGO_DB", "panda")

# 补齐目标日期
TARGET_DATE = "2026-07-22"
TARGET_DATE_C = "20260722"  # 紧凑格式

# 默认起始日期（新标的无历史数据时从此开始下载）
DEFAULT_START = "2024-01-01"

# 回退标的列表（仅当 stock_info_new 为空时才用）
FALLBACK_STOCKS = [
    ("000001", "平安银行"), ("000002", "万科A"), ("000333", "美的集团"),
    ("000651", "格力电器"), ("000858", "五粮液"), ("002594", "比亚迪"),
    ("300750", "宁德时代"), ("600000", "浦发银行"), ("600036", "招商银行"),
    ("600519", "贵州茅台"), ("601318", "中国平安"), ("000568", "泸州老窖"),
    ("002415", "海康威视"),
]
FALLBACK_INDICES = [
    ("000001", "上证指数"), ("000300", "沪深300"),
    ("000500", "中证500"), ("001000", "中证1000"),
]


def get_symbol(code):
    return f"{code}.SH" if code.startswith("6") else f"{code}.SZ"


def get_bs_code(code):
    return f"sh.{code}" if code.startswith("6") else f"sz.{code}"


def get_raw_code(symbol):
    """从 '000001.SZ' → '000001'"""
    return symbol.split(".")[0]


def wait_mongo(max_retries=30):
    uri = f"mongodb://{MONGO_USER}:{MONGO_PASS}@{MONGO_HOST}/{MONGO_AUTH}"
    for i in range(max_retries):
        try:
            c = pymongo.MongoClient(uri, serverSelectionTimeoutMS=2000)
            c.admin.command("ping")
            c.close()
            return True
        except Exception:
            if i < 3 or (i + 1) % 5 == 0:
                print(f"   等待 MongoDB ({i + 1}/{max_retries})...")
            time.sleep(2)
    return False


def get_latest_date(collection, symbol):
    """查询某只标的在 collection 中的最新日期，返回 YYYYMMDD 或 None"""
    doc = collection.find_one({"symbol": symbol}, sort=[("date", -1)])
    return doc["date"] if doc else None


def fetch_kline(bs_code, start, end):
    """从 baostock 下载日线，返回 dict 列表"""
    rs = bs.query_history_k_data_plus(
        bs_code,
        "date,code,open,high,low,close,preclose,volume,amount",
        start, end,
        frequency="d",
        adjustflag="2",
    )
    rows = []
    while rs.next():
        row = rs.get_row_data()
        if row[0] is None:
            continue
        rows.append(row)
    return rows


def build_doc(row, symbol, code):
    """将 baostock 行转为 MongoDB 文档"""
    try:
        return {
            "symbol": symbol,
            "code": code,
            "date": row[0].replace("-", ""),
            "trade_date": row[0].replace("-", ""),
            "open": float(row[2]) if row[2] else 0.0,
            "high": float(row[3]) if row[3] else 0.0,
            "low": float(row[4]) if row[4] else 0.0,
            "close": float(row[5]) if row[5] else 0.0,
            "preclose": float(row[6]) if row[6] else 0.0,
            "volume": float(row[7]) if row[7] else 0.0,
            "turnover": float(row[8]) if row[8] else 0.0,
            "trade_status": "交易",
        }
    except (ValueError, TypeError, IndexError):
        return None


def update_trade_calendar(db):
    """重新生成交易日历（全量覆盖）"""
    cal_first = db.trade_calendar.find_one(sort=[("nature_date", 1)])
    if cal_first:
        start = str(cal_first["nature_date"])
        y, m, d = start[:4], start[4:6], start[6:8]
        cal_start = f"{y}-{m}-{d}"
    else:
        cal_start = DEFAULT_START

    rs = bs.query_trade_dates(cal_start, TARGET_DATE)
    docs = []
    while rs.next():
        row = rs.get_row_data()
        nat = int(row[0].replace("-", ""))
        is_trade = 1 if row[1] == "1" else 0
        for ex in ("SH", "SZ"):
            docs.append({"nature_date": nat, "is_trade": is_trade, "exchange": ex})

    if docs:
        db.trade_calendar.delete_many({})
        db.trade_calendar.insert_many(docs, ordered=False)
        trade_days = sum(1 for d in docs if d["is_trade"] == 1) // 2
        print(f"   √ 交易日历 {len(docs)} 条（交易日 {trade_days} 天）")
    return len(docs)


def fill_kline(db, coll, items, kind_label):
    """补齐一批标的的日线数据"""
    filled = 0
    skipped = 0
    for symbol, name in items:
        code = get_raw_code(symbol)
        # 已有最新日期
        latest = get_latest_date(coll, symbol)
        if latest and latest >= TARGET_DATE_C:
            skipped += 1
            continue

        # 确定 baostock 查询起始日
        if latest:
            y, m, d = latest[:4], latest[4:6], latest[6:8]
            start = f"{y}-{m}-{d}"
        else:
            start = DEFAULT_START

        # 下载
        try:
            bs_code = get_bs_code(code) if kind_label == "股票" else f"sh.{code}"
            rows = fetch_kline(bs_code, start, TARGET_DATE)
        except Exception as e:
            print(f"   × {symbol} 下载失败: {e}")
            continue

        # 过滤已存在的日期
        existing = set()
        for d in coll.find({"symbol": symbol}, {"date": 1, "_id": 0}):
            existing.add(d["date"])

        docs = []
        for row in rows:
            date_c = row[0].replace("-", "")
            if date_c in existing:
                continue
            doc = build_doc(row, symbol, code)
            if doc:
                docs.append(doc)

        if docs:
            try:
                coll.insert_many(docs, ordered=False)
                print(f"   √ {symbol} ({name}): +{len(docs)} 条")
                filled += len(docs)
            except Exception as e:
                print(f"   × {symbol} 写入失败: {e}")
        else:
            print(f"   - {symbol} ({name}): 无新增")
    return filled, skipped


def main():
    print("=" * 60)
    print(f"回测行情数据预加载（增量补齐至 {TARGET_DATE}）")
    print("=" * 60)

    # 0. 等 MongoDB
    print("\n[0/5] 等待 MongoDB...")
    if not wait_mongo():
        print("❌ MongoDB 未就绪，跳过")
        sys.exit(1)
    print("   MongoDB 就绪 ✓")

    # 1. 连接
    uri = f"mongodb://{MONGO_USER}:{MONGO_PASS}@{MONGO_HOST}/{MONGO_AUTH}"
    client = pymongo.MongoClient(uri)
    db = client[MONGO_DB]

    # 2. 检查数据现状
    print("\n[1/5] 检查数据完整性...")
    stock_cnt = db.stock_market.count_documents({})
    index_cnt = db.index_daily_price.count_documents({})
    info_cnt = db.stock_info_new.count_documents({})

    print(f"   stock_info_new: {info_cnt} 条")
    print(f"   stock_market:   {stock_cnt} 条")
    print(f"   index_daily_price: {index_cnt} 条")

    # 从 stock_info_new 读取已有标的
    stocks = list(db.stock_info_new.find({"type": 0}))
    indices = list(db.stock_info_new.find({"type": 1}))

    # 回退：如果 stock_info_new 为空，用硬编码列表
    if not stocks:
        print("   ⚠ stock_info_new 无股票数据，使用内置回退列表")
        stocks = [(c, n) for c, n in FALLBACK_STOCKS]
        stocks = [{"symbol": get_symbol(c), "name": n} for c, n in FALLBACK_STOCKS]
    if not indices:
        print("   ⚠ stock_info_new 无指数数据，使用内置回退列表")
        indices = [{"symbol": f"{c}.SH", "name": n} for c, n in FALLBACK_INDICES]

    # 检查哪些标的未补齐
    missing_stocks = []
    for s in stocks:
        sym = s["symbol"]
        latest = get_latest_date(db.stock_market, sym)
        if not latest or latest < TARGET_DATE_C:
            missing_stocks.append((sym, s.get("name", sym)))
    missing_indices = []
    for idx in indices:
        sym = idx["symbol"]
        latest = get_latest_date(db.index_daily_price, sym)
        if not latest or latest < TARGET_DATE_C:
            missing_indices.append((sym, idx.get("name", sym)))

    # 交易日历检查
    cal_latest = db.trade_calendar.find_one(sort=[("nature_date", -1)])
    cal_missing = cal_latest is None or str(cal_latest["nature_date"]) < TARGET_DATE_C

    if not missing_stocks and not missing_indices and not cal_missing:
        print(f"\n✅ 全部数据已补齐至 {TARGET_DATE}，跳过预加载")
        client.close()
        return

    print(f"\n   需补齐: 股票 {len(missing_stocks)} 只, 指数 {len(missing_indices)} 个"
          f"{', 日历' if cal_missing else ''}")

    # 3. 连接 baostock
    print("\n[2/5] 连接 baostock...")
    lg = bs.login()
    if lg.error_code != "0":
        print(f"❌ baostock 登录失败: {lg.error_msg}")
        client.close()
        return
    print("   baostock 就绪 ✓")

    try:
        # 4. 交易日历
        print(f"\n[3/5] 交易日历更新...")
        update_trade_calendar(db)

        # 5. 补齐股票日线
        print(f"\n[4/5] 补齐股票日线 (stock_market)...")
        stock_filled, stock_ok = fill_kline(db, db.stock_market, missing_stocks, "股票")

        # 6. 补齐指数日线
        print(f"\n[5/5] 补齐指数日线 (index_daily_price)...")
        idx_filled, idx_ok = fill_kline(db, db.index_daily_price, missing_indices, "指数")

        # 统计
        final_stock = db.stock_market.count_documents({})
        final_idx = db.index_daily_price.count_documents({})
        print(f"\n{'=' * 60}")
        print(f"✅ 补齐完成！")
        print(f"   新增行情 {stock_filled} 条 / 指数 {idx_filled} 条")
        print(f"   总量 — 股票 {final_stock} 条 / 指数 {final_idx} 条")
        print(f"{'=' * 60}")

    except Exception as e:
        print(f"\n❌ 异常: {e}")
        traceback.print_exc()
    finally:
        bs.logout()
        client.close()


if __name__ == "__main__":
    main()
