"""
开机自动补齐回测行情数据到 MongoDB
使用 baostock 全量补齐 A 股日线数据，支持多线程并行下载
"""

import baostock as bs
import pymongo
import os
import sys
import time
import traceback

MONGO_HOST = os.getenv("MONGO_URI", "127.0.0.1:27017")
MONGO_USER = os.getenv("MONGO_USER", "panda")
MONGO_PASS = os.getenv("MONGO_PASSWORD", "panda")
MONGO_AUTH = os.getenv("MONGO_AUTH_DB", "admin")
MONGO_DB = os.getenv("MONGO_DB", "panda")

# 测试模式：限制下载股票数（0=不限，用于快速验证 pipeline 全流程）
# 每批最多下载多少只股票（防 pipeline 2h 超时，每次部署增量下载）
# 500 只 ≈ 9 分钟（0.9 只/秒），留下足够时间给索引/日历/备份
DOWNLOAD_BATCH = 0

# 强制重下载列表（环境变量 FORCE_SYMBOLS="000001.SZ,600000.SH"）
# 这些标的即使已有数据也会重新下载（用于修复数据不完整的问题）
FORCE_SYMBOLS = set()
_force = os.getenv("FORCE_SYMBOLS", "")
if _force:
    FORCE_SYMBOLS = set(s.strip() for s in _force.split(",") if s.strip())
    print(f"   ⚠ 强制重下载标的: {FORCE_SYMBOLS}")

START_DATE = "2020-01-01"
END_DATE = "2026-07-22"
END_DATE_C = "20260722"

# 指数列表
INDEX_LIST = [
    ("000001", "上证指数"),
    ("000300", "沪深300"),
    ("000500", "中证500"),
    ("001000", "中证1000"),
]


def get_symbol(code, exchange="sz"):
    """根据 baostock 的交易所前缀生成标准 symbol
    exchange: 'sh' 或 'sz'（来自 baostock code 的 sh.xxxx / sz.xxxx 前缀）
    """
    exch_map = {"sh": "SH", "sz": "SZ"}
    return f"{code}.{exch_map.get(exchange, 'SZ')}"


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


def build_doc(row, symbol, code):
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
    rs = bs.query_trade_dates(START_DATE, END_DATE)
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


def download_index_kline(code, name):
    """下载单只指数日线"""
    symbol = f"{code}.SH"
    bs_code = f"sh.{code}"
    try:
        rs = bs.query_history_k_data_plus(
            bs_code,
            "date,code,open,high,low,close,preclose,volume,amount",
            START_DATE, END_DATE,
            frequency="d",
            adjustflag="3",
        )
        docs = []
        while rs.next():
            row = rs.get_row_data()
            if row[0] is None:
                continue
            doc = build_doc(row, symbol, code)
            if doc:
                docs.append(doc)
        return (symbol, name, docs, None)
    except Exception as e:
        return (symbol, name, [], str(e))


