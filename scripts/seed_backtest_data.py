"""
开机自动填充回测行情数据到 MongoDB
使用 baostock（免费、免注册）下载 A 股日线数据
数据持久化不依赖外部文件，每次重启自动生成
"""

import baostock as bs
import pymongo
import os
import sys
import time

# 从环境变量读取 MongoDB 配置，与项目 config.py 一致
MONGO_HOST = os.getenv("MONGO_URI", "127.0.0.1:27017")
MONGO_USER = os.getenv("MONGO_USER", "panda")
MONGO_PASS = os.getenv("MONGO_PASSWORD", "panda")
MONGO_AUTH = os.getenv("MONGO_AUTH_DB", "admin")
MONGO_DB = os.getenv("MONGO_DB", "panda")

# 回测数据时间范围
START_DATE = "2024-10-01"
END_DATE = "2025-01-10"

# 基准指数（用于计算超额收益）
INDEX_LIST = [
    ("000001", "上证指数"),
    ("000300", "沪深300"),
    ("000500", "中证500"),
    ("001000", "中证1000"),
]

# 回测用测试股票（覆盖面：深主板、沪主板、创业板）
STOCK_LIST = [
    ("000001", "平安银行"),
    ("000002", "万科A"),
    ("000333", "美的集团"),
    ("000651", "格力电器"),
    ("000858", "五粮液"),
    ("002594", "比亚迪"),
    ("300750", "宁德时代"),
    ("600000", "浦发银行"),
    ("600036", "招商银行"),
    ("600519", "贵州茅台"),
    ("601318", "中国平安"),
    ("000568", "泸州老窖"),
    ("002415", "海康威视"),
]


def get_symbol(code: str) -> str:
    """A股 code → symbol（交易所后缀，项目格式）"""
    if code.startswith("6"):
        return f"{code}.SH"
    else:
        return f"{code}.SZ"


def get_baostock_code(code: str) -> str:
    """A股 code → baostock 格式（带交易所前缀）"""
    if code.startswith("6"):
        return f"sh.{code}"
    else:
        return f"sz.{code}"


def wait_for_mongodb(max_retries=30):
    """等待 MongoDB 就绪"""
    uri = f"mongodb://{MONGO_USER}:{MONGO_PASS}@{MONGO_HOST}/{MONGO_AUTH}"
    for i in range(max_retries):
        try:
            client = pymongo.MongoClient(uri, serverSelectionTimeoutMS=2000)
            client.admin.command("ping")
            client.close()
            return True
        except Exception:
            print(f"  等待 MongoDB 就绪 ({i+1}/{max_retries})...")
            time.sleep(2)
    return False


