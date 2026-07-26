import time
import logging

from collections import defaultdict

from panda_backtest.backtest_common.constant.string_constant import STOCK_ORDER_FAILED_MESSAGE, SYMBOL_CAN_NOT_CROSS
from panda_backtest.backtest_common.data.order.common.work_order_list import WorkOrderList
from panda_backtest.backtest_common.exchange.stock.back_test.etf_split_manager import ETFSplitManager
from panda_backtest.backtest_common.order.common.order_quotation_verify import OrderQuotationVerify

from panda_backtest.backtest_common.exchange.stock.back_test.dividend_manager import DividendManager
from panda_backtest.backtest_common.data.stock.stock_info_map import StockInfoMap
from panda_backtest.backtest_common.model.result.panda_backtest_trade import PandaBacktestTrade, MARKET, OPEN
from panda_backtest.backtest_common.data.quotation.quotation_data import QuotationData
from panda_backtest.backtest_common.order.stock.common.stock_order_builder import StockOrderBuilder
from panda_backtest.backtest_common.system.context.core_context import CoreContext
from panda_backtest.backtest_common.model.result.order import Order, ACTIVE, REJECTED, LIMIT, SIDE_BUY, FILLED, CANCELLED, \
    PartTradedNotQueueing, CLOSE
from panda_backtest.backtest_common.system.event.event import ConstantEvent, Event