def main():
    print("=" * 60)
    print(f"回测行情数据预加载（{START_DATE} ~ {END_DATE}）")
    print("=" * 60)

    # 0. 等 MongoDB
    print("\n[0/5] 等待 MongoDB...")
    if not wait_mongo():
        print("❌ MongoDB 未就绪，跳过")
        sys.exit(1)
    print("   MongoDB 就绪 ✓")

    uri = f"mongodb://{MONGO_USER}:{MONGO_PASS}@{MONGO_HOST}/{MONGO_AUTH}"
    client = pymongo.MongoClient(uri)
    db = client[MONGO_DB]

    # 1. 连接 baostock
    print("\n[1/5] 连接 baostock...")
    lg = bs.login()
    if lg.error_code != "0":
        print(f"❌ baostock 登录失败: {lg.error_msg}")
        client.close()
        return
    print("   baostock 就绪 ✓")

    try:
        # 2. 数据现状
        print("\n[2/5] 检查已有数据...")
        stock_cnt = db.stock_market.count_documents({})
        info_cnt = db.stock_info_new.count_documents({})
        print(f"   stock_info_new: {info_cnt} 条")
        print(f"   stock_market:   {stock_cnt} 条")

        # 3. 获取完整 A 股列表
        print("\n[3/5] 获取 A 股列表...")
        rs = bs.query_all_stock(END_DATE)
        print(f"   baostock 字段: {rs.fields}")
        all_stocks = []
        while rs.next():
            row = rs.get_row_data()
            # baostock query_all_stock 返回: [code, tradeStatus, code_name, ipoDate]
            # code 格式如 sh.600000 或 sz.000001，必须保留交易所前缀！
            parts = row[0].split(".")
            exchange = parts[0] if len(parts) == 2 else "sz"  # sh / sz
            raw_code = parts[1] if len(parts) == 2 else row[0]
            trade_status = row[1]  # "1"=正常交易
            code_name = row[2]     # 股票中文名
            all_stocks.append((raw_code, exchange, code_name, trade_status))
        print(f"   baostock 返回 {len(all_stocks)} 只股票")

        # 4. 更新 stock_info_new（只插入新股票，不覆盖已有）
        print(f"\n[4/5] 更新 stock_info_new...")
        existing_symbols = set()
        for s in db.stock_info_new.find({"type": 0}, {"symbol": 1, "_id": 0}):
            existing_symbols.add(s["symbol"])

        new_info = []
        for raw_code, exchange, code_name, trade_status in all_stocks:
            if not code_name or code_name == "":
                code_name = raw_code
            symbol = get_symbol(raw_code, exchange)
            if symbol not in existing_symbols and trade_status == "1":
                new_info.append({"symbol": symbol, "name": code_name, "type": 0})
                existing_symbols.add(symbol)

        if new_info:
            db.stock_info_new.insert_many(new_info, ordered=False)
            print(f"   √ 新增 {len(new_info)} 只股票到 stock_info_new")
        else:
            print(f"   - stock_info_new 已包含全部 {len(existing_symbols)} 只股票，无需更新")

        # 更新指数信息
        existing_idx = set()
        for s in db.stock_info_new.find({"type": 1}, {"symbol": 1, "_id": 0}):
            existing_idx.add(s["symbol"])
        new_idx_info = []
        for code, name in INDEX_LIST:
            symbol = f"{code}.SH"
            if symbol not in existing_idx:
                new_idx_info.append({"symbol": symbol, "name": name, "type": 1})
                existing_idx.add(symbol)
        if new_idx_info:
            db.stock_info_new.insert_many(new_idx_info, ordered=False)
            print(f"   √ 新增 {len(new_idx_info)} 只指数到 stock_info_new")

        # 5. 批量顺序下载股票日线（baostock 非线程安全，不能并行）
        print(f"\n[5/5] 顺序下载股票日线...")
        # 确保 stock_market 有 symbol 索引，否则 7000+ find_one 全表扫描会卡 10 分钟
        print("   - 创建/确认索引...")
        db.stock_market.create_index("symbol")
        db.stock_market.create_index([("symbol", 1), ("date", -1)])
        db.index_daily_price.create_index("symbol")

        # 强制重下载的标的提前处理（不受 DOWNLOAD_BATCH 限制）
        force_list = []
        if FORCE_SYMBOLS:
            print(f"   - 预取强制重下载标的: {FORCE_SYMBOLS}")
            for raw_code, exchange, code_name, trade_status in all_stocks:
                symbol = get_symbol(raw_code, exchange)
                if symbol in FORCE_SYMBOLS and trade_status == "1":
                    force_list.append((symbol, code_name or raw_code, raw_code, exchange, START_DATE))
                    print(f"      √ {symbol} → 强制重下载")
            # 已处理的从列表中移除，避免后面重复计数
            if force_list:
                print()

        print("   - 构建待下载列表（每 500 只打一次进度）...")
        to_download = []
        build_t0 = time.time()
        for i, (raw_code, exchange, code_name, trade_status) in enumerate(all_stocks, 1):
            if trade_status != "1":
                continue
            symbol = get_symbol(raw_code, exchange)
            if symbol in FORCE_SYMBOLS:
                # 已提前处理，跳过
                continue
            latest = db.stock_market.find_one({"symbol": symbol}, sort=[("date", -1)])
            if latest and latest["date"] >= END_DATE_C:
                continue
            if latest:
                d = str(latest["date"])
                start = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
            else:
                start = START_DATE
            to_download.append((symbol, code_name or raw_code, raw_code, exchange, start))
            if i % 500 == 0:
                now = time.time()
                print(f"      已扫描 {i}/{len(all_stocks)}（{now - build_t0:.0f}s），待下载: {len(to_download)}")

        # 强制重下载的标的插到最前面，确保不被批次截断
        to_download = force_list + to_download
        total_raw = len(to_download)
        force_count = len(force_list)
        if total_raw - force_count > DOWNLOAD_BATCH:
            remaining = (total_raw - force_count) - DOWNLOAD_BATCH
            batch_end = DOWNLOAD_BATCH + force_count  # 保留 force 标的 + BATCH 个普通标的
            to_download = to_download[:batch_end]
            print(f"   ⚠ 分批模式: 本次下载 {len(force_list)} 强制 + {DOWNLOAD_BATCH} 增量（剩余 {remaining} 只下次补）")
        total = len(to_download)
        print(f"   需下载: {total} 只（构建耗时 {time.time() - build_t0:.0f}s）")
        if total == 0:
            print("   - 全部已最新")
        else:
            stock_filled = 0
            t0 = last_print = time.time()
            for idx, (symbol, name, code, exchange, start) in enumerate(to_download, 1):
                try:
                    bs_code = f"{exchange}.{code}"
                    rs = bs.query_history_k_data_plus(
                        bs_code,
                        "date,code,open,high,low,close,preclose,volume,amount",
                        start, END_DATE,
                        frequency="d",
                        adjustflag="3",
                    )
                    rows = []
                    while rs.next():
                        row = rs.get_row_data()
                        if row[0] is not None:
                            rows.append(row)

                    # 诊断：打印前2行原始数据
                    if rows:
                        print(f"     诊断 {symbol}: 字段={rs.fields}, 第1行={rows[0]}, 第2行={rows[1] if len(rows)>1 else 'N/A'}")

                    if rows:
                        docs = [build_doc(r, symbol, code) for r in rows]
                        docs = [d for d in docs if d is not None]

                        if docs:
                            if symbol in FORCE_SYMBOLS:
                                # 强制重下载：删除旧数据，全量写入
                                db.stock_market.delete_many({"symbol": symbol})
                                db.stock_market.insert_many(docs, ordered=False)
                                stock_filled += len(docs)
                            else:
                                existing = set()
                                for d in db.stock_market.find({"symbol": symbol}, {"date": 1, "_id": 0}):
                                    existing.add(d["date"])
                                new_docs = [d for d in docs if d["date"] not in existing]
                                if new_docs:
                                    db.stock_market.insert_many(new_docs, ordered=False)
                                    stock_filled += len(new_docs)

                except Exception as e:
                    print(f"   × {symbol} 异常: {e}")
                    traceback.print_exc()

                # 每 50 只或 25 秒打一次进度，防止 pipeline 超时 kill
                now = time.time()
                if idx % 50 == 0 or idx == total or (now - last_print) > 25:
                    elapsed = now - t0
                    rate = idx / elapsed if elapsed > 0 else 0
                    eta = (total - idx) / rate if rate > 0 else 0
                    print(f"   √ [{idx}/{total}] +{stock_filled} 条 | "
                          f"{rate:.1f} 只/秒 | 预计剩余 {eta:.0f}s")
                    last_print = now

        # 6. 下载指数日线（全量下载，覆盖补齐，4 只指数很快）
        print(f"\n  下载指数日线...")
        idx_filled = 0
        for code, name in INDEX_LIST:
            symbol = f"{code}.SH"

            sym, n, docs, err = download_index_kline(code, name)
            if err:
                print(f"   × {sym} ({name}) 失败: {err}")
                continue

            if docs:
                existing_dates = set()
                for d in db.index_daily_price.find({"symbol": sym}, {"date": 1, "_id": 0}):
                    existing_dates.add(d["date"])
                new_docs = [d for d in docs if d["date"] not in existing_dates]

                if new_docs:
                    db.index_daily_price.insert_many(new_docs, ordered=False)
                    idx_filled += len(new_docs)
                    print(f"   √ {sym} ({name}): +{len(new_docs)} 条")

        # 7. 交易日历
        print(f"\n  更新交易日历...")
        update_trade_calendar(db)

        # 统计
        final_stock = db.stock_market.count_documents({})
        final_idx = db.index_daily_price.count_documents({})
        elapsed = time.time() - t0
        print(f"\n{'=' * 60}")
        print(f"✅ 补齐完成！耗时 {elapsed:.0f}s")
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
