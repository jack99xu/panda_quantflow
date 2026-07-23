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
from concurrent.futures import ThreadPoolExecutor, as_completed

MONGO_HOST = os.getenv("MONGO_URI", "127.0.0.1:27017")
MONGO_USER = os.getenv("MONGO_USER", "panda")
MONGO_PASS = os.getenv("MONGO_PASSWORD", "panda")
MONGO_AUTH = os.getenv("MONGO_AUTH_DB", "admin")
MONGO_DB = os.getenv("MONGO_DB", "panda")

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

DOWNLOAD_WORKERS = 10  # 并行下载线程数


def get_symbol(code):
    return f"{code}.SH" if code.startswith("6") else f"{code}.SZ"


def get_bs_code(code):
    return f"sh.{code}" if code.startswith("6") else f"sz.{code}"


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


def download_stock(args):
    """下载单只股票从 start 到 END_DATE 的日线，返回 (symbol, name, docs, error)"""
    symbol, name, code, start = args
    try:
        bs_code = get_bs_code(code)
        rs = bs.query_history_k_data_plus(
            bs_code,
            "date,code,open,high,low,close,preclose,volume,amount",
            start, END_DATE,
            frequency="d",
            adjustflag="2",
        )
        rows = []
        while rs.next():
            row = rs.get_row_data()
            if row[0] is not None:
                rows.append(row)
    except Exception as e:
        return (symbol, name, [], str(e))

    docs = [build_doc(r, symbol, code) for r in rows]
    docs = [d for d in docs if d is not None]
    return (symbol, name, docs, None)


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
            adjustflag="2",
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
        all_stocks = []
        while rs.next():
            row = rs.get_row_data()
            code = row[0]       # sh.600000 或 sz.000001
            code_name = row[1]  # 股票名称
            status = row[2]     # 1=正常交易
            raw_code = code.split(".")[1] if "." in code else code
            all_stocks.append((raw_code, code_name, status))
        print(f"   baostock 返回 {len(all_stocks)} 只股票")

        # 4. 更新 stock_info_new（只插入新股票，不覆盖已有）
        print(f"\n[4/5] 更新 stock_info_new...")
        existing_symbols = set()
        for s in db.stock_info_new.find({"type": 0}, {"symbol": 1, "_id": 0}):
            existing_symbols.add(s["symbol"])

        new_info = []
        for raw_code, code_name, status in all_stocks:
            if not code_name or code_name == "":
                code_name = raw_code
            symbol = get_symbol(raw_code)
            if symbol not in existing_symbols and status == "1":
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

        # 5. 批量并行下载股票日线
        print(f"\n[5/5] 并行下载股票日线（{DOWNLOAD_WORKERS} 线程）...")
        # 确定哪些股票需要下载
        to_download = []
        for raw_code, code_name, status in all_stocks:
            if status != "1":
                continue
            symbol = get_symbol(raw_code)
            latest = db.stock_market.find_one({"symbol": symbol}, sort=[("date", -1)])
            if latest and latest["date"] >= END_DATE_C:
                continue  # 已是最新
            start = START_DATE if not latest else latest["date"]
            to_download.append((symbol, code_name or raw_code, raw_code, start))

        total = len(to_download)
        print(f"   需下载: {total} 只（跳过已补齐的）")

        stock_filled = 0
        done = 0
        t0 = time.time()

        if total > 0:
            with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
                fut_map = {pool.submit(download_stock, t): t for t in to_download}
                for fut in as_completed(fut_map):
                    task = fut_map[fut]
                    symbol, name, docs, err = fut.result()
                    done += 1

                    if err:
                        print(f"   × [{done}/{total}] {symbol} ({name}) 失败: {err}")
                        continue

                    if docs:
                        # 过滤已存在的日期
                        existing_dates = set()
                        for d in db.stock_market.find({"symbol": symbol}, {"date": 1, "_id": 0}):
                            existing_dates.add(d["date"])
                        new_docs = [d for d in docs if d["date"] not in existing_dates]

                        if new_docs:
                            try:
                                db.stock_market.insert_many(new_docs, ordered=False)
                                stock_filled += len(new_docs)
                            except Exception as e:
                                print(f"   × [{done}/{total}] {symbol} 写入失败: {e}")

                    # 进度显示
                    if done % 100 == 0 or done == total:
                        elapsed = time.time() - t0
                        rate = done / elapsed if elapsed > 0 else 0
                        print(f"   √ [{done}/{total}] 已新增 {stock_filled} 条 | "
                              f"{rate:.0f} 只/秒")
        else:
            print(f"   - 全部股票已是最新，无需下载")

        # 6. 下载指数日线
        print(f"\n  下载指数日线...")
        idx_filled = 0
        for code, name in INDEX_LIST:
            symbol = f"{code}.SH"
            latest = db.index_daily_price.find_one({"symbol": symbol}, sort=[("date", -1)])
            if latest and latest["date"] >= END_DATE_C:
                print(f"   - {symbol} ({name}) 已最新")
                continue

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