def main():
    print("=" * 60)
    print("回测行情数据预加载")
    print("=" * 60)

    # 1. 等 MongoDB 就绪
    print("\n[0/5] 等待 MongoDB...")
    if not wait_for_mongodb():
        print("❌ MongoDB 未就绪，跳过数据预加载")
        sys.exit(1)
    print("   MongoDB 就绪 ✓")

    # 2. 连接 MongoDB
    uri = f"mongodb://{MONGO_USER}:{MONGO_PASS}@{MONGO_HOST}/{MONGO_AUTH}"
    client = pymongo.MongoClient(uri)
    db = client[MONGO_DB]

    # 3. 检查是否已有数据（幂等：全部就绪则跳过）
    stock_count = db.stock_market.count_documents({})
    index_count = db.index_daily_price.count_documents({})
    if stock_count > 0 and index_count > 0:
        print(f"\n✅ 行情数据已存在（股票 {stock_count} + 指数 {index_count}），跳过预加载")
        client.close()
        return

    # 4. 连接 baostock
    print("\n[1/5] 连接 baostock 数据源...")
    try:
        lg = bs.login()
        if lg.error_code != "0":
            print(f"❌ baostock 登录失败: {lg.error_msg}")
            client.close()
            return
    except Exception as e:
        print(f"❌ baostock 连接异常: {e}")
        client.close()
        return
    print("   baostock 就绪 ✓")

    try:
        # ---- 交易日历 ----
        print("\n[2/5] 生成交易日历 (trade_calendar)...")
        rs = bs.query_trade_dates(START_DATE, END_DATE)
        cal_docs = []
        while rs.next():
            row = rs.get_row_data()
            # row[0] = "2024-10-01", row[1] = "1" (交易日) / "0"
            nat_date = int(row[0].replace("-", ""))
            is_trade = 1 if row[1] == "1" else 0
            for ex in ["SH", "SZ"]:
                cal_docs.append({
                    "nature_date": nat_date,
                    "is_trade": is_trade,
                    "exchange": ex,
                })
        if cal_docs:
            db.trade_calendar.insert_many(cal_docs, ordered=False)
            trade_days = sum(1 for d in cal_docs if d["is_trade"] == 1) // 2  # SH+SZ 重复计数
            print(f"   √ {len(cal_docs)} 条（交易日: {trade_days} 天）")

        # ---- 股票 & 指数基本信息 ----
        print("\n[3/5] 生成股票和指数信息 (stock_info_new)...")
        info_docs = []
        for code, name in STOCK_LIST:
            symbol = get_symbol(code)
            # type: 0=普通股票
            info_docs.append({
                "symbol": symbol,
                "name": name,
                "type": 0,
            })
        for code, name in INDEX_LIST:
            symbol = f"{code}.SH"
            # type: 1=指数 → 查询 index_daily_price 集合
            info_docs.append({
                "symbol": symbol,
                "name": name,
                "type": 1,
            })
        if info_docs:
            db.stock_info_new.insert_many(info_docs, ordered=False)
            print(f"   √ {len(info_docs)} 条（股票 {len(STOCK_LIST)} + 指数 {len(INDEX_LIST)}）")

        # ---- 股票日线行情 ----
        print("\n[4/5] 下载股票日线数据 (stock_market)...")
        total_records = 0
        for code, name in STOCK_LIST:
            symbol = get_symbol(code)
            try:
                bs_code = get_baostock_code(code)
                rs = bs.query_history_k_data_plus(
                    bs_code,
                    "date,code,open,high,low,close,preclose,volume,amount",
                    START_DATE, END_DATE,
                    frequency="d",
                    adjustflag="2",  # 前复权
                )
            except Exception as e:
                print(f"   × {symbol} ({name}) 下载失败: {e}")
                continue

            docs = []
            while rs.next():
                row = rs.get_row_data()
                if row[0] is None:  # 跳过空行
                    continue
                date_str = row[0].replace("-", "")  # "20241001"
                try:
                    doc = {
                        "symbol": symbol,
                        "code": code,
                        "date": date_str,
                        "trade_date": date_str,
                        "open": float(row[2]) if row[2] else 0.0,
                        "high": float(row[3]) if row[3] else 0.0,
                        "low": float(row[4]) if row[4] else 0.0,
                        "close": float(row[5]) if row[5] else 0.0,
                        "preclose": float(row[6]) if row[6] else 0.0,
                        "volume": float(row[7]) if row[7] else 0.0,
                        "turnover": float(row[8]) if row[8] else 0.0,
                        "trade_status": "交易",
                    }
                    docs.append(doc)
                except (ValueError, TypeError, IndexError) as ex:
                    print(f"   跳过异常行: {row[:4]}... - {ex}")
                    continue

            if docs:
                try:
                    db.stock_market.insert_many(docs, ordered=False)
                    print(f"   √ {symbol} ({name}): {len(docs)} 条")
                    total_records += len(docs)
                except Exception as e:
                    print(f"   × {symbol} 写入失败: {e}")
            else:
                print(f"   - {symbol} ({name}): 无数据（可能已退市或停牌）")

        # ---- 指数日线行情（基准用） ----
        print("\n[5/5] 下载指数日线数据 (index_daily_price)...")
        index_records = 0
        for code, name in INDEX_LIST:
            symbol = f"{code}.SH"
            try:
                bs_code = f"sh.{code}"
                rs = bs.query_history_k_data_plus(
                    bs_code,
                    "date,code,open,high,low,close,preclose,volume,amount",
                    START_DATE, END_DATE,
                    frequency="d",
                    adjustflag="2",
                )
            except Exception as e:
                print(f"   × {symbol} ({name}) 下载失败: {e}")
                continue

            docs = []
            while rs.next():
                row = rs.get_row_data()
                if row[0] is None:
                    continue
                date_str = row[0].replace("-", "")
                try:
                    doc = {
                        "symbol": symbol,
                        "code": code,
                        "date": date_str,
                        "trade_date": date_str,
                        "open": float(row[2]) if row[2] else 0.0,
                        "high": float(row[3]) if row[3] else 0.0,
                        "low": float(row[4]) if row[4] else 0.0,
                        "close": float(row[5]) if row[5] else 0.0,
                        "preclose": float(row[6]) if row[6] else 0.0,
                        "volume": float(row[7]) if row[7] else 0.0,
                        "turnover": float(row[8]) if row[8] else 0.0,
                        "trade_status": "交易",
                    }
                    docs.append(doc)
                except (ValueError, TypeError, IndexError) as ex:
                    print(f"   跳过异常行: {row[:4]}... - {ex}")
                    continue

            if docs:
                try:
                    db.index_daily_price.insert_many(docs, ordered=False)
                    print(f"   √ {symbol} ({name}): {len(docs)} 条")
                    index_records += len(docs)
                except Exception as e:
                    print(f"   × {symbol} 写入失败: {e}")
            else:
                print(f"   - {symbol} ({name}): 无数据")

        print(f"\n{'=' * 60}")
        print(f"✅  数据预加载完成！共导入 {total_records} 条行情 + {index_records} 条指数")
        print(f"   股票: {len(STOCK_LIST)} 只, 指数: {len(INDEX_LIST)} 只, 日期: {START_DATE} ~ {END_DATE}")
        print(f"{'=' * 60}")

    except Exception as e:
        print(f"\n❌ 数据预加载异常: {e}")
        import traceback
        traceback.print_exc()
    finally:
        bs.logout()
        client.close()


if __name__ == "__main__":
    main()
