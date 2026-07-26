#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time   : 2020/9/16 20:19
# @Author : wlb
# @File   : stock_order_builder.py
# @desc   :
from panda_backtest.backtest_common.constant.string_constant import STOCK_ORDER_FAILED_MESSAGE, STOCK_GEM_QUANTITY_NOT_RIGHT, \
STOCK_QUANTITY_NOT_RIGHT, STOCK_HAD_NO_INFO, SYMBOL_NO_QUOTATION, SYMBOL_PRICE_NOT_RIGHT
from panda_backtest.backtest_common.model.result.order import Order, ACTIVE, OPEN, SIDE_BUY, LIMIT, MARKET, REJECTED, CLOSE, SIDE_SELL
from panda_backtest.backtest_common.order.common.order_quotation_verify import OrderQuotationVerify
from panda_backtest.backtest_common.system.context.core_context import CoreContext
from panda_backtest.util.log.remote_log_factory import RemoteLogFactory
class StockOrderBuilder(object):

    def __init__(self, stock_info_map):
        self.context = CoreContext.get_instance()
        self.order_count = 0
        self.stock_info_map = stock_info_map
        self.order_quotation_verify = OrderQuotationVerify()
        self.rate = 0.0008

    def init_stock_order(self, account, order_dict):
        self.order_count += 1
        instrument_info = self.stock_info_map[order_dict['symbol']]

        strategy_context = self.context.strategy_context
        run_info = strategy_context.run_info

        order_result = Order()
        order_result.account = account
        order_result.status = ACTIVE
        order_result.order_id = str(self.order_count)
        order_result.order_book_id = order_dict['symbol']
        order_result.client_id = order_dict.get('client_id', run_info.run_id)
        order_result.risk_control_id = order_dict.get('risk_control_id', None)
        order_result.remark = order_dict.get('remark', None)
        order_result.now_system_order = order_dict.get('now_system_order', 1)
        order_result.datetime = strategy_context.trade_time
        order_result.side = order_dict['side']
        order_result.effect = order_dict['effect']
        order_result.order_book_name = instrument_info['name']
        stock_type = instrument_info['type']
        if stock_type == 1:
            order_result.stock_type = 3
        elif stock_type == 2:
            order_result.stock_type = 2
        else:
            order_result.stock_type = 0
        order_result.retry_num = order_dict.get('retry_num', 0)

        order_insert_type = order_dict['order_insert_type']
        if order_insert_type == 0:
            quantity = order_dict['quantity']
        else:
            quantity = min(
                int(order_dict['amount'] / order_result.price * (1 + run_info.commission_multiplier * self.rate)),
                int(order_dict['amount'] - 5 / order_result.price)
            )

        order_result.quantity = int(quantity)
        order_result.unfilled_quantity = int(quantity)

        if instrument_info['name'] == '未知' or 'type' not in instrument_info.keys():
            if order_result.side == SIDE_BUY:
                order_side = '买入'
                order_effect = '开仓'
            else:
                order_side = '卖出'
                order_effect = '平仓'
            sr_logger = RemoteLogFactory.get_sr_logger()
            sr_logger.error(STOCK_ORDER_FAILED_MESSAGE % (order_result.order_book_id, str(order_result.quantity),
                                                          account, order_effect, order_side, order_result.order_id,
                                                          STOCK_HAD_NO_INFO))
            order_result.quantity = 0
            order_result.status = REJECTED
            return order_result

        if order_dict['price_type'] == LIMIT:
            order_result.price = order_dict['price']
            order_result.price_type = LIMIT
        else:
            hq_price = self.order_quotation_verify.get_order_market_price(order_result)
            order_result.price_type = MARKET
            order_result.price = hq_price

            if hq_price == 0:
                if order_result.side == SIDE_BUY:
                    order_side = '买入'
                else:
                    order_side = '卖出'
                if order_result.effect == CLOSE:
                    order_effect = '平仓'
                else:
                    order_effect = '开仓'
                order_result.message = STOCK_ORDER_FAILED_MESSAGE % (
                    order_result.order_book_id, str(order_result.quantity),
                    order_result.account, order_effect, order_side,
                    order_result.order_id,
                    SYMBOL_NO_QUOTATION)
                order_result.status = REJECTED
                return order_result

        if order_result.price <= 0:
            if order_result.side == SIDE_BUY:
                order_side = '买入'
            else:
                order_side = '卖出'
            if order_result.effect == CLOSE:
                order_effect = '平仓'
            else:
                order_effect = '开仓'
            order_result.message = STOCK_ORDER_FAILED_MESSAGE % (order_result.order_book_id, str(order_result.quantity),
                                                                 order_result.account, order_effect, order_side,
                                                                 order_result.order_id,
                                                                 SYMBOL_PRICE_NOT_RIGHT)
            order_result.status = REJECTED
            return order_result

        self.check_quantity(account, quantity, instrument_info['type'], order_result)

        if order_result.status == REJECTED:
            return order_result

        rate = order_result.price * order_result.quantity * run_info.commission_multiplier * self.rate
        # 佣金费不足5元，按5元算
        if rate < 5:
            rate = 5
        if order_result.side == SIDE_BUY:
            order_result.transaction_cost = rate
        else:
            order_result.transaction_cost = rate + order_result.price * order_result.quantity * 0.0005

        return order_result

    def check_quantity(self, account, quantity, instrument_type, order_result):
        if instrument_type == '科创板':
            if quantity < 200:
                if order_result.side == SIDE_BUY:
                    order_side = '买入'
                    order_effect = '开仓'
                else:
                    order_side = '卖出'
                    order_effect = '平仓'

                order_result.message = STOCK_ORDER_FAILED_MESSAGE % (
                    order_result.order_book_id, str(order_result.quantity),
                    account, order_effect, order_side, order_result.order_id,
                    STOCK_GEM_QUANTITY_NOT_RIGHT
                    % (str(quantity)))
                order_result.status = REJECTED
                order_result.quantity = 0
                return order_result
            else:
                order_result.quantity = int(quantity / 1) * 1
                return order_result
        else:
            if quantity < 100:
                if quantity > 0:
                    if order_result.side == SIDE_SELL and \
                            quantity % 100 != 0 and \
                            quantity == self.context.strategy_context.stock_account_dict[account].positions[
                        order_result.order_book_id].sellable:
                        quantity = quantity
                        order_result.quantity = quantity
                        return order_result

                if order_result.side == SIDE_BUY:
                    order_side = '买入'
                    order_effect = '开仓'
                else:
                    order_side = '卖出'
                    order_effect = '平仓'

                order_result.message = STOCK_ORDER_FAILED_MESSAGE % (
                    order_result.order_book_id, str(order_result.quantity),
                    account, order_effect, order_side, order_result.order_id,
                    STOCK_QUANTITY_NOT_RIGHT
                    % (str(quantity)))
                order_result.status = REJECTED
                order_result.quantity = 0
                return order_result
            else:
                quantity = int(quantity / 100) * 100
                order_result.quantity = quantity
                return order_result
