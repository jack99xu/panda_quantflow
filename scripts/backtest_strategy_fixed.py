from panda_backtest.api.api import *
from panda_backtest.api.stock_api import *
import panda_data


def initialize(context):
    """初始化：使用平台默认可用标的做轮动（示例：000001.SZ 与 600000.SH）"""
    # 股票账号（回测环境默认账号是 "8888"）
    context.stock_account = "8888"

    # 注意：平台回测环境里，ETF/指数是否可用不确定
    # 但 000001.SZ（平安银行）、600000.SH（浦发银行）在官方文档和 demo 里经常出现，基本确定有数据且可交易
    # 不使用 sub_stock()— 回测引擎的 ReverseOperationProxy 没有这个方法
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

    # ============ 账号检查 ============
    stock_account = context.stock_account_dict.get(context.stock_account)
    if stock_account is None:
        if not context.debug_logged:
            print(f"[回测警告] 股票账号 {context.stock_account} 不存在，"
                  f"可用账号: {list(context.stock_account_dict.keys())}")
            context.debug_logged = True
        return

    # ============ 更新价格历史 ============
    valid_symbols = []
    for symbol in context.symbol_list:
        try:
            b = data[symbol]
        except Exception:
            if symbol not in context.__dict__.setdefault("_warned_symbols", set()):
                print(f"[回测警告] {symbol} 行情数据不可用（可能未下载到 stock_market 中）")
                context._warned_symbols.add(symbol)
            continue
        if b is None or b.close is None or b.close <= 0:
            if symbol not in context.__dict__.setdefault("_warned_symbols", set()):
                print(f"[回测警告] {symbol} 行情数据为空（close={getattr(b, 'close', None)}）")
                context._warned_symbols.add(symbol)
            continue

        context.price_history.setdefault(symbol, []).append(float(b.close))
        if len(context.price_history[symbol]) > context.mom_lookback + 2:
            context.price_history[symbol].pop(0)
        valid_symbols.append(symbol)

    if len(valid_symbols) < 2:
        if not context.debug_logged:
            print(f"[回测警告] 有效标的不足2个（{len(valid_symbols)}），跳过交易，"
                  f"数据状态: {[(s, len(context.price_history.get(s, []))) for s in context.symbol_list]}")
            context.debug_logged = True
        return

    # ============ 计算动量 ============
    momentum_dict = {}
    for symbol in context.symbol_list:
        momentum = _calc_momentum(context.price_history[symbol], context.mom_lookback)
        if momentum is not None:
            momentum_dict[symbol] = momentum

    if len(momentum_dict) < 2:
        if not context.debug_logged:
            print(f"[回测警告] 动量计算不足，无法轮动")
            context.debug_logged = True
        return

    # 选择动量更强的
    sorted_by_momentum = sorted(momentum_dict.items(), key=lambda x: x[1], reverse=True)
    best_symbol = sorted_by_momentum[0][0]

    if best_symbol != context.current_symbol:
        # 卖出旧持仓（如果有）
        if context.current_symbol is not None:
            current_position = stock_account.positions.get(context.current_symbol)
            if current_position and current_position.quantity > 0:
                order_shares(context.stock_account, context.current_symbol,
                             -current_position.quantity)

        # 计算可买股数（按收盘价，向下取整到 100 股）
        total_cash = stock_account.cash
        best_bar = data[best_symbol]
        if best_bar is None or best_bar.close is None or best_bar.close <= 0:
            return
        target_value = total_cash * context.target_stock_ratio
        shares = int(target_value / best_bar.close / 100) * 100
        if shares > 0:
            order_shares(context.stock_account, best_symbol, shares)
            context.current_symbol = best_symbol
