from panda_backtest.api.api import *
from panda_backtest.api.stock_api import *
import panda_data


def initialize(context):
    """初始化：使用平台默认可用标的做轮动（示例：000001.SZ 与 600000.SH）"""
    # 股票账户（回测环境默认账号）
    context.stock_account = "8888"

    # 注意：平台回测环境里，ETF/指数是否可用不确定
    # 但 000001.SZ（平安银行）、600000.SH（浦发银行）在官方文档和 demo 里经常出现，基本确定有数据且可交易
    context.symbol_list = ["000001.SZ", "600000.SH"]

    # 动量参数：过去N日收益率作为动量
    context.mom_lookback = 20

    # 目标仓位：总资金的比例
    context.target_stock_ratio = 1.0

    # 当前持仓股票（用于轮动）
    context.current_symbol = None

    # 价格历史缓存
    context.price_history = {symbol: [] for symbol in context.symbol_list}

    # 调试标记
    context.debug_logged = False

    # DEBUG：打印参数
    print(f"[DEBUG] mom_lookback={context.mom_lookback}")
    print(f"[DEBUG] target_stock_ratio={context.target_stock_ratio}")
    print(f"[DEBUG] stock_account={context.stock_account}")


def _calc_momentum(prices, lookback):
    if len(prices) < lookback + 1:
        return None
    p0 = prices[-lookback - 1]
    p1 = prices[-1]
    if p0 <= 0:
        return None
    return (p1 / p0) - 1.0


def handle_data(context, data):
    """在两只确定可交易的股票之间做短期动量轮动"""
    stock_account = context.stock_account_dict.get(context.stock_account)
    if stock_account is None:
        print(f"[DEBUG] {context.now} stock_account None, 跳过")
        return

    # 1）更新两个标的的价格历史
    valid_symbols = []
    for symbol in context.symbol_list:
        try:
            b = data[symbol]
        except Exception:
            if symbol not in context.__dict__.setdefault("_warned_symbols", set()):
                print(f"[DEBUG] {context.now} {symbol} data异常, 跳过")
                context._warned_symbols.add(symbol)
            continue
        if b is None or b.close is None or b.close <= 0:
            if symbol not in context.__dict__.setdefault("_warned_symbols", set()):
                print(f"[DEBUG] {context.now} {symbol} close={getattr(b, 'close', None)}, 跳过")
                context._warned_symbols.add(symbol)
            continue
        context.price_history.setdefault(symbol, []).append(float(b.close))
        if len(context.price_history[symbol]) > context.mom_lookback + 2:
            context.price_history[symbol].pop(0)
        valid_symbols.append(symbol)

    # ==== DEBUG：每日打印价格历史和动量 ====
    s1, s2 = context.symbol_list
    hist_s1 = context.price_history.get(s1, [])
    hist_s2 = context.price_history.get(s2, [])
    close_s1 = hist_s1[-1] if hist_s1 else None
    close_s2 = hist_s2[-1] if hist_s2 else None
    mom_s1 = _calc_momentum(hist_s1, context.mom_lookback)
    mom_s2 = _calc_momentum(hist_s2, context.mom_lookback)

    print(f"[DEBUG] {context.now} "
          f"close: {s1}={close_s1}, {s2}={close_s2} | "
          f"hist_len: {len(hist_s1)}, {len(hist_s2)} | "
          f"mom: {mom_s1}, {mom_s2} | "
          f"total_value={stock_account.total_value:.2f}, cash={stock_account.cash:.2f}")

    if len(valid_symbols) < 2:
        print(f"[DEBUG] {context.now} 有效标的={len(valid_symbols)} < 2, 跳过")
        return

    if mom_s1 is None or mom_s2 is None:
        print(f"[DEBUG] {context.now} 动量不足, 跳过")
        return

    # 3）选动量更高的标的
    target_symbol = s1 if mom_s1 >= mom_s2 else s2
    current_pos = stock_account.positions.get(context.current_symbol, None) if context.current_symbol else None
    current_qty = current_pos.quantity if current_pos else 0

    # 4）计算目标股数（全仓）
    total_value = stock_account.total_value
    try:
        target_bar = data[target_symbol]
    except Exception:
        print(f"[DEBUG] {context.now} 取{target_symbol}行情异常, 跳过")
        return
    if target_bar is None or target_bar.close is None or target_bar.close <= 0:
        return

    price = float(target_bar.close)
    raw_shares = int(total_value / price)
    target_shares = (raw_shares // 100) * 100

    print(f"[DEBUG] {context.now} 决策: best={target_symbol}, "
          f"当前持仓: {context.current_symbol}={current_qty}, "
          f"total_value={total_value:.2f}, price={price}, "
          f"raw_shares={raw_shares}, target_shares={target_shares}")

    if target_shares < 100:
        print(f"[DEBUG] {context.now} target_shares={target_shares} < 100, 跳过")
        return

    target_dict = {target_symbol: target_shares}

    # 5）使用组合目标下单
    try:
        target_stock_group_order(context.stock_account, target_dict)
        print(f"{context.now} 调仓 -> {target_symbol}, 目标股数: {target_shares}")
    except Exception as e:
        print(f"{context.now} 调仓异常: {e}")
        return

    context.current_symbol = target_symbol


def after_trading(context):
    stock_account = context.stock_account_dict.get(context.stock_account)
    if stock_account is None:
        return

    positions = stock_account.positions
    pos_info = []
    for symbol, pos in positions.items():
        if pos.quantity > 0:
            pos_info.append(f"{symbol}:{pos.quantity}")

    info_str = ",".join(pos_info)
    print(f"[{context.now}] 盘后 total_value={stock_account.total_value:.2f} cash={stock_account.cash:.2f} 持仓: {info_str}")
