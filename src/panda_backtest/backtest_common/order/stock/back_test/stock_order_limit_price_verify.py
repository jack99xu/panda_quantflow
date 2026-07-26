from panda_backtest.backtest_common.constant.string_constant import STOCK_ORDER_FAILED_MESSAGE, \
ORDER_PRICE_TOO_HIGH, ORDER_PRICE_TOO_LOW, SYMBOL_LIMIT_HIGH, SYMBOL_LIMIT_LOW, SYMBOL_LIMIT_NOT_EXIST
from panda_backtest.backtest_common.constant.strategy_constant import SIDE_BUY, SIDE_SELL, CLOSE
from panda_backtest.backtest_common.data.quotation.quotation_data import QuotationData
from panda_backtest.backtest_common.system.context.core_context import CoreContext
from panda_backtest.backtest_common.order.order_verify import OrderVerify
from panda_backtest.util.log.remote_log_factory import RemoteLogFactory
class StockOrderLimitPriceVerify(OrderVerify):

    def __init__(self):
        self.context = CoreContext.get_instance()

    def can_submit_order(self, account, order_result):
        sr_logger = RemoteLogFactory.get_sr_logger()
        strategy_context = self.context.strategy_context
        run_info = strategy_context.run_info
        trade_date = strategy_context.trade_date

        bar_dict = QuotationData.get_instance().bar_dict
        bar_data_source = bar_dict.bar_data_source
        bar = bar_dict[order_result.order_book_id]

        result_dict = dict()
        result_dict['status'] = 0
        limit_price_obj = bar_data_source.get_stock_daily_bar(order_result.order_book_id, trade_date)

        # 行情数据中没有 limit_up/limit_down 字段时跳过涨跌停校验
        # （baostock 数据源不提供该字段，不影响市价单成交）
        limit_up = getattr(limit_price_obj, 'limit_up', None)
        limit_down = getattr(limit_price_obj, 'limit_down', None)
        # 行情数据中没有 limit_up/limit_down 时跳过涨跌停校验
        # （baostock 不提供该字段，不影响市价单成交）
        if limit_up is None or limit_down is None:
            return True

        if run_info.matching_type == 0:
            jz_price = bar.close
        else:
            jz_price = bar.open

        if order_result.side == SIDE_BUY:
            order_side = '买入'
        else:
            order_side = '卖出'
        if order_result.effect == CLOSE:
            order_effect = '平仓'
        else:
            order_effect = '开仓'
        if jz_price == 0:
            return False

        if jz_price >= limit_up and order_result.side == SIDE_BUY:
            order_result.message = STOCK_ORDER_FAILED_MESSAGE % (order_result.order_book_id, str(order_result.quantity),
                                                                 account, order_effect, order_side,
                                                                 order_result.order_id,
                                                                 SYMBOL_LIMIT_HIGH)

            return False
        if jz_price <= limit_down and order_result.side == SIDE_SELL:
            order_result.message = STOCK_ORDER_FAILED_MESSAGE % (order_result.order_book_id, str(order_result.quantity),
                                                                 account, order_effect, order_side,
                                                                 order_result.order_id,
                                                                 SYMBOL_LIMIT_LOW)
            return False

        if order_result.price > limit_up:
            order_result.message = STOCK_ORDER_FAILED_MESSAGE % (order_result.order_book_id, str(order_result.quantity),
                                                                 account, order_effect, order_side,
                                                                 order_result.order_id,
                                                                 ORDER_PRICE_TOO_HIGH %
                                                                 (str(order_result.price),
                                                                  str(limit_up)))
            return False
        if order_result.price < limit_down:
            order_result.message = STOCK_ORDER_FAILED_MESSAGE % (order_result.order_book_id, str(order_result.quantity),
                                                                 account, order_effect, order_side,
                                                                 order_result.order_id,
                                                                 ORDER_PRICE_TOO_LOW %
                                                                 (str(order_result.price),
                                                                  str(limit_down)))
            return False

        return True