class StockExchange(object):
    def __init__(self, quotation_mongo_db):
        self.context = CoreContext.get_instance()
        self.work_order = WorkOrderList()
        self.all_order = defaultdict(dict)
        self.order_verify_chain = list()
        self.order_count = 0
        self.trade_count = 0
        self.quotation_mongo_db = quotation_mongo_db
        self.dividend_manager = DividendManager(self.quotation_mongo_db)
        self.stock_info_map = StockInfoMap(self.quotation_mongo_db)
        self.rate = 0.0008
        self.stock_order_builder = StockOrderBuilder(self.stock_info_map)
        self.etf_split_manager = ETFSplitManager(self.quotation_mongo_db)

    def add_order_verify(self, order_verify):
        self.order_verify_chain.append(order_verify)

    def init_event(self):
        event_bus = self.context.event_bus
        event_bus.register_handle(ConstantEvent.SYSTEM_STOCK_ORDER_CROSS, self.sys_cross_order)

    def day_start(self):
        """
        每日开始，对股票进行分红
        """
        strategy_context = self.context.strategy_context
        if strategy_context.is_trade_date():
            self.dividend_manager.start_dividend()

    def end_date(self):
        """
        每日结束，对未成交订单进行撤单
        :return:
        """
        for order in self.work_order.get_order_list():
            self.cancel_order(order.account, order.order_id)

        self.work_order.clear()
        self.all_order.clear()

    def etf_split(self):
        strategy_context = self.context.strategy_context
        if strategy_context.is_trade_date():
            self.etf_split_manager.get_etf_split()

    def insert_order(self, account, order_dict):
        event_bus = self.context.event_bus
        strategy_context = self.context.strategy_context
        run_info = strategy_context.run_info

        order_result = self.stock_order_builder.init_stock_order(account, order_dict)

        if order_result.status == REJECTED:
            # self.log_order_error(order_result)
            event = Event(ConstantEvent.SYSTEM_STOCK_RTN_ORDER, order=order_result)
            event_bus.publish_event(event)
            event = Event(ConstantEvent.SYSTEM_STOCK_ORDER_CANCEL, order=order_result)
            event_bus.publish_event(event)
            return [order_result]

        for chain_item in self.order_verify_chain:
            if not chain_item.can_submit_order(account, order_result):
                order_result.status = REJECTED
                # self.log_order_error(order_result)
                self.all_order[account][order_result.order_id] = order_result
                event = Event(ConstantEvent.SYSTEM_STOCK_RTN_ORDER, order=order_result)
                event_bus.publish_event(event)
                event = Event(ConstantEvent.SYSTEM_STOCK_ORDER_CANCEL, order=order_result)
                event_bus.publish_event(event)
                self.all_order[account][order_result.order_id] = order_result
                return [order_result]
        event = Event(ConstantEvent.SYSTEM_STOCK_RTN_ORDER, order=order_result)
        event_bus.publish_event(event)
        self.all_order[account][order_result.order_id] = order_result
        self.work_order.add_order(order_result)

        # 订阅
        event_bus = self.context.event_bus
        event = Event(
            ConstantEvent.SYSTEM_STOCK_QUOTATION_START_SUB,
            symbol_list=[order_result.order_book_id],
            sub_type=1)
        event_bus.publish_event(event)

        # strategy_context.sub_stock_symbol([order_result.order_book_id])

        # TODO
        # if run_info.matching_type == 0:
        #     self.handle_cross_order(order_result)
        self.handle_cross_order(order_result)

        return [order_result]

    def cancel_order(self, account, order_id):
        event_bus = self.context.event_bus

        order_list = self.work_order.get_order_list(order_id)
        if len(order_list) == 0:
            return False

        order = order_list[0]
        if order.status == CANCELLED or order.status == FILLED or \
                order.status == REJECTED:
            return False
        else:
            order.status = CANCELLED

            event = Event(ConstantEvent.SYSTEM_STOCK_RTN_ORDER, order=order)
            event_bus.publish_event(event)

            event = Event(ConstantEvent.SYSTEM_STOCK_ORDER_CANCEL, order=order)
            event_bus.publish_event(event)
            self.work_order.remove_order(order_id)

            # 取消订阅
            if order.order_book_id not in self.work_order.symbol_index.keys():
                event_bus = self.context.event_bus
                event = Event(
                    ConstantEvent.SYSTEM_STOCK_QUOTATION_START_UN_SUB,
                    symbol_list=[order.order_book_id],
                    sub_type=1)
                event_bus.publish_event(event)

            return True

    def sys_cross_order(self, bar_data=None):
        if bar_data is None or bar_data.symbol == '':
            return
        order_list = self.work_order.get_order_list(symbol=bar_data.symbol)
        for order in order_list:
            self.handle_cross_order(order)

    def handle_cross_order(self, order):
        bar_dict = QuotationData.get_instance().bar_dict
        bar_data = bar_dict[order.order_book_id]
        if not bar_data :
            self.trade_cross_failed(order, "开仓" if order.effect != CLOSE else "平仓",
                                    "买入" if order.side == SIDE_BUY else "卖出")
            return
        strategy_context = self.context.strategy_context

        run_info = strategy_context.run_info

        slippage = run_info.slippage

        if run_info.run_strategy_type == 0:
            bar_data_source = bar_dict.bar_data_source
            limit_price_obj = bar_data_source.get_stock_daily_bar(order.order_book_id, strategy_context.trade_date)
            # DailyQuotationData 可能没有 limit_up/limit_down 字段（baostock 数据源不提供）
            # 缺失时使用安全默认值：涨停不限制（99999），跌停不限制（0）
            if limit_price_obj is None:
                limit_up = 99999
                limit_down = 0
            else:
                limit_up = getattr(limit_price_obj, 'limit_up', 99999)
                limit_down = getattr(limit_price_obj, 'limit_down', 0)
            if run_info.matching_type == 1:
                hq_price = bar_data.open
            else:
                hq_price = bar_data.close
        else:
            limit_up = bar_data.limit_up
            limit_down = bar_data.limit_down
            hq_price = bar_data.last

        if order.side == SIDE_BUY:
            hq_price = hq_price * (1 + slippage)
        else:
            hq_price = hq_price * (1 - slippage)

        buy_cross_price = hq_price  # 若买入方向限价单价格高于该价格，则会成交
        sell_cross_price = hq_price  # 若卖出方向限价单价格低于该价格，则会成交

        if order.price_type == LIMIT:  # 限价单
            if order.side == SIDE_BUY:
                cross = order.price >= buy_cross_price > 0
                trade_price = buy_cross_price
                trade_price = min(trade_price, limit_up)
            else:
                cross = order.price <= sell_cross_price and sell_cross_price > 0
                trade_price = sell_cross_price
                trade_price = max(trade_price, limit_down)
        else:
            cross = True
            if order.side == SIDE_BUY:
                trade_price = hq_price
                trade_price = min(trade_price, limit_up)
            else:
                trade_price = hq_price
                trade_price = max(trade_price, limit_down)

        if order.side == SIDE_BUY:
            order_side = '买入'
        else:
            order_side = '卖出'
        if order.effect == CLOSE:
            order_effect = '平仓'
        else:
            order_effect = '开仓'
        event_bus = self.context.event_bus

        instrument_info = self.stock_info_map[order.order_book_id]

        if cross and bar_data.volume > 0:
            if order.quantity > bar_data.volume:
                if instrument_info['type'] == '科创板':
                    if int(bar_data.volume / 200) * 200 <= 0:
                        self.trade_cross_failed(order, order_effect, order_side)
                        return
                    order.filled_quantity = int(bar_data.volume / 200) * 200
                else:
                    if int(bar_data.volume / 100) * 100 <= 0:
                        self.trade_cross_failed(order, order_effect, order_side)
                        return
                    order.filled_quantity = int(bar_data.volume / 100) * 100

                order.unfilled_quantity = int(order.quantity - order.filled_quantity)
                order.cur_filled_quantity = order.filled_quantity
                order.status = PartTradedNotQueueing
                order.message = STOCK_ORDER_FAILED_MESSAGE % (order.order_book_id, str(order.unfilled_quantity),
                                                              order.account, order_effect, order_side,
                                                              order.order_id,
                                                              SYMBOL_CAN_NOT_CROSS)
            else:
                order.filled_quantity = order.quantity
                order.unfilled_quantity = 0
                order.cur_filled_quantity = order.quantity
                order.status = FILLED

            # 推送成交数据
            self.trade_count += 1  # 成交编号自增1
            trade_id = str(self.trade_count)
            trade = PandaBacktestTrade()
            trade.type = 0
            trade.back_id = run_info.run_id
            trade.now_system_order = order.now_system_order
            trade.client_id = order.client_id
            trade.account_id = order.account
            trade.contract_code = order.order_book_id
            trade.contract_name = order.order_book_name
            trade.stock_type = order.stock_type
            trade.trade_id = trade_id
            trade.order_id = order.order_id
            trade.direction = order.effect
            trade.price = trade_price
            trade.business = order.side
            trade.volume = int(order.cur_filled_quantity)
            trade.gmt_create = strategy_context.now
            trade.trade_date = strategy_context.trade_date
            trade.gmt_create_time = strategy_context.hms
            trade.order_remark = order.remark
            rate = trade.price * trade.volume * self.rate * run_info.commission_multiplier
            # 佣金费不足5元，按5元算
            if rate < 5:
                rate = 5
            if order.side == SIDE_BUY:
                trade.cost = rate
            else:
                trade.cost = rate + trade.price * trade.volume * 0.001

            # 推送委托数据
            self.work_order.remove_order(order.order_id)
            event = Event(ConstantEvent.SYSTEM_STOCK_RTN_ORDER, order=order)
            event_bus.publish_event(event)

            event = Event(ConstantEvent.SYSTEM_STOCK_RTN_TRADE, trade=trade)
            event_bus.publish_event(event)

            if order.status == PartTradedNotQueueing:
                event = Event(ConstantEvent.SYSTEM_STOCK_ORDER_CANCEL, order=order)
                event_bus.publish_event(event)

            # 取消订阅
            if order.order_book_id not in self.work_order.symbol_index.keys():
                event_bus = self.context.event_bus
                event = Event(
                    ConstantEvent.SYSTEM_STOCK_QUOTATION_START_UN_SUB,
                    symbol_list=[order.order_book_id],
                    sub_type=1)
                event_bus.publish_event(event)

        else:
            self.trade_cross_failed(order, order_effect, order_side)

    def trade_cross_failed(self, order, order_effect, order_side):
        event_bus = self.context.event_bus
        order.filled_quantity = 0
        order.unfilled_quantity = order.quantity
        order.status = CANCELLED
        order.message = STOCK_ORDER_FAILED_MESSAGE % (order.order_book_id, str(order.quantity),
                                                      order.account, order_effect, order_side,
                                                      order.order_id,
                                                      SYMBOL_CAN_NOT_CROSS)
        event = Event(ConstantEvent.SYSTEM_STOCK_RTN_ORDER, order=order)
        event_bus.publish_event(event)

        event = Event(ConstantEvent.SYSTEM_STOCK_ORDER_CANCEL, order=order)
        event_bus.publish_event(event)

        # 取消订阅
        if order.order_book_id not in self.work_order.symbol_index.keys():
            event_bus = self.context.event_bus
            event = Event(
                ConstantEvent.SYSTEM_STOCK_QUOTATION_START_UN_SUB,
                symbol_list=[order.order_book_id],
                sub_type=1)
            event_bus.publish_event(event)

    def restore_read(self, redis_client, key, hkey):
        self.work_order.restore_read(redis_client, key, hkey)

    def restore_save(self, redis_client, key, hkey):
        self.work_order.restore_save(redis_client, key, hkey)
