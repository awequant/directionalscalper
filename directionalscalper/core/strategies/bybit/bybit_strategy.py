from colorama import Fore
from typing import Optional, Tuple, List, Dict, Union
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, ROUND_HALF_DOWN, ROUND_DOWN
import pandas as pd
import time
import math
import numpy as np
import random
import ta as ta
import uuid
import os
import uuid
import logging
import json
import threading
import traceback
import ccxt
import pytz
import sqlite3
from collections import defaultdict
from ..logger import Logger
from datetime import datetime, timedelta
from threading import Thread, Lock

from ...bot_metrics import BotDatabase

from directionalscalper.core.strategies.base_strategy import BaseStrategy

logging = Logger(logger_name="BybitBaseStrategy", filename="BybitBaseStrategy.log", stream=True)

class BybitStrategy(BaseStrategy):
    def __init__(self, exchange, config, manager, symbols_allowed=None):
        super().__init__(exchange, config, manager, symbols_allowed)
        self.grid_levels = {}
        self.linear_grid_orders = {}
        self.last_price = {}
        self.last_cancel_time = {}
        self.cancel_all_orders_interval = 240
        self.cancel_interval = 120
        self.order_refresh_interval = 120  # seconds
        self.last_order_refresh_time = 0
        self.last_grid_cancel_time = {}
        self.entered_grid_levels = {}
        self.filled_order_levels = {}
        self.filled_levels = {}
        self.active_grids = set()
        self.position_inactive_threshold = 150
        self.no_entry_signal_threshold = 150
        self.order_inactive_threshold = 150
        self.last_active_long_order_time = {}
        self.last_active_short_order_time = {}
        pass

    TAKER_FEE_RATE = 0.00055

    def get_market_data_with_retry(self, symbol, max_retries=5, retry_delay=5):
        for i in range(max_retries):
            try:
                return self.exchange.get_market_data_bybit(symbol)
            except Exception as e:
                if i < max_retries - 1:
                    print(f"Error occurred while fetching market data: {e}. Retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                else:
                    raise e
                
    def update_dynamic_amounts(self, symbol, total_equity, best_ask_price, best_bid_price):
        if symbol not in self.long_dynamic_amount or symbol not in self.short_dynamic_amount:
            long_dynamic_amount, short_dynamic_amount, _ = self.calculate_dynamic_amounts(symbol, total_equity, best_ask_price, best_bid_price)
            self.long_dynamic_amount[symbol] = long_dynamic_amount
            self.short_dynamic_amount[symbol] = short_dynamic_amount

        if symbol in self.max_long_trade_qty_per_symbol:
            self.long_dynamic_amount[symbol] = min(
                self.long_dynamic_amount[symbol], 
                self.max_long_trade_qty_per_symbol[symbol]
            )
        if symbol in self.max_short_trade_qty_per_symbol:
            self.short_dynamic_amount[symbol] = min(
                self.short_dynamic_amount[symbol], 
                self.max_short_trade_qty_per_symbol[symbol]
            )

        logging.info(f"Updated dynamic amounts for {symbol}. New long_dynamic_amount: {self.long_dynamic_amount[symbol]}, New short_dynamic_amount: {self.short_dynamic_amount[symbol]}")
    
    def get_open_symbols(self):
        open_position_data = self.retry_api_call(self.exchange.get_all_open_positions_bybit)
        position_symbols = set()
        for position in open_position_data:
            info = position.get('info', {})
            position_symbol = info.get('symbol', '').split(':')[0]
            if 'size' in info and 'side' in info:
                position_symbols.add(position_symbol.replace("/", ""))
        return position_symbols

    def execute_grid_auto_reduce(self, position_type, symbol, pos_qty, dynamic_amount, market_price, total_equity, long_pos_price, short_pos_price, min_qty):
        """
        Executes auto-reduction of positions by placing tagged limit orders.
        """
        amount_precision, price_precision = self.exchange.get_symbol_precision_bybit(symbol)
        price_precision_level = -int(math.log10(price_precision))
        qty_precision_level = -int(math.log10(amount_precision))
        
        market_price = Decimal(str(market_price))
        max_levels, price_interval = self.calculate_dynamic_auto_reduce_levels(symbol, pos_qty, market_price, total_equity, long_pos_price, short_pos_price)
        
        for i in range(1, max_levels + 1):
            step_price = self.calculate_step_price(position_type, market_price, price_interval, i)
            if not self.is_price_valid(position_type, step_price, market_price):
                continue
            
            step_price = round(step_price, price_precision_level)
            adjusted_dynamic_amount = max(dynamic_amount, min_qty)
            adjusted_dynamic_amount = round(adjusted_dynamic_amount, qty_precision_level)
            
            tag = f"auto_reduce_{position_type}_{symbol}_{step_price}_{i}"
            self.place_tagged_limit_order(symbol, 'sell' if position_type == 'long' else 'buy', adjusted_dynamic_amount, step_price, True, tag)

    def calculate_step_price(self, position_type, market_price, price_interval, level):
        """
        Calculates the step price for auto-reduce orders.
        """
        return market_price + (price_interval * level) if position_type == 'long' else market_price - (price_interval * level)

    def is_price_valid(self, position_type, step_price, market_price):
        """
        Checks if the calculated step price is valid for the given position type.
        """
        return step_price > market_price if position_type == 'long' else step_price < market_price

    def place_tagged_limit_order(self, symbol, side, amount, price, reduce_only, tag):
        """
        Places a tagged limit order with the option for it to be a reduce-only order.
        """
        try:
            order = self.exchange.create_tagged_limit_order_bybit(symbol, side, amount, price, reduceOnly=reduce_only, orderLinkId=tag)
            logging.info(f"Placed {side} order at {price} with tag {tag} and amount {amount}")
            return order.get('id', None) if order else None
        except Exception as e:
            logging.info(f"Error placing {side} order at {price} with tag {tag}: {e}")
            return None

    def auto_reduce_logic_grid_hardened(self, symbol, min_qty, long_pos_price, short_pos_price, 
                                        long_pos_qty, short_pos_qty, long_upnl, short_upnl,
                                        auto_reduce_enabled, total_equity, current_market_price,
                                        long_dynamic_amount, short_dynamic_amount, auto_reduce_start_pct,
                                        min_buffer_percentage_ar, max_buffer_percentage_ar,
                                        upnl_auto_reduce_threshold_long, upnl_auto_reduce_threshold_short, current_leverage):
        logging.info(f"Starting auto-reduce logic for symbol: {symbol}")
        if not auto_reduce_enabled:
            logging.info(f"Auto-reduce is disabled for {symbol}.")
            return

        try:
            # Calculate the uPNL percentage relative to the total equity
            long_upnl_pct_equity = (long_upnl / total_equity) * 100
            short_upnl_pct_equity = (short_upnl / total_equity) * 100

            logging.info(f"{symbol} Long uPNL % of Equity: {long_upnl_pct_equity:.2f}, Short uPNL % of Equity: {short_upnl_pct_equity:.2f}")

            # Determine if the market price loss exceeded the start percentage threshold
            long_loss_exceeded = long_pos_price is not None and long_pos_price != 0 and current_market_price < long_pos_price * (1 - auto_reduce_start_pct)
            short_loss_exceeded = short_pos_price is not None and short_pos_price != 0 and current_market_price > short_pos_price * (1 + auto_reduce_start_pct)

            logging.info(f"{symbol} Price Loss Exceeded - Long: {long_loss_exceeded}, Short: {short_loss_exceeded}")

            # Log the actual loss thresholds being used
            logging.info(f"Loss thresholds - Long: {upnl_auto_reduce_threshold_long}%, Short: {upnl_auto_reduce_threshold_short}%")

            # Check if the uPNL percentage of equity exceeds the loss thresholds
            upnl_long_exceeded = long_upnl_pct_equity < -upnl_auto_reduce_threshold_long
            upnl_short_exceeded = short_upnl_pct_equity < -upnl_auto_reduce_threshold_short

            logging.info(f"{symbol} UPnL Exceeded - Long: {upnl_long_exceeded}, Short: {upnl_short_exceeded}")

            # Determine if auto-reduce conditions are met
            trigger_auto_reduce_long = long_pos_qty > 0 and long_loss_exceeded and upnl_long_exceeded
            trigger_auto_reduce_short = short_pos_qty > 0 and short_loss_exceeded and upnl_short_exceeded

            logging.info(f"{symbol} Trigger Auto-Reduce - Long: {trigger_auto_reduce_long}, Short: {trigger_auto_reduce_short}")

            if trigger_auto_reduce_long:
                logging.info(f"Executing auto-reduce for long position in {symbol}.")
                self.execute_grid_auto_reduce_hardened('long', symbol, long_pos_qty, long_dynamic_amount, current_market_price, total_equity, long_pos_price, short_pos_price, min_qty, min_buffer_percentage_ar, max_buffer_percentage_ar)
            else:
                logging.info(f"No auto-reduce executed for long position in {symbol}.")

            if trigger_auto_reduce_short:
                logging.info(f"Executing auto-reduce for short position in {symbol}.")
                self.execute_grid_auto_reduce_hardened('short', symbol, short_pos_qty, short_dynamic_amount, current_market_price, total_equity, long_pos_price, short_pos_price, min_qty, min_buffer_percentage_ar, max_buffer_percentage_ar)
            else:
                logging.info(f"No auto-reduce executed for short position in {symbol}.")

        except Exception as e:
            logging.info(f"Error in auto-reduce logic for {symbol}: {e}")
            raise  # Optionally re-raise exception after logging for external handling or fail-safe mechanisms.

    def execute_grid_auto_reduce_hardened(self, position_type, symbol, pos_qty, dynamic_amount, market_price, total_equity, long_pos_price, short_pos_price, min_qty, min_buffer_percentage_ar, max_buffer_percentage_ar):
        """
        Executes a single auto-reduction order for a position based on the best market price available,
        aiming to ensure a higher probability of order execution.
        """
        amount_precision, price_precision = self.exchange.get_symbol_precision_bybit(symbol)
        price_precision_level = -int(math.log10(price_precision))
        qty_precision_level = -int(math.log10(amount_precision))

        # Fetch current best bid and ask prices to place the order as close as possible to the market
        # best_bid, best_ask = self.exchange.get_best_bid_ask(symbol)

        current_price = self.exchange.get_current_price(symbol)

        order_book = self.exchange.get_orderbook(symbol)
        best_ask_price = order_book['asks'][0][0] if 'asks' in order_book else self.last_known_ask.get(symbol, current_price)
        best_bid_price = order_book['bids'][0][0] if 'bids' in order_book else self.last_known_bid.get(symbol, current_price)
        
        # Determine the appropriate price to place the order based on position type
        order_price = best_bid_price if position_type == 'long' else best_ask_price
        order_price = round(order_price, price_precision_level)
        adjusted_dynamic_amount = max(dynamic_amount, min_qty)
        adjusted_dynamic_amount = round(adjusted_dynamic_amount, qty_precision_level)

        # Determine the positionIdx based on the position_type
        positionIdx = 1 if position_type == 'long' else 2

        logging.info(f"Attempting to place auto-reduce order: Symbol={symbol}, Type={'sell' if position_type == 'long' else 'buy'}, Qty={adjusted_dynamic_amount}, Price={order_price}")

        # Try placing the order using the provided utility method
        try:
            order_result = self.postonly_limit_order_bybit_nolimit(symbol, 'sell' if position_type == 'long' else 'buy', adjusted_dynamic_amount, order_price, positionIdx, reduceOnly=True)
            logging.info(f"Auto-reduce order placed successfully: {order_result}")
        except Exception as e:
            logging.info(f"Failed to place auto-reduce order for {symbol}: {e}")
            raise

        # Log the order details for monitoring
        logging.info(f"Placed auto-reduce order for {symbol} at {order_price} for {adjusted_dynamic_amount}")


    def auto_reduce_logic_grid(self, symbol, min_qty, long_pos_price, short_pos_price, long_pos_qty, short_pos_qty,
                                auto_reduce_enabled, total_equity, available_equity, current_market_price,
                                long_dynamic_amount, short_dynamic_amount, auto_reduce_start_pct,
                                max_pos_balance_pct, upnl_threshold_pct, shared_symbols_data):
        logging.info(f"Starting auto-reduce logic for symbol: {symbol}")
        if not auto_reduce_enabled:
            logging.info(f"Auto-reduce is disabled for {symbol}.")
            return

        try:
            #total_upnl = sum(data['long_upnl'] + data['short_upnl'] for data in shared_symbols_data.values())
            # Possibly has issues w/ calculation above

            # Testing fix

            total_upnl = sum(
                (data.get('long_upnl', 0) or 0) + (data.get('short_upnl', 0) or 0)
                for data in shared_symbols_data.values()
            )
            
            logging.info(f"Total uPNL : {total_upnl}")

            # Correct calculation for total UPnL percentage
            total_upnl_pct = total_upnl / total_equity if total_equity else 0

            # Correcting the UPnL threshold exceeded logic to compare absolute UPnL against the threshold value of total equity
            upnl_threshold_exceeded = abs(total_upnl) > (total_equity * upnl_threshold_pct)

            symbol_data = shared_symbols_data.get(symbol, {})
            long_position_value_pct = (symbol_data.get('long_pos_qty', 0) * current_market_price / total_equity) if total_equity else 0
            short_position_value_pct = (symbol_data.get('short_pos_qty', 0) * current_market_price / total_equity) if total_equity else 0

            long_loss_exceeded = long_pos_price is not None and current_market_price < long_pos_price * (1 - auto_reduce_start_pct)
            short_loss_exceeded = short_pos_price is not None and current_market_price > short_pos_price * (1 + auto_reduce_start_pct)

            trigger_auto_reduce_long = long_pos_qty > 0 and long_loss_exceeded and long_position_value_pct > max_pos_balance_pct and upnl_threshold_exceeded
            trigger_auto_reduce_short = short_pos_qty > 0 and short_loss_exceeded and short_position_value_pct > max_pos_balance_pct and upnl_threshold_exceeded

            logging.info(f"Total UPnL for all symbols: {total_upnl}, which is {total_upnl_pct * 100}% of total equity")
            logging.info(f"{symbol} Long Position Value %: {long_position_value_pct * 100}, Short Position Value %: {short_position_value_pct * 100}")
            logging.info(f"{symbol} Long Loss Exceeded: {long_loss_exceeded}, Short Loss Exceeded: {short_loss_exceeded}, UPnL Threshold Exceeded: {upnl_threshold_exceeded}")
            logging.info(f"{symbol} Trigger Auto-Reduce Long: {trigger_auto_reduce_long}, Trigger Auto-Reduce Short: {trigger_auto_reduce_short}")

            if trigger_auto_reduce_long:
                logging.info(f"Executing auto-reduce for long position in {symbol}.")
                self.execute_grid_auto_reduce('long', symbol, long_pos_qty, long_dynamic_amount, current_market_price, total_equity, long_pos_price, short_pos_price, min_qty)
            else:
                logging.info(f"No auto-reduce executed for long position in {symbol}.")

            if trigger_auto_reduce_short:
                logging.info(f"Executing auto-reduce for short position in {symbol}.")
                self.execute_grid_auto_reduce('short', symbol, short_pos_qty, short_dynamic_amount, current_market_price, total_equity, long_pos_price, short_pos_price, min_qty)
            else:
                logging.info(f"No auto-reduce executed for short position in {symbol}.")

        except Exception as e:
            logging.info(f"Error in auto-reduce logic for {symbol}: {e}")

    def execute_auto_reduce(self, position_type, symbol, pos_qty, dynamic_amount, market_price, total_equity, long_pos_price, short_pos_price, min_qty):
        # Fetch precision for the symbol
        amount_precision, price_precision = self.exchange.get_symbol_precision_bybit(symbol)
        price_precision_level = -int(math.log10(price_precision))
        qty_precision_level = -int(math.log10(amount_precision))

        # Convert market_price to Decimal for consistent arithmetic operations
        market_price = Decimal(str(market_price))

        max_levels, price_interval = self.calculate_dynamic_auto_reduce_levels(symbol, pos_qty, market_price, total_equity, long_pos_price, short_pos_price)
        for i in range(1, max_levels + 1):
            # Calculate step price based on position type
            if position_type == 'long':
                step_price = market_price + (price_interval * i)
                # Ensure step price is greater than the market price for long positions
                if step_price <= market_price:
                    logging.warning(f"Skipping auto-reduce long order for {symbol} at {step_price} as it is not greater than the market price.")
                    continue
            else:  # position_type == 'short'
                step_price = market_price - (price_interval * i)
                # Ensure step price is less than the market price for short positions
                if step_price >= market_price:
                    logging.warning(f"Skipping auto-reduce short order for {symbol} at {step_price} as it is not less than the market price.")
                    continue

            # Round the step price to the correct precision
            step_price = round(step_price, price_precision_level)

            # Ensure dynamic_amount is at least the minimum required quantity and rounded to the correct precision
            adjusted_dynamic_amount = max(dynamic_amount, min_qty)
            adjusted_dynamic_amount = round(adjusted_dynamic_amount, qty_precision_level)

            # Attempt to place the auto-reduce order
            try:
                if position_type == 'long':
                    order_id = self.auto_reduce_long(symbol, adjusted_dynamic_amount, float(step_price))
                elif position_type == 'short':
                    order_id = self.auto_reduce_short(symbol, adjusted_dynamic_amount, float(step_price))

                # Initialize the symbol key if it doesn't exist
                if symbol not in self.auto_reduce_orders:
                    self.auto_reduce_orders[symbol] = []

                if order_id:
                    self.auto_reduce_orders[symbol].append(order_id)
                    logging.info(f"{symbol} {position_type.capitalize()} Auto-Reduce Order Placed at {step_price} with amount {adjusted_dynamic_amount}")
                else:
                    logging.warning(f"{symbol} {position_type.capitalize()} Auto-Reduce Order Not Filled Immediately at {step_price} with amount {adjusted_dynamic_amount}")
            except Exception as e:
                logging.info(f"Error in executing auto-reduce {position_type} order for {symbol}: {e}")
                logging.info("Traceback:", traceback.format_exc())

    def cancel_all_auto_reduce_orders_bybit(self, symbol: str) -> None:
        try:
            if symbol in self.auto_reduce_orders:
                for order_id in self.auto_reduce_orders[symbol]:
                    try:
                        self.exchange.cancel_order(order_id, symbol)
                        logging.info(f"Cancelling auto-reduce order: {order_id}")
                    except Exception as e:
                        logging.warning(f"An error occurred while cancelling auto-reduce order {order_id}: {e}")
                self.auto_reduce_orders[symbol].clear()  # Clear the list after cancellation
            else:
                logging.info(f"No auto-reduce orders found for {symbol}")

        except Exception as e:
            logging.warning(f"An unknown error occurred in cancel_all_auto_reduce_orders_bybit(): {e}")

    def auto_reduce_logic_simple(self, symbol, min_qty, long_pos_price, short_pos_price, long_pos_qty, short_pos_qty,
                                auto_reduce_enabled, total_equity, available_equity, current_market_price,
                                long_dynamic_amount, short_dynamic_amount, auto_reduce_start_pct,
                                max_pos_balance_pct, upnl_threshold_pct, shared_symbols_data):
        logging.info(f"Starting auto-reduce logic for symbol: {symbol}")
        if not auto_reduce_enabled:
            logging.info(f"Auto-reduce is disabled for {symbol}.")
            return

        try:
            #total_upnl = sum(data['long_upnl'] + data['short_upnl'] for data in shared_symbols_data.values())
            # Possibly has issues w/ calculation above

            # Testing fix

            total_upnl = sum(
                (data.get('long_upnl', 0) or 0) + (data.get('short_upnl', 0) or 0)
                for data in shared_symbols_data.values()
            )
            
            logging.info(f"Total uPNL : {total_upnl}")

            # Correct calculation for total UPnL percentage
            total_upnl_pct = total_upnl / total_equity if total_equity else 0

            # Correcting the UPnL threshold exceeded logic to compare absolute UPnL against the threshold value of total equity
            upnl_threshold_exceeded = abs(total_upnl) > (total_equity * upnl_threshold_pct)

            symbol_data = shared_symbols_data.get(symbol, {})
            long_position_value_pct = (symbol_data.get('long_pos_qty', 0) * current_market_price / total_equity) if total_equity else 0
            short_position_value_pct = (symbol_data.get('short_pos_qty', 0) * current_market_price / total_equity) if total_equity else 0

            long_loss_exceeded = long_pos_price is not None and current_market_price < long_pos_price * (1 - auto_reduce_start_pct)
            short_loss_exceeded = short_pos_price is not None and current_market_price > short_pos_price * (1 + auto_reduce_start_pct)

            trigger_auto_reduce_long = long_pos_qty > 0 and long_loss_exceeded and long_position_value_pct > max_pos_balance_pct and upnl_threshold_exceeded
            trigger_auto_reduce_short = short_pos_qty > 0 and short_loss_exceeded and short_position_value_pct > max_pos_balance_pct and upnl_threshold_exceeded

            logging.info(f"Total UPnL for all symbols: {total_upnl}, which is {total_upnl_pct * 100}% of total equity")
            logging.info(f"{symbol} Long Position Value %: {long_position_value_pct * 100}, Short Position Value %: {short_position_value_pct * 100}")
            logging.info(f"{symbol} Long Loss Exceeded: {long_loss_exceeded}, Short Loss Exceeded: {short_loss_exceeded}, UPnL Threshold Exceeded: {upnl_threshold_exceeded}")
            logging.info(f"{symbol} Trigger Auto-Reduce Long: {trigger_auto_reduce_long}, Trigger Auto-Reduce Short: {trigger_auto_reduce_short}")

            if trigger_auto_reduce_long:
                logging.info(f"Executing auto-reduce for long position in {symbol}.")
                self.execute_auto_reduce('long', symbol, long_pos_qty, long_dynamic_amount, current_market_price, total_equity, long_pos_price, short_pos_price, min_qty)
            else:
                logging.info(f"No auto-reduce executed for long position in {symbol}.")

            if trigger_auto_reduce_short:
                logging.info(f"Executing auto-reduce for short position in {symbol}.")
                self.execute_auto_reduce('short', symbol, short_pos_qty, short_dynamic_amount, current_market_price, total_equity, long_pos_price, short_pos_price, min_qty)
            else:
                logging.info(f"No auto-reduce executed for short position in {symbol}.")

        except Exception as e:
            logging.info(f"Error in auto-reduce logic for {symbol}: {e}")

    def should_terminate_open_orders(self, symbol, long_pos_qty, short_pos_qty, open_positions_data, open_orders, current_time):
        try:
            # Check if there are any open positions for the symbol
            has_position = any(pos['symbol'].split(':')[0] == symbol for pos in open_positions_data)

            # Filter active orders (non-reduce-only orders)
            active_orders = [order for order in open_orders if not order.get('reduceOnly', False)]

            # Check if there are any active long or short orders for the symbol
            long_orders = [order for order in active_orders if order['side'] == 'buy']
            short_orders = [order for order in active_orders if order['side'] == 'sell']

            # Determine if long or short orders should be terminated
            should_terminate_long = long_orders and not has_position and long_pos_qty == 0
            should_terminate_short = short_orders and not has_position and short_pos_qty == 0

            result = None
            if should_terminate_long:
                if symbol not in self.last_active_long_order_time:
                    self.last_active_long_order_time[symbol] = current_time
                elif current_time - self.last_active_long_order_time[symbol] > self.order_inactive_threshold:
                    logging.info(f"Long orders for {symbol} have been active without matching positions for more than {self.order_inactive_threshold} seconds.")
                    result = "long"
                else:
                    if symbol in self.last_active_long_order_time:
                        del self.last_active_long_order_time[symbol]

            if should_terminate_short:
                if symbol not in self.last_active_short_order_time:
                    self.last_active_short_order_time[symbol] = current_time
                elif current_time - self.last_active_short_order_time[symbol] > self.order_inactive_threshold:
                    logging.info(f"Short orders for {symbol} have been active without matching positions for more than {self.order_inactive_threshold} seconds.")
                    result = "short" if result is None else "both"
                else:
                    if symbol in self.last_active_short_order_time:
                        del self.last_active_short_order_time[symbol]

            return result

        except Exception as e:
            logging.info(f"Exception caught in should terminate open orders: {e}")
            return None
        
    # Threading locks
    def should_terminate_full(self, symbol, current_time, previous_long_pos_qty, long_pos_qty, previous_short_pos_qty, short_pos_qty):
        open_symbols = self.get_open_symbols()  # Fetch open symbols

        if symbol not in self.symbol_locks:
            self.symbol_locks[symbol] = threading.Lock()

        with self.symbol_locks[symbol]:
            if symbol not in open_symbols:
                if not hasattr(self, 'position_closed_time'):
                    self.position_closed_time = current_time
                elif current_time - self.position_closed_time > self.position_inactive_threshold:
                    logging.info(f"Position for {symbol} has been inactive for more than {self.position_inactive_threshold} seconds.")
                    return True

            if not hasattr(self, 'last_entry_signal_time'):
                self.last_entry_signal_time = current_time
            elif current_time - self.last_entry_signal_time > self.no_entry_signal_threshold:
                logging.info(f"No entry signal for {symbol} in the last {self.no_entry_signal_threshold} seconds.")
                return True
            else:
                if hasattr(self, 'position_closed_time'):
                    del self.position_closed_time

            if previous_long_pos_qty > 0 and long_pos_qty == 0:
                logging.info(f"Long position closed for {symbol}. Canceling long grid orders.")
                self.cancel_grid_orders(symbol, "buy")
                return True

            if previous_short_pos_qty > 0 and short_pos_qty == 0:
                logging.info(f"Short position closed for {symbol}. Canceling short grid orders.")
                self.cancel_grid_orders(symbol, "sell")
                return True

        return False
        
    def should_terminate(self, symbol, current_time):
        open_symbols = self.get_open_symbols()  # Fetch open symbols
        if symbol not in open_symbols:
            if not hasattr(self, 'position_closed_time'):
                self.position_closed_time = current_time
            elif current_time - self.position_closed_time > self.position_inactive_threshold:
                logging.info(f"Position for {symbol} has been inactive for more than {self.position_inactive_threshold} seconds.")
                return True
        else:
            if hasattr(self, 'position_closed_time'):
                del self.position_closed_time
        return False

    def cleanup_before_termination(self, symbol):
        # Cancel all orders for the symbol and perform any other cleanup needed
        self.exchange.cancel_all_orders_for_symbol_bybit(symbol)

    def calculate_next_update_time(self):
        """Returns the time for the next TP update, which is 30 seconds from the current time."""
        now = datetime.now()
        next_update_time = now + timedelta(seconds=3)
        return next_update_time.replace(microsecond=0)

    # Bybit cancel all entries
    def cancel_entries_bybit(self, symbol, best_ask_price, ma_1m_3_high, ma_5m_3_high):
        # Cancel entries
        current_time = time.time()
        if current_time - self.last_entries_cancel_time >= 60: #60 # Execute this block every 1 minute
            try:
                if best_ask_price < ma_1m_3_high or best_ask_price < ma_5m_3_high:
                    self.exchange.cancel_all_entries_bybit(symbol)
                    logging.info(f"Canceled entry orders for {symbol}")
                    time.sleep(0.05)
            except Exception as e:
                logging.info(f"An error occurred while canceling entry orders: {e}")

            self.last_entries_cancel_time = current_time

    def clear_stale_positions(self, open_orders, rotator_symbols, max_time_without_volume=3600): # default time is 1 hour
        open_positions = self.exchange.get_open_positions()

        for position in open_positions:
            symbol = position['symbol']

            # Check if the symbol is not in the rotator list
            if symbol not in rotator_symbols:

                # Check how long the position has been open
                position_open_time = position.get('timestamp', None)  # assuming your position has a 'timestamp' field
                current_time = time.time()
                time_elapsed = current_time - position_open_time

                # Fetch volume for the coin
                volume = self.exchange.get_24hr_volume(symbol)

                # Check if the volume is low and position has been open for too long
                if volume < self.MIN_VOLUME_THRESHOLD and time_elapsed > max_time_without_volume:

                    # Place take profit order at the current price
                    current_price = self.exchange.get_current_price(symbol)
                    amount = position['amount']  # assuming your position has an 'amount' field

                    # Determine if it's a buy or sell based on position type
                    order_type = "sell" if position['side'] == 'long' else "buy"
                    self.bybit_hedge_placetp_maker(symbol, amount, current_price, positionIdx=1, order_side="sell", open_orders=open_orders)
                    #self.exchange.place_order(symbol, order_type, amount, current_price, take_profit=True)

                    logging.info(f"Placed take profit order for stale position: {symbol} at price: {current_price}")

    def cancel_stale_orders_bybit(self, symbol):
        current_time = time.time()
        if current_time - self.last_stale_order_check_time < 3720:  # 3720 seconds = 1 hour 12 minutes
            return  # Skip the rest of the function if it's been less than 1 hour 12 minutes

        # Directly cancel orders for the given symbol
        self.exchange.cancel_all_open_orders_bybit(symbol)
        logging.info(f"Stale orders for {symbol} canceled")

        self.last_stale_order_check_time = current_time  # Update the last check time

    def cancel_all_orders_for_symbol_bybit(self, symbol):
        try:
            self.exchange.cancel_all_open_orders_bybit(symbol)
            logging.info(f"All orders for {symbol} canceled")
        except Exception as e:
            logging.info(f"An error occurred while canceling all orders for {symbol}: {e}")

    def get_all_open_orders_bybit(self):
        """
        Fetch all open orders for all symbols from the Bybit API.

        :return: A list of open orders for all symbols.
        """
        try:
            # Call fetch_open_orders with no symbol to get orders for all symbols
            all_open_orders = self.exchange.fetch_open_orders()
            return all_open_orders
        except Exception as e:
            print(f"An error occurred while fetching all open orders: {e}")
            return []

    def cancel_old_entries_bybit(self, symbol):
        # Cancel entries
        try:
            self.exchange.cancel_all_entries_bybit(symbol)
            logging.info(f"Canceled entry orders for {symbol}")
            time.sleep(0.05)
        except Exception as e:
            logging.info(f"An error occurred while canceling entry orders: {e}")

    def update_quickscalp_tp_dynamic(self, symbol, pos_qty, upnl_profit_pct, max_upnl_profit_pct, short_pos_price, long_pos_price, positionIdx, order_side, last_tp_update, tp_order_counts, max_retries=10):
        # Fetch the current open TP orders and TP order counts for the symbol
        long_tp_orders, short_tp_orders = self.exchange.get_open_tp_orders(symbol)
        long_tp_count = tp_order_counts['long_tp_count']
        short_tp_count = tp_order_counts['short_tp_count']

        # Calculate the dynamic TP range based on position size
        min_tp_pct = upnl_profit_pct
        max_tp_pct = min(max_upnl_profit_pct, upnl_profit_pct + (pos_qty / 1000) * 0.01)  # Adjust the scaling factor as needed

        # Calculate the new TP values using quickscalp method and the dynamic TP range
        new_short_tp_min, new_short_tp_max = self.calculate_quickscalp_short_take_profit_dynamic_distance(short_pos_price, symbol, min_tp_pct, max_tp_pct)
        new_long_tp_min, new_long_tp_max = self.calculate_quickscalp_long_take_profit_dynamic_distance(long_pos_price, symbol, min_tp_pct, max_tp_pct)

        # Determine the relevant TP orders based on the order side
        relevant_tp_orders = long_tp_orders if order_side == "sell" else short_tp_orders

        # Check if there's an existing TP order with a mismatched quantity
        mismatched_qty_orders = [order for order in relevant_tp_orders if order['qty'] != pos_qty and order['id'] not in self.auto_reduce_order_ids.get(symbol, [])]

        # Cancel mismatched TP orders if any
        for order in mismatched_qty_orders:
            try:
                self.exchange.cancel_order_by_id(order['id'], symbol)
                logging.info(f"Cancelled TP order {order['id']} for update.")
            except Exception as e:
                logging.info(f"Error in cancelling {order_side} TP order {order['id']}. Error: {e}")

        now = datetime.now()
        if now >= last_tp_update or mismatched_qty_orders:
            # Check if a TP order already exists
            tp_order_exists = (order_side == "sell" and long_tp_count > 0) or (order_side == "buy" and short_tp_count > 0)

            # Set new TP order with updated prices only if no TP order exists
            if not tp_order_exists:
                new_tp_price_min = new_long_tp_min if order_side == "sell" else new_short_tp_min
                new_tp_price_max = new_long_tp_max if order_side == "sell" else new_short_tp_max
                current_price = self.exchange.get_current_price(symbol)

                if (order_side == "sell" and current_price >= new_tp_price_min) or (order_side == "buy" and current_price <= new_tp_price_max):
                    # If the current price has surpassed the new TP price range, use a normal limit order
                    try:
                        self.exchange.create_normal_take_profit_order_bybit(symbol, "limit", order_side, pos_qty, new_tp_price_min, positionIdx=positionIdx, reduce_only=True)
                        logging.info(f"New {order_side.capitalize()} TP set at {new_tp_price_min} using a normal limit order")
                    except Exception as e:
                        logging.info(f"Failed to set new {order_side} TP for {symbol} using a normal limit order. Error: {e}")
                else:
                    # If the current price hasn't surpassed the new TP price range, use a post-only order
                    try:
                        self.exchange.create_take_profit_order_bybit(symbol, "limit", order_side, pos_qty, new_tp_price_max, positionIdx=positionIdx, reduce_only=True)
                        logging.info(f"New {order_side.capitalize()} TP set at {new_tp_price_max} using a post-only order")
                    except Exception as e:
                        logging.info(f"Failed to set new {order_side} TP for {symbol} using a post-only order. Error: {e}")
            else:
                logging.info(f"Skipping TP update as a TP order already exists for {symbol}")

            # Calculate and return the next update time
            return self.calculate_next_update_time()
        else:
            logging.info(f"No immediate update needed for TP orders for {symbol}. Last update at: {last_tp_update}")
            return last_tp_update
        
    def update_quickscalp_tp(self, symbol, pos_qty, upnl_profit_pct, short_pos_price, long_pos_price, positionIdx, order_side, last_tp_update, tp_order_counts, max_retries=10):
        # Fetch the current open TP orders and TP order counts for the symbol
        long_tp_orders, short_tp_orders = self.exchange.get_open_tp_orders(symbol)
        long_tp_count = tp_order_counts['long_tp_count']
        short_tp_count = tp_order_counts['short_tp_count']

        # Calculate the new TP values using quickscalp method
        new_short_tp = self.calculate_quickscalp_short_take_profit(short_pos_price, symbol, upnl_profit_pct)
        new_long_tp = self.calculate_quickscalp_long_take_profit(long_pos_price, symbol, upnl_profit_pct)

        # Determine the relevant TP orders based on the order side
        relevant_tp_orders = long_tp_orders if order_side == "sell" else short_tp_orders

        # Check if there's an existing TP order with a mismatched quantity
        mismatched_qty_orders = [order for order in relevant_tp_orders if order['qty'] != pos_qty and order['id'] not in self.auto_reduce_order_ids.get(symbol, [])]

        # Cancel mismatched TP orders if any
        for order in mismatched_qty_orders:
            try:
                self.exchange.cancel_order_by_id(order['id'], symbol)
                logging.info(f"Cancelled TP order {order['id']} for update.")
            except Exception as e:
                logging.info(f"Error in cancelling {order_side} TP order {order['id']}. Error: {e}")

        now = datetime.now()
        if now >= last_tp_update or mismatched_qty_orders:
            # Check if a TP order already exists
            tp_order_exists = (order_side == "sell" and long_tp_count > 0) or (order_side == "buy" and short_tp_count > 0)

            # Set new TP order with updated prices only if no TP order exists
            if not tp_order_exists:
                new_tp_price = new_long_tp if order_side == "sell" else new_short_tp
                current_price = self.exchange.get_current_price(symbol)

                if (order_side == "sell" and current_price >= new_tp_price) or (order_side == "buy" and current_price <= new_tp_price):
                    # If the current price has surpassed the new TP price, use a normal limit order
                    try:
                        self.exchange.create_normal_take_profit_order_bybit(symbol, "limit", order_side, pos_qty, new_tp_price, positionIdx=positionIdx, reduce_only=True)
                        logging.info(f"New {order_side.capitalize()} TP set at {new_tp_price} using a normal limit order")
                    except Exception as e:
                        logging.info(f"Failed to set new {order_side} TP for {symbol} using a normal limit order. Error: {e}")
                else:
                    # If the current price hasn't surpassed the new TP price, use a post-only order
                    try:
                        self.exchange.create_take_profit_order_bybit(symbol, "limit", order_side, pos_qty, new_tp_price, positionIdx=positionIdx, reduce_only=True)
                        logging.info(f"New {order_side.capitalize()} TP set at {new_tp_price} using a post-only order")
                    except Exception as e:
                        logging.info(f"Failed to set new {order_side} TP for {symbol} using a post-only order. Error: {e}")
            else:
                logging.info(f"Skipping TP update as a TP order already exists for {symbol}")

            # Calculate and return the next update time
            return self.calculate_next_update_time()
        else:
            logging.info(f"No immediate update needed for TP orders for {symbol}. Last update at: {last_tp_update}")
            return last_tp_update

    def calculate_quickscalp_long_take_profit_dynamic_distance(self, long_pos_price, symbol, min_upnl_profit_pct, max_upnl_profit_pct):
        if long_pos_price is None:
            return None

        price_precision = int(self.exchange.get_price_precision(symbol))
        logging.info(f"Price precision for {symbol}: {price_precision}")

        # Calculate the minimum and maximum target profit prices
        min_target_profit_price = Decimal(long_pos_price) * (1 + Decimal(min_upnl_profit_pct))
        max_target_profit_price = Decimal(long_pos_price) * (1 + Decimal(max_upnl_profit_pct))

        # Quantize the target profit prices
        try:
            min_target_profit_price = min_target_profit_price.quantize(
                Decimal('1e-{}'.format(price_precision)), rounding=ROUND_HALF_UP
            )
            max_target_profit_price = max_target_profit_price.quantize(
                Decimal('1e-{}'.format(price_precision)), rounding=ROUND_HALF_UP
            )
        except InvalidOperation as e:
            logging.info(f"Error when quantizing target_profit_prices. {e}")
            return None

        # Return the minimum and maximum target profit prices as a tuple
        return float(min_target_profit_price), float(max_target_profit_price)

    def calculate_quickscalp_short_take_profit_dynamic_distance(self, short_pos_price, symbol, min_upnl_profit_pct, max_upnl_profit_pct):
        if short_pos_price is None:
            return None

        price_precision = int(self.exchange.get_price_precision(symbol))
        logging.info(f"Price precision for {symbol}: {price_precision}")

        # Calculate the minimum and maximum target profit prices
        min_target_profit_price = Decimal(short_pos_price) * (1 - Decimal(min_upnl_profit_pct))
        max_target_profit_price = Decimal(short_pos_price) * (1 - Decimal(max_upnl_profit_pct))

        # Quantize the target profit prices
        try:
            min_target_profit_price = min_target_profit_price.quantize(
                Decimal('1e-{}'.format(price_precision)), rounding=ROUND_HALF_UP
            )
            max_target_profit_price = max_target_profit_price.quantize(
                Decimal('1e-{}'.format(price_precision)), rounding=ROUND_HALF_UP
            )
        except InvalidOperation as e:
            logging.info(f"Error when quantizing target_profit_prices. {e}")
            return None

        # Return the minimum and maximum target profit prices as a tuple
        return float(min_target_profit_price), float(max_target_profit_price)
            
    def calculate_quickscalp_long_take_profit(self, long_pos_price, symbol, upnl_profit_pct):
        if long_pos_price is None:
            return None

        price_precision = int(self.exchange.get_price_precision(symbol))
        logging.info(f"Price precision for {symbol}: {price_precision}")

        # Calculate the target profit price
        target_profit_price = Decimal(long_pos_price) * (1 + Decimal(upnl_profit_pct))
        
        # Quantize the target profit price
        try:
            target_profit_price = target_profit_price.quantize(
                Decimal('1e-{}'.format(price_precision)),
                rounding=ROUND_HALF_UP
            )
        except InvalidOperation as e:
            logging.info(f"Error when quantizing target_profit_price. {e}")
            return None

        return float(target_profit_price)

    def calculate_quickscalp_short_take_profit(self, short_pos_price, symbol, upnl_profit_pct):
        if short_pos_price is None:
            return None

        price_precision = int(self.exchange.get_price_precision(symbol))
        logging.info(f"Price precision for {symbol}: {price_precision}")

        # Calculate the target profit price
        target_profit_price = Decimal(short_pos_price) * (1 - Decimal(upnl_profit_pct))
        
        # Quantize the target profit price
        try:
            target_profit_price = target_profit_price.quantize(
                Decimal('1e-{}'.format(price_precision)),
                rounding=ROUND_HALF_UP
            )
        except InvalidOperation as e:
            logging.info(f"Error when quantizing target_profit_price. {e}")
            return None

        return float(target_profit_price)
    
# price_precision, qty_precision = self.exchange.get_symbol_precision_bybit(symbol)
    def calculate_dynamic_long_take_profit(self, best_bid_price, long_pos_price, symbol, upnl_profit_pct, max_deviation_pct=0.0040):
        if long_pos_price is None:
            logging.info("Long position price is None for symbol: " + symbol)
            return None

        _, price_precision = self.exchange.get_symbol_precision_bybit(symbol)
        logging.info(f"Price precision for {symbol}: {price_precision}")

        original_tp = long_pos_price * (1 + upnl_profit_pct)
        logging.info(f"Original long TP for {symbol}: {original_tp}")

        bid_walls, ask_walls = self.detect_significant_order_book_walls_atr(symbol)
        if not ask_walls:
            logging.info(f"No significant ask walls found for {symbol}")

        adjusted_tp = original_tp
        for price, size in ask_walls:
            if price > original_tp:
                extended_tp = price - float(price_precision)
                if extended_tp > 0:
                    adjusted_tp = max(adjusted_tp, extended_tp)
                    logging.info(f"Adjusted long TP for {symbol} based on ask wall: {adjusted_tp}")
                break

        # Check if the adjusted TP is within the allowed deviation from the original TP
        if adjusted_tp > original_tp * (1 + max_deviation_pct):
            logging.info(f"Adjusted long TP for {symbol} exceeds the allowed deviation. Reverting to original TP: {original_tp}")
            adjusted_tp = original_tp

        # Adjust TP to best bid price if surpassed
        if best_bid_price >= adjusted_tp:
            adjusted_tp = best_bid_price
            logging.info(f"TP surpassed, adjusted to best bid price for {symbol}: {adjusted_tp}")

        rounded_tp = round(adjusted_tp, len(str(price_precision).split('.')[-1]))
        logging.info(f"Final rounded long TP for {symbol}: {rounded_tp}")
        return rounded_tp

    def calculate_dynamic_short_take_profit(self, best_ask_price, short_pos_price, symbol, upnl_profit_pct, max_deviation_pct=0.05):
        if short_pos_price is None:
            logging.info("Short position price is None for symbol: " + symbol)
            return None

        _, price_precision = self.exchange.get_symbol_precision_bybit(symbol)
        logging.info(f"Price precision for {symbol}: {price_precision}")

        original_tp = short_pos_price * (1 - upnl_profit_pct)
        logging.info(f"Original short TP for {symbol}: {original_tp}")

        bid_walls, ask_walls = self.detect_significant_order_book_walls_atr(symbol)
        if not bid_walls:
            logging.info(f"No significant bid walls found for {symbol}")

        adjusted_tp = original_tp
        for price, size in bid_walls:
            if price < original_tp:
                extended_tp = price + float(price_precision)
                if extended_tp > 0:
                    adjusted_tp = min(adjusted_tp, extended_tp)
                    logging.info(f"Adjusted short TP for {symbol} based on bid wall: {adjusted_tp}")
                break

        # Check if the adjusted TP is within the allowed deviation from the original TP
        if adjusted_tp < original_tp * (1 - max_deviation_pct):
            logging.info(f"Adjusted short TP for {symbol} exceeds the allowed deviation. Reverting to original TP: {original_tp}")
            adjusted_tp = original_tp

        # Adjust TP to best ask price if surpassed
        if best_ask_price <= adjusted_tp:
            adjusted_tp = best_ask_price
            logging.info(f"TP surpassed, adjusted to best ask price for {symbol}: {adjusted_tp}")

        rounded_tp = round(adjusted_tp, len(str(price_precision).split('.')[-1]))
        logging.info(f"Final rounded short TP for {symbol}: {rounded_tp}")
        return rounded_tp
    def detect_order_book_walls(self, symbol, threshold=5.0):
        order_book = self.exchange.get_orderbook(symbol)
        bids = order_book['bids']
        asks = order_book['asks']

        avg_bid_size = sum([bid[1] for bid in bids[:10]]) / 10
        bid_walls = [(price, size) for price, size in bids if size > avg_bid_size * threshold]

        avg_ask_size = sum([ask[1] for ask in asks[:10]]) / 10
        ask_walls = [(price, size) for price, size in asks if size > avg_ask_size * threshold]

        if bid_walls:
            logging.info(f"Detected buy walls at {bid_walls} for {symbol}")
        if ask_walls:
            logging.info(f"Detected sell walls at {ask_walls} for {symbol}")

        return bid_walls, ask_walls

    def detect_significant_order_book_walls_atr(self, symbol, timeframe='1h', base_threshold_factor=5.0, atr_proximity_percentage=10.0):
        order_book = self.exchange.get_orderbook(symbol)
        bids, asks = order_book['bids'], order_book['asks']

        # Ensure there are enough orders to perform analysis
        if len(bids) < 20 or len(asks) < 20:
            logging.info("Not enough data in the order book to detect walls.")
            return [], []

        # Calculate ATR for market volatility
        historical_data = self.fetch_historical_data(symbol, timeframe)
        atr = self.calculate_atr(historical_data)
        
        # Calculate dynamic threshold based on ATR
        dynamic_threshold = base_threshold_factor * atr

        # Calculate average order size for a larger sample of orders
        sample_size = 20  # Increased sample size
        avg_bid_size = sum([bid[1] for bid in bids[:sample_size]]) / sample_size
        avg_ask_size = sum([ask[1] for ask in asks[:sample_size]]) / sample_size

        # Current market price
        current_price = self.exchange.get_current_price(symbol)

        # Calculate proximity threshold as a percentage of the current price
        proximity_threshold = (atr_proximity_percentage / 100) * current_price

        # Function to check wall significance
        def is_wall_significant(price, size, threshold, avg_size):
            return size > max(threshold, avg_size * base_threshold_factor) and abs(price - current_price) <= proximity_threshold

        # Detect significant bid and ask walls
        significant_bid_walls = [(price, size) for price, size in bids if is_wall_significant(price, size, dynamic_threshold, avg_bid_size)]
        significant_ask_walls = [(price, size) for price, size in asks if is_wall_significant(price, size, dynamic_threshold, avg_ask_size)]

        logging.info(f"Significant bid walls: {significant_bid_walls} for {symbol}")
        logging.info(f"Significant ask walls: {significant_ask_walls} for {symbol}")

        return significant_bid_walls, significant_ask_walls

    def is_price_approaching_wall(self, current_price, wall_price, wall_type):
        # Define a relative proximity threshold, e.g., 0.5%
        proximity_percentage = 0.005  # 0.5%

        # Calculate the proximity threshold in price units
        proximity_threshold = wall_price * proximity_percentage

        # Check if current price is within the threshold of the wall price
        if wall_type == 'bid' and current_price >= wall_price - proximity_threshold:
            # Price is approaching a bid wall
            return True
        elif wall_type == 'ask' and current_price <= wall_price + proximity_threshold:
            # Price is approaching an ask wall
            return True

        return False

    def calculate_trading_fee(self, qty, executed_price, fee_rate=TAKER_FEE_RATE):
        order_value = qty / executed_price
        trading_fee = order_value * fee_rate
        return trading_fee

    def calculate_orderbook_strength(self, symbol, depth=10):
        order_book = self.exchange.get_orderbook(symbol)
        
        top_bids = order_book['bids'][:depth]
        total_bid_quantity = sum([bid[1] for bid in top_bids])
        
        top_asks = order_book['asks'][:depth]
        total_ask_quantity = sum([ask[1] for ask in top_asks])
        
        if (total_bid_quantity + total_ask_quantity) == 0:
            return 0.5  # Neutral strength
        
        strength = total_bid_quantity / (total_bid_quantity + total_ask_quantity)
        
        return strength

    def initialize_symbol(self, symbol, total_equity, best_ask_price, max_leverage):
        with self.initialized_symbols_lock:
            if symbol not in self.initialized_symbols:
                self.initialize_trade_quantities(symbol, total_equity, best_ask_price, max_leverage)
                logging.info(f"Initialized quantities for {symbol}. Initial long qty: {self.initial_max_long_trade_qty_per_symbol.get(symbol, 'N/A')}, Initial short qty: {self.initial_max_short_trade_qty_per_symbol.get(symbol, 'N/A')}")
                self.initialized_symbols.add(symbol)
                return True
            else:
                logging.info(f"{symbol} is already initialized.")
                return False

    def adjust_risk_parameters(self, exchange_max_leverage):
        """
        Adjust risk parameters based on user preferences.
        
        :param exchange_max_leverage: The maximum leverage allowed by the exchange.
        """
        # Ensure the wallet exposure limit is within a practical range (0.1% to 100%)
        self.wallet_exposure_limit = min(self.wallet_exposure_limit, 1.0)

        # Check if user-defined leverage is zero and adjust to use exchange's maximum if true
        if self.user_defined_leverage_long == 0:
            self.user_defined_leverage_long = exchange_max_leverage
        else:
            # Otherwise, ensure it's between 1 and the exchange maximum
            self.user_defined_leverage_long = max(1, min(self.user_defined_leverage_long, exchange_max_leverage))

        if self.user_defined_leverage_short == 0:
            self.user_defined_leverage_short = exchange_max_leverage
        else:
            # Otherwise, ensure it's between 1 and the exchange maximum
            self.user_defined_leverage_short = max(1, min(self.user_defined_leverage_short, exchange_max_leverage))
        
        logging.info(f"Wallet exposure limit set to {self.wallet_exposure_limit*100}%")
        logging.info(f"User-defined leverage for long positions set to {self.user_defined_leverage_long}x")
        logging.info(f"User-defined leverage for short positions set to {self.user_defined_leverage_short}x")

    # Handle the calculation of trade quantities per symbol
    def handle_trade_quantities(self, symbol, total_equity, best_ask_price):
        if symbol not in self.initialized_symbols:
            max_trade_qty = self.calculate_max_trade_qty(symbol, total_equity, best_ask_price)
            self.max_trade_qty_per_symbol[symbol] = max_trade_qty
            self.initialized_symbols.add(symbol)
            logging.info(f"Symbol {symbol} initialization: Max Trade Qty: {max_trade_qty}, Total Equity: {total_equity}, Best Ask Price: {best_ask_price}")

        dynamic_amount = self.calculate_dynamic_amount(symbol, total_equity, best_ask_price)
        self.dynamic_amount_per_symbol[symbol] = dynamic_amount
        logging.info(f"Dynamic Amount Updated: Symbol: {symbol}, Dynamic Amount: {dynamic_amount}")

    # Calculate maximum trade quantity for a symbol
    def calculate_max_trade_qty(self, symbol, total_equity, best_ask_price, max_leverage):
        leveraged_equity = total_equity * max_leverage
        max_trade_qty = (self.dynamic_amount_multiplier * leveraged_equity) / best_ask_price
        logging.info(f"Calculating Max Trade Qty: Symbol: {symbol}, Leveraged Equity: {leveraged_equity}, Max Trade Qty: {max_trade_qty}")
        return max_trade_qty

    
    def check_amount_validity_bybit(self, amount, symbol):
        market_data = self.exchange.get_market_data_bybit(symbol)
        min_qty_bybit = market_data["min_qty"]
        if float(amount) < min_qty_bybit:
            logging.info(f"The amount you entered ({amount}) is less than the minimum required by Bybit for {symbol}: {min_qty_bybit}.")
            return False
        else:
            logging.info(f"The amount you entered ({amount}) is valid for {symbol}")
            return True

    def check_amount_validity_once_bybit(self, amount, symbol):
        if not self.check_amount_validity_bybit:
            market_data = self.exchange.get_market_data_bybit(symbol)
            min_qty_bybit = market_data["min_qty"]
            if float(amount) < min_qty_bybit:
                logging.info(f"The amount you entered ({amount}) is less than the minimum required by Bybit for {symbol}: {min_qty_bybit}.")
                return False
            else:
                logging.info(f"The amount you entered ({amount}) is valid for {symbol}")
                return True

    def can_proceed_with_trade_funding(self, symbol: str) -> dict:
        """
        Check if we can proceed with a long or short trade based on the funding rate.

        Parameters:
            symbol (str): The trading symbol to check.
            
        Returns:
            dict: A dictionary containing boolean values for 'can_long' and 'can_short'.
        """
        # Initialize the result dictionary
        result = {
            'can_long': False,
            'can_short': False
        }

        # Retrieve the maximum absolute funding rate from config
        max_abs_funding_rate = self.config.MaxAbsFundingRate

        # Get the current funding rate for the symbol
        funding_rate = self.get_funding_rate(symbol)
        
        # If funding_rate is None, we can't make a decision
        if funding_rate is None:
            return result
        
        # Check conditions for long and short trades
        if funding_rate <= max_abs_funding_rate:
            result['can_long'] = True

        if funding_rate >= -max_abs_funding_rate:
            result['can_short'] = True

        return result
    
    def can_place_order(self, symbol, interval=60):
        with self.lock:
            current_time = time.time()
            logging.info(f"Attempting to check if an order can be placed for {symbol} at {current_time}")
            
            if symbol in self.last_order_time:
                time_difference = current_time - self.last_order_time[symbol]
                logging.info(f"Time since last order for {symbol}: {time_difference} seconds")
                
                if time_difference <= interval:
                    logging.warning(f"Rate limit exceeded for {symbol}. Denying order placement.")
                    return False
                
            self.last_order_time[symbol] = current_time
            logging.info(f"Order allowed for {symbol} at {current_time}")
            return True

    def bybit_hedge_placetp_maker(self, symbol, pos_qty, take_profit_price, positionIdx, order_side, open_orders):
        logging.info(f"TP maker function Trying to place TP for {symbol}")
        existing_tps = self.get_open_take_profit_order_quantities(open_orders, order_side)
        logging.info(f"Existing TP from TP maker functions: {existing_tps}")
        total_existing_tp_qty = sum(qty for qty, _ in existing_tps)
        logging.info(f"TP maker function Existing {order_side} TPs: {existing_tps}")

        if not math.isclose(total_existing_tp_qty, pos_qty):
            try:
                for qty, existing_tp_id in existing_tps:
                    if not math.isclose(qty, pos_qty) and existing_tp_id not in self.auto_reduce_order_ids.get(symbol, []):
                        self.exchange.cancel_order_by_id(existing_tp_id, symbol)
                        logging.info(f"{order_side.capitalize()} take profit {existing_tp_id} canceled")
                        time.sleep(0.05)
            except Exception as e:
                logging.info(f"Error in cancelling {order_side} TP orders {e}")

        if len(existing_tps) < 1:
            try:
                # Use postonly_limit_order_bybit function to place take profit order
                tp_order = self.postonly_limit_order_bybit_nolimit(symbol, order_side, pos_qty, take_profit_price, positionIdx, reduceOnly=True)
                if tp_order and 'id' in tp_order:
                    logging.info(f"{order_side.capitalize()} take profit set at {take_profit_price} with ID {tp_order['id']}")
                else:
                    logging.warning(f"Failed to place {order_side} take profit for {symbol}")
                time.sleep(0.05)
            except Exception as e:
                logging.info(f"Error in placing {order_side} TP: {e}")

    def postonly_limit_order_bybit(self, symbol, side, amount, price, positionIdx, reduceOnly=False):
        """Directly places the order with the exchange."""
        params = {"reduceOnly": reduceOnly, "postOnly": True}
        order = self.exchange.create_limit_order_bybit(symbol, side, amount, price, positionIdx=positionIdx, params=params)

        # Log and store the order ID if the order was placed successfully
        if order and 'id' in order:
            logging.info(f"Successfully placed post-only limit order for {symbol}. Order ID: {order['id']}. Side: {side}, Amount: {amount}, Price: {price}, PositionIdx: {positionIdx}, ReduceOnly: {reduceOnly}")
            if symbol not in self.order_ids:
                self.order_ids[symbol] = []
            self.order_ids[symbol].append(order['id'])
        else:
            logging.warning(f"Failed to place post-only limit order for {symbol}. Side: {side}, Amount: {amount}, Price: {price}, PositionIdx: {positionIdx}, ReduceOnly: {reduceOnly}")

        return order
      
    def place_hedge_order_bybit(self, symbol, side, amount, price, positionIdx, reduceOnly=False):
        """Places a hedge order and updates the hedging status."""
        order = self.place_postonly_order_bybit(symbol, side, amount, price, positionIdx, reduceOnly)
        if order and 'id' in order:
            self.update_hedged_status(symbol, True)
            logging.info(f"Hedge order placed for {symbol}: {order['id']}")
        else:
            logging.warning(f"Failed to place hedge order for {symbol}")
        return order

    def place_postonly_order_bybit(self, symbol, side, amount, price, positionIdx, reduceOnly=False):
        current_thread_id = threading.get_ident()  # Get the current thread ID
        logging.info(f"[Thread ID: {current_thread_id}] Attempting to place post-only order for {symbol}. Side: {side}, Amount: {amount}, Price: {price}, PositionIdx: {positionIdx}, ReduceOnly: {reduceOnly}")

        if not self.can_place_order(symbol):
            logging.warning(f"[Thread ID: {current_thread_id}] Order placement rate limit exceeded for {symbol}. Skipping...")
            return None

        return self.postonly_limit_order_bybit(symbol, side, amount, price, positionIdx, reduceOnly)

    def postonly_limit_entry_order_bybit(self, symbol, side, amount, price, positionIdx, reduceOnly=False):
        """Places a post-only limit entry order and stores its ID."""
        order = self.postonly_limit_order_bybit(symbol, side, amount, price, positionIdx, reduceOnly)
        
        # If the order was successfully placed, store its ID as an entry order ID for the symbol
        if order and 'id' in order:
            if symbol not in self.entry_order_ids:
                self.entry_order_ids[symbol] = []
            self.entry_order_ids[symbol].append(order['id'])
            logging.info(f"Stored order ID {order['id']} for symbol {symbol}. Current order IDs for {symbol}: {self.entry_order_ids[symbol]}")
        else:
            logging.warning(f"Failed to store order ID for symbol {symbol} due to missing 'id' or unsuccessful order placement.")

        return order

    def postonly_limit_order_bybit(self, symbol, side, amount, price, positionIdx, reduceOnly=False):
        """Directly places the order with the exchange."""
        params = {"reduceOnly": reduceOnly, "postOnly": True}
        order = self.exchange.create_limit_order_bybit(symbol, side, amount, price, positionIdx=positionIdx, params=params)

        # Log and store the order ID if the order was placed successfully
        if order and 'id' in order:
            logging.info(f"Successfully placed post-only limit order for {symbol}. Order ID: {order['id']}. Side: {side}, Amount: {amount}, Price: {price}, PositionIdx: {positionIdx}, ReduceOnly: {reduceOnly}")
            if symbol not in self.order_ids:
                self.order_ids[symbol] = []
            self.order_ids[symbol].append(order['id'])
        else:
            logging.warning(f"Failed to place post-only limit order for {symbol}. Side: {side}, Amount: {amount}, Price: {price}, PositionIdx: {positionIdx}, ReduceOnly: {reduceOnly}")

        return order

    def limit_order_bybit_reduce_nolimit(self, symbol, side, amount, price, positionIdx, reduceOnly=False):
        params = {"reduceOnly": reduceOnly}
        logging.info(f"Placing {side} limit order for {symbol} at {price} with qty {amount} and params {params}...")
        try:
            order = self.exchange.create_limit_order_bybit(symbol, side, amount, price, positionIdx=positionIdx, params=params)
            logging.info(f"Order result: {order}")
            if order is None:
                logging.warning(f"Order result is None for {side} limit order on {symbol}")
            return order
        except Exception as e:
            logging.info(f"Error placing order: {str(e)}")
            logging.exception("Stack trace for error in placing order:")
            return None
            
    def postonly_limit_order_bybit_nolimit(self, symbol, side, amount, price, positionIdx, reduceOnly=False):
        params = {"reduceOnly": reduceOnly, "postOnly": True}
        logging.info(f"Placing {side} limit order for {symbol} at {price} with qty {amount} and params {params}...")
        try:
            order = self.exchange.create_limit_order_bybit(symbol, side, amount, price, positionIdx=positionIdx, params=params)
            logging.info(f"Nolimit postonly order result for {symbol}: {order}")
            if order is None:
                logging.warning(f"Order result is None for {side} limit order on {symbol}")
            return order
        except Exception as e:
            logging.info(f"Error placing order: {str(e)}")
            logging.exception("Stack trace for error in placing order:")  # This will log the full stack trace

    def postonly_limit_order_bybit_s(self, symbol, side, amount, price, positionIdx, reduceOnly=False):
        params = {"reduceOnly": reduceOnly, "postOnly": True}
        logging.info(f"Placing {side} limit order for {symbol} at {price} with qty {amount} and params {params}...")
        try:
            order = self.exchange.create_limit_order_bybit(symbol, side, amount, price, positionIdx=positionIdx, params=params)
            logging.info(f"Order result: {order}")
            if order is None:
                logging.warning(f"Order result is None for {side} limit order on {symbol}")
            return order
        except Exception as e:
            logging.info(f"Error placing order: {str(e)}")
            logging.exception("Stack trace for error in placing order:")  # This will log the full stack trace

    def limit_order_bybit(self, symbol, side, amount, price, positionIdx, reduceOnly=False):
        params = {"reduceOnly": reduceOnly}
        #print(f"Symbol: {symbol}, Side: {side}, Amount: {amount}, Price: {price}, Params: {params}")
        order = self.exchange.create_limit_order_bybit(symbol, side, amount, price, positionIdx=positionIdx, params=params)
        return order


    def limit_order_bybit_unified(self, symbol, side, amount, price, positionIdx, reduceOnly=False):
        params = {"reduceOnly": reduceOnly}
        #print(f"Symbol: {symbol}, Side: {side}, Amount: {amount}, Price: {price}, Params: {params}")
        order = self.exchange.create_limit_order_bybit_unified(symbol, side, amount, price, positionIdx=positionIdx, params=params)
        return order

    def limit_order_bybit(self, symbol, side, amount, price, positionIdx, reduceOnly=False):
        params = {"reduceOnly": reduceOnly}
        #print(f"Symbol: {symbol}, Side: {side}, Amount: {amount}, Price: {price}, Params: {params}")
        order = self.exchange.create_limit_order_bybit(symbol, side, amount, price, positionIdx=positionIdx, params=params)
        return order

    def update_take_profit_if_profitable_for_one_minute(self, symbol):
        try:
            # Fetch position data and open position data
            position_data = self.exchange.get_positions_bybit(symbol)
            open_position_data = self.exchange.get_all_open_positions_bybit()
            current_time = datetime.utcnow()

            # Fetch current order book prices
            best_ask_price = float(self.exchange.get_orderbook(symbol)['asks'][0][0])
            best_bid_price = float(self.exchange.get_orderbook(symbol)['bids'][0][0])

            # Initialize next_tp_update using your calculate_next_update_time function
            next_tp_update = self.calculate_next_update_time()

            # Loop through all open positions to find the one for the given symbol
            for position in open_position_data:
                if position['symbol'].split(':')[0] == symbol:
                    timestamp = datetime.utcfromtimestamp(position['timestamp'] / 1000.0)  # Convert to seconds from milliseconds
                    time_in_position = current_time - timestamp

                    # Check if the position has been open for more than a minute
                    if time_in_position > timedelta(minutes=1):
                        side = position['side']
                        pos_qty = position['contracts']
                        entry_price = position['entryPrice']

                        # Check if the position is profitable
                        is_profitable = (best_ask_price > entry_price) if side == 'long' else (best_bid_price < entry_price)

                        if is_profitable:
                            # Calculate take profit price based on the current price
                            take_profit_price = best_ask_price if side == 'long' else best_bid_price

                            # Fetch open orders for the symbol
                            open_orders = self.retry_api_call(self.exchange.get_open_orders, symbol)

                            # Update the take profit
                            positionIdx = 1 if side == 'long' else 2
                            order_side = 'Buy' if side == 'short' else 'Sell'

                            next_tp_update = self.update_take_profit_spread_bybit(
                                symbol, pos_qty, take_profit_price, positionIdx, order_side,
                                open_orders, next_tp_update
                            )
                            logging.info(f"Updated take profit for {side} position on {symbol} to {take_profit_price}")

        except Exception as e:
            logging.info(f"Error in updating take profit: {e}")

    def update_dynamic_quickscalp_tp(self, symbol, best_ask_price, best_bid_price, pos_qty, upnl_profit_pct, short_pos_price, long_pos_price, positionIdx, order_side, last_tp_update, tp_order_counts, max_retries=10):
        # Fetch the current open TP orders and TP order counts for the symbol
        long_tp_orders, short_tp_orders = self.exchange.get_open_tp_orders(symbol)

        long_tp_count = tp_order_counts['long_tp_count']
        short_tp_count = tp_order_counts['short_tp_count']

        # Calculate the new TP values using quickscalp method w/ dynamic
        new_short_tp = self.calculate_dynamic_short_take_profit(
            best_ask_price,
            short_pos_price,
            symbol,
            upnl_profit_pct
        )

        new_long_tp = self.calculate_dynamic_long_take_profit(
            best_bid_price,
            long_pos_price,
            symbol,
            upnl_profit_pct
        )

        # Determine the relevant TP orders based on the order side
        relevant_tp_orders = long_tp_orders if order_side == "sell" else short_tp_orders

        # Check if there's an existing TP order with a mismatched quantity
        mismatched_qty_orders = [order for order in relevant_tp_orders if order['qty'] != pos_qty]

        # Cancel mismatched TP orders if any
        for order in mismatched_qty_orders:
            try:
                self.exchange.cancel_order_by_id(order['id'], symbol)
                logging.info(f"Cancelled TP order {order['id']} for update.")
                time.sleep(0.05)
            except Exception as e:
                logging.info(f"Error in cancelling {order_side} TP order {order['id']}. Error: {e}")

        now = datetime.now()
        if now >= last_tp_update or mismatched_qty_orders:
            # Check if a TP order already exists
            tp_order_exists = (order_side == "sell" and long_tp_count > 0) or (order_side == "buy" and short_tp_count > 0)

            # Set new TP order with updated prices only if no TP order exists
            if not tp_order_exists:
                new_tp_price = new_long_tp if order_side == "sell" else new_short_tp
                try:
                    self.exchange.create_take_profit_order_bybit(symbol, "limit", order_side, pos_qty, new_tp_price, positionIdx=positionIdx, reduce_only=True)
                    logging.info(f"New {order_side.capitalize()} TP set at {new_tp_price}")
                except Exception as e:
                    logging.info(f"Failed to set new {order_side} TP for {symbol}. Error: {e}")
            else:
                logging.info(f"Skipping TP update as a TP order already exists for {symbol}")

            # Calculate and return the next update time
            return self.calculate_next_update_time()
        else:
            logging.info(f"No immediate update needed for TP orders for {symbol}. Last update at: {last_tp_update}")
            return last_tp_update


    def place_long_tp_order(self, symbol, best_ask_price, long_pos_price, long_pos_qty, long_take_profit, open_orders):
        try:
            if long_pos_qty > 0:  # Check if long position quantity is greater than 0
                tp_order_counts = self.exchange.get_open_tp_order_count(symbol)
                logging.info(f"Long TP order counts for {symbol}: {tp_order_counts}")
                if tp_order_counts['long_tp_count'] == 0:
                    if long_pos_price is not None and best_ask_price is not None and long_pos_price >= long_take_profit:
                        long_take_profit = best_ask_price
                        logging.info(f"Adjusted long TP to current bid price for {symbol}: {long_take_profit}")
                    if long_take_profit is not None:
                        logging.info(f"Placing long TP order for {symbol} at {long_take_profit} with {long_pos_qty}")
                        self.bybit_hedge_placetp_maker(symbol, long_pos_qty, long_take_profit, positionIdx=1, order_side="sell", open_orders=open_orders)
        except Exception as e:
            logging.info(f"Exception caught in placing long TP order for {symbol}: {e}")

    def place_short_tp_order(self, symbol, best_bid_price, short_pos_price, short_pos_qty, short_take_profit, open_orders):
        try:
            if short_pos_qty > 0:  # Check if short position quantity is greater than 0
                tp_order_counts = self.exchange.get_open_tp_order_count(symbol)
                logging.info(f"Short TP order counts for {symbol}: {tp_order_counts}")
                if tp_order_counts['short_tp_count'] == 0:
                    if short_pos_price is not None and best_bid_price is not None and short_pos_price <= short_take_profit:
                        short_take_profit = best_bid_price
                        logging.info(f"Adjusted short TP to current ask price for {symbol}: {short_take_profit}")
                    if short_take_profit is not None:
                        logging.info(f"Placing short TP order for {symbol} at {short_take_profit} with {short_pos_qty}")
                        self.bybit_hedge_placetp_maker(symbol, short_pos_qty, short_take_profit, positionIdx=2, order_side="buy", open_orders=open_orders)
        except Exception as e:
            logging.info(f"Exception caught in placing short TP order for {symbol}: {e}")
            
    def entry_order_exists(self, open_orders, side):
        for order in open_orders:
            # Assuming order details include 'info' which contains the API's raw response
            if order.get("info", {}).get("orderLinkId", "").startswith("helperOrder"):
                continue  # Skip helper orders based on their unique identifier
            
            if order["side"].lower() == side and not order.get("reduceOnly", False):
                logging.info(f"An entry order for side {side} already exists.")
                return True
        
        logging.info(f"No entry order found for side {side}, excluding helper orders.")
        return False
    
    def calculate_dynamic_amounts_notional(self, symbol, total_equity, best_ask_price, best_bid_price):
        """
        Calculate the dynamic entry sizes for both long and short positions based on wallet exposure limit and user-defined leverage,
        ensuring compliance with the exchange's minimum notional value requirements.

        :param symbol: Trading symbol.
        :param total_equity: Total equity in the wallet.
        :param best_ask_price: Current best ask price of the symbol for buying (long entry).
        :param best_bid_price: Current best bid price of the symbol for selling (short entry).
        :return: A tuple containing entry sizes for long and short trades.
        """
        # Calculate the minimum notional value based on the symbol
        if symbol in ["BTCUSDT", "BTC-PERP"]:
            min_notional_value = 101  # Slightly above 100 to ensure orders are above the minimum
        elif symbol in ["ETHUSDT", "ETH-PERP"] or symbol.endswith("USDC"):
            min_notional_value = 21  # Slightly above 20 to ensure orders are above the minimum
        else:
            min_notional_value = 6  # Slightly above 5 to ensure orders are above the minimum

        # Calculate dynamic entry sizes based on risk parameters
        max_equity_for_long_trade = total_equity * self.wallet_exposure_limit
        max_long_position_value = max_equity_for_long_trade * self.user_defined_leverage_long
        logging.info(f"Max long pos value for {symbol} : {max_long_position_value}")
        long_entry_size = max(max_long_position_value / best_ask_price, min_notional_value / best_ask_price)

        max_equity_for_short_trade = total_equity * self.wallet_exposure_limit
        max_short_position_value = max_equity_for_short_trade * self.user_defined_leverage_short
        logging.info(f"Max short pos value for {symbol} : {max_short_position_value}")
        short_entry_size = max(max_short_position_value / best_bid_price, min_notional_value / best_bid_price)

        # Adjusting entry sizes based on the symbol's minimum quantity precision
        qty_precision = self.exchange.get_symbol_precision_bybit(symbol)[1]
        if qty_precision is None:
            long_entry_size_adjusted = math.ceil(long_entry_size)
            short_entry_size_adjusted = math.ceil(short_entry_size)
        else:
            long_entry_size_adjusted = math.ceil(long_entry_size / qty_precision) * qty_precision
            short_entry_size_adjusted = math.ceil(short_entry_size / qty_precision) * qty_precision

        # Ensure the adjusted entry sizes meet the minimum notional value requirement
        long_entry_size_adjusted = max(long_entry_size_adjusted, math.ceil(min_notional_value / best_ask_price / qty_precision) * qty_precision)
        short_entry_size_adjusted = max(short_entry_size_adjusted, math.ceil(min_notional_value / best_bid_price / qty_precision) * qty_precision)

        logging.info(f"Calculated long entry size for {symbol}: {long_entry_size_adjusted} units")
        logging.info(f"Calculated short entry size for {symbol}: {short_entry_size_adjusted} units")

        return long_entry_size_adjusted, short_entry_size_adjusted

    def calculate_dynamic_amounts(self, symbol, total_equity, best_ask_price, best_bid_price):
        """
        Calculate the dynamic entry sizes for both long and short positions based on wallet exposure limit and user-defined leverage,
        ensuring compliance with the exchange's minimum trade quantity in USD value.
        
        :param symbol: Trading symbol.
        :param total_equity: Total equity in the wallet.
        :param best_ask_price: Current best ask price of the symbol for buying (long entry).
        :param best_bid_price: Current best bid price of the symbol for selling (short entry).
        :return: A tuple containing entry sizes for long and short trades.
        """
        # Fetch market data to get the minimum trade quantity for the symbol
        market_data = self.get_market_data_with_retry(symbol, max_retries=100, retry_delay=5)
        min_qty = float(market_data["min_qty"])
        # Simplify by using best_ask_price for min_qty in USD value calculation
        min_qty_usd_value = min_qty * best_ask_price

        # Calculate dynamic entry sizes based on risk parameters
        max_equity_for_long_trade = total_equity * self.wallet_exposure_limit
        max_long_position_value = max_equity_for_long_trade * self.user_defined_leverage_long

        logging.info(f"Max long pos value for {symbol} : {max_long_position_value}")

        long_entry_size = max(max_long_position_value / best_ask_price, min_qty_usd_value / best_ask_price)

        max_equity_for_short_trade = total_equity * self.wallet_exposure_limit
        max_short_position_value = max_equity_for_short_trade * self.user_defined_leverage_short

        logging.info(f"Max short pos value for {symbol} : {max_short_position_value}")
        
        short_entry_size = max(max_short_position_value / best_bid_price, min_qty_usd_value / best_bid_price)

        # Adjusting entry sizes based on the symbol's minimum quantity precision
        qty_precision = self.exchange.get_symbol_precision_bybit(symbol)[1]
        if qty_precision is None:
            long_entry_size_adjusted = round(long_entry_size)
            short_entry_size_adjusted = round(short_entry_size)
        else:
            long_entry_size_adjusted = round(long_entry_size, -int(math.log10(qty_precision)))
            short_entry_size_adjusted = round(short_entry_size, -int(math.log10(qty_precision)))

        logging.info(f"Calculated long entry size for {symbol}: {long_entry_size_adjusted} units")
        logging.info(f"Calculated short entry size for {symbol}: {short_entry_size_adjusted} units")

        return long_entry_size_adjusted, short_entry_size_adjusted

    def calculate_dynamic_amounts_notional_bybit_2(self, symbol, total_equity, best_ask_price, best_bid_price):
        """
        Calculate the dynamic entry sizes for both long and short positions based on wallet exposure limit and user-defined leverage,
        ensuring compliance with the exchange's minimum notional value requirements.
        
        :param symbol: Trading symbol.
        :param total_equity: Total equity in the wallet.
        :param best_ask_price: Current best ask price of the symbol for buying (long entry).
        :param best_bid_price: Current best bid price of the symbol for selling (short entry).
        :return: A tuple containing entry sizes for long and short trades.
        """
        # Fetch the exchange's minimum quantity for the symbol
        min_qty = self.exchange.get_min_qty_bybit(symbol)
        
        # Set the minimum notional value based on the contract type
        if symbol in ["BTCUSDT", "BTC-PERP"]:
            min_notional_value = 100  # $100 for BTCUSDT and BTC-PERP
        elif symbol in ["ETHUSDT", "ETH-PERP"]:
            min_notional_value = 20  # $20 for ETHUSDT and ETH-PERP
        else:
            min_notional_value = 5  # $5 for other USDT and USDC perpetual contracts
        
        # Check if the exchange's minimum quantity meets the minimum notional value requirement
        if min_qty * best_ask_price < min_notional_value:
            min_qty_long = min_notional_value / best_ask_price
        else:
            min_qty_long = min_qty
        
        if min_qty * best_bid_price < min_notional_value:
            min_qty_short = min_notional_value / best_bid_price
        else:
            min_qty_short = min_qty
        
        # Calculate dynamic entry sizes based on risk parameters
        max_equity_for_long_trade = total_equity * self.wallet_exposure_limit
        max_long_position_value = max_equity_for_long_trade * self.user_defined_leverage_long

        logging.info(f"Max long pos value for {symbol} : {max_long_position_value}")

        long_entry_size = max(max_long_position_value / best_ask_price, min_qty_long)

        max_equity_for_short_trade = total_equity * self.wallet_exposure_limit
        max_short_position_value = max_equity_for_short_trade * self.user_defined_leverage_short

        logging.info(f"Max short pos value for {symbol} : {max_short_position_value}")
        
        short_entry_size = max(max_short_position_value / best_bid_price, min_qty_short)

        # Adjusting entry sizes based on the symbol's minimum quantity precision
        qty_precision = self.exchange.get_symbol_precision_bybit(symbol)[1]
        long_entry_size_adjusted = round(long_entry_size, -int(math.log10(qty_precision)))
        short_entry_size_adjusted = round(short_entry_size, -int(math.log10(qty_precision)))

        logging.info(f"Calculated long entry size for {symbol}: {long_entry_size_adjusted} units")
        logging.info(f"Calculated short entry size for {symbol}: {short_entry_size_adjusted} units")

        return long_entry_size_adjusted, short_entry_size_adjusted

    def calculate_dynamic_amounts_notional_bybit(self, symbol, total_equity, best_ask_price, best_bid_price):
        """
        Calculate the dynamic entry sizes for both long and short positions based on wallet exposure limit and user-defined leverage,
        ensuring compliance with the exchange's minimum notional value requirements.
        
        :param symbol: Trading symbol.
        :param total_equity: Total equity in the wallet.
        :param best_ask_price: Current best ask price of the symbol for buying (long entry).
        :param best_bid_price: Current best bid price of the symbol for selling (short entry).
        :return: A tuple containing entry sizes for long and short trades.
        """
        # Set the minimum notional value based on the contract type
        if symbol in ["BTCUSDT", "BTC-PERP"]:
            min_notional_value = 100  # $100 for BTCUSDT and BTC-PERP
        elif symbol in ["ETHUSDT", "ETH-PERP"]:
            min_notional_value = 20  # $20 for ETHUSDT and ETH-PERP
        else:
            min_notional_value = 5  # $5 for other USDT and USDC perpetual contracts
        
        # Calculate dynamic entry sizes based on risk parameters
        max_equity_for_long_trade = total_equity * self.wallet_exposure_limit
        max_long_position_value = max_equity_for_long_trade * self.user_defined_leverage_long

        logging.info(f"Max long pos value for {symbol} : {max_long_position_value}")

        long_entry_size = max(max_long_position_value / best_ask_price, min_notional_value / best_ask_price)

        max_equity_for_short_trade = total_equity * self.wallet_exposure_limit
        max_short_position_value = max_equity_for_short_trade * self.user_defined_leverage_short

        logging.info(f"Max short pos value for {symbol} : {max_short_position_value}")
        
        short_entry_size = max(max_short_position_value / best_bid_price, min_notional_value / best_bid_price)

        # Adjusting entry sizes based on the symbol's minimum quantity precision
        qty_precision = self.exchange.get_symbol_precision_bybit(symbol)[1]
        long_entry_size_adjusted = round(long_entry_size, -int(math.log10(qty_precision)))
        short_entry_size_adjusted = round(short_entry_size, -int(math.log10(qty_precision)))

        logging.info(f"Calculated long entry size for {symbol}: {long_entry_size_adjusted} units")
        logging.info(f"Calculated short entry size for {symbol}: {short_entry_size_adjusted} units")

        return long_entry_size_adjusted, short_entry_size_adjusted

    def calculate_dynamic_amount_obstrength(self, symbol, total_equity, best_ask_price, max_leverage):
        self.initialize_trade_quantities(symbol, total_equity, best_ask_price, max_leverage)

        market_data = self.get_market_data_with_retry(symbol, max_retries=100, retry_delay=5)
        min_qty = float(market_data["min_qty"])
        logging.info(f"Min qty for {symbol} : {min_qty}")

        # Starting with 0.1% of total equity for both long and short orders
        long_dynamic_amount = 0.001 * total_equity
        short_dynamic_amount = 0.001 * total_equity

        # Calculate the order book strength
        strength = self.calculate_orderbook_strength(symbol)
        logging.info(f"OB strength: {strength}")

        # Reduce the aggressive multiplier from 10 to 5
        aggressive_steps = max(0, (strength - 0.5) * 5)  # This ensures values are always non-negative
        long_dynamic_amount += aggressive_steps * min_qty
        short_dynamic_amount += aggressive_steps * min_qty

        logging.info(f"Long dynamic amount for {symbol} {long_dynamic_amount}")
        logging.info(f"Short dynamic amount for {symbol} {short_dynamic_amount}")

        # Reduce the maximum allowed dynamic amount to be more conservative
        AGGRESSIVE_MAX_PCT_EQUITY = 0.05  # 5% of the total equity
        max_allowed_dynamic_amount = AGGRESSIVE_MAX_PCT_EQUITY * total_equity
        logging.info(f"Max allowed dynamic amount for {symbol} : {max_allowed_dynamic_amount}")

        # Determine precision level directly
        precision_level = len(str(min_qty).split('.')[-1]) if '.' in str(min_qty) else 0
        logging.info(f"min_qty: {min_qty}, precision_level: {precision_level}")

        # Round the dynamic amounts based on precision level
        long_dynamic_amount = round(long_dynamic_amount, precision_level)
        short_dynamic_amount = round(short_dynamic_amount, precision_level)
        logging.info(f"Rounded long_dynamic_amount: {long_dynamic_amount}, short_dynamic_amount: {short_dynamic_amount}")

        # Apply the cap to the dynamic amounts
        long_dynamic_amount = min(long_dynamic_amount, max_allowed_dynamic_amount)
        short_dynamic_amount = min(short_dynamic_amount, max_allowed_dynamic_amount)

        logging.info(f"Forced min qty long_dynamic_amount: {long_dynamic_amount}, short_dynamic_amount: {short_dynamic_amount}")

        self.check_amount_validity_once_bybit(long_dynamic_amount, symbol)
        self.check_amount_validity_once_bybit(short_dynamic_amount, symbol)

        # Using min_qty if dynamic amount is too small
        if long_dynamic_amount < min_qty:
            logging.info(f"Dynamic amount too small for 0.001x, using min_qty for long_dynamic_amount")
            long_dynamic_amount = min_qty
        if short_dynamic_amount < min_qty:
            logging.info(f"Dynamic amount too small for 0.001x, using min_qty for short_dynamic_amount")
            short_dynamic_amount = min_qty

        logging.info(f"Symbol: {symbol} Final long_dynamic_amount: {long_dynamic_amount}, short_dynamic_amount: {short_dynamic_amount}")

        return long_dynamic_amount, short_dynamic_amount, min_qty

    def bybit_1m_mfi_quickscalp_trend_noeri(self, open_orders: list, symbol: str, min_vol: float, one_minute_volume: float, mfirsi: str, long_dynamic_amount: float, short_dynamic_amount: float, long_pos_qty: float, short_pos_qty: float, long_pos_price: float, short_pos_price: float, entry_during_autoreduce: bool, volume_check: bool, long_take_profit: float, short_take_profit: float, upnl_profit_pct: float, tp_order_counts: dict):
        try:
            if symbol not in self.symbol_locks:
                self.symbol_locks[symbol] = threading.Lock()

            with self.symbol_locks[symbol]:
                current_price = self.exchange.get_current_price(symbol)
                logging.info(f"Current price for {symbol}: {current_price}")

                order_book = self.exchange.get_orderbook(symbol)
                best_ask_price = order_book['asks'][0][0] if 'asks' in order_book else self.last_known_ask.get(symbol)
                best_bid_price = order_book['bids'][0][0] if 'bids' in order_book else self.last_known_bid.get(symbol)

                mfi_signal_long = mfirsi.lower() == "long"
                mfi_signal_short = mfirsi.lower() == "short"

                logging.info(f"MFI signal for {symbol}: Long={mfi_signal_long}, Short={mfi_signal_short}")
                logging.info(f"Volume check for {symbol}: Enabled={volume_check}, One-minute volume={one_minute_volume}, Min volume={min_vol}")

                # Check if volume check is enabled or not
                if not volume_check or (one_minute_volume > min_vol):
                    if not self.auto_reduce_active_long.get(symbol, False):
                        logging.info(f"Auto-reduce for long position on {symbol} is not active")
                        if long_pos_qty == 0 and mfi_signal_long and not self.entry_order_exists(open_orders, "buy"):
                            logging.info(f"Placing initial long entry order for {symbol}")
                            self.place_postonly_order_bybit(symbol, "buy", long_dynamic_amount, best_bid_price, positionIdx=1, reduceOnly=False)
                            time.sleep(1)
                            if long_pos_qty > 0:
                                logging.info(f"Initial long entry order filled for {symbol}, placing take-profit order")
                                self.place_long_tp_order(symbol, best_ask_price, long_pos_price, long_pos_qty, long_take_profit, open_orders)
                                # Update TP for long position
                                self.next_long_tp_update = self.update_quickscalp_tp(
                                    symbol=symbol,
                                    pos_qty=long_pos_qty,
                                    upnl_profit_pct=upnl_profit_pct,
                                    short_pos_price=short_pos_price,
                                    long_pos_price=long_pos_price,
                                    positionIdx=1,
                                    order_side="sell",
                                    last_tp_update=self.next_long_tp_update,
                                    tp_order_counts=tp_order_counts
                                )
                            else:
                                logging.info(f"Initial long entry order not filled for {symbol}")
                        elif long_pos_qty > 0 and mfi_signal_long and current_price < long_pos_price and not self.entry_order_exists(open_orders, "buy"):
                            if entry_during_autoreduce or not self.auto_reduce_active_long.get(symbol, False):
                                logging.info(f"Placing additional long entry order for {symbol}")
                                self.place_postonly_order_bybit(symbol, "buy", long_dynamic_amount, best_bid_price, positionIdx=1, reduceOnly=False)
                                time.sleep(1)
                                if long_pos_qty > 0:
                                    logging.info(f"Additional long entry order filled for {symbol}, placing take-profit order")
                                    self.place_long_tp_order(symbol, best_ask_price, long_pos_price, long_pos_qty, long_take_profit, open_orders)
                                    # Update TP for long position
                                    self.next_long_tp_update = self.update_quickscalp_tp(
                                        symbol=symbol,
                                        pos_qty=long_pos_qty,
                                        upnl_profit_pct=upnl_profit_pct,
                                        short_pos_price=short_pos_price,
                                        long_pos_price=long_pos_price,
                                        positionIdx=1,
                                        order_side="sell",
                                        last_tp_update=self.next_long_tp_update,
                                        tp_order_counts=tp_order_counts
                                    )
                                else:
                                    logging.info(f"Additional long entry order not filled for {symbol}")
                            else:
                                logging.info(f"Skipping additional long entry for {symbol} due to active auto-reduce.")
                        else:
                            logging.info(f"Conditions not met for long entry on {symbol}")
                    else:
                        logging.info(f"Auto-reduce for long position on {symbol} is active, skipping entry")

                    if not self.auto_reduce_active_short.get(symbol, False):
                        logging.info(f"Auto-reduce for short position on {symbol} is not active")
                        if short_pos_qty == 0 and mfi_signal_short and not self.entry_order_exists(open_orders, "sell"):
                            logging.info(f"Placing initial short entry order for {symbol}")
                            self.place_postonly_order_bybit(symbol, "sell", short_dynamic_amount, best_ask_price, positionIdx=2, reduceOnly=False)
                            time.sleep(1)
                            if short_pos_qty > 0:
                                logging.info(f"Initial short entry order filled for {symbol}, placing take-profit order")
                                self.place_short_tp_order(symbol, best_bid_price, short_pos_price, short_pos_qty, short_take_profit, open_orders)
                                # Update TP for short position
                                self.next_short_tp_update = self.update_quickscalp_tp(
                                    symbol=symbol,
                                    pos_qty=short_pos_qty,
                                    upnl_profit_pct=upnl_profit_pct,
                                    short_pos_price=short_pos_price,
                                    long_pos_price=long_pos_price,
                                    positionIdx=2,
                                    order_side="buy",
                                    last_tp_update=self.next_short_tp_update,
                                    tp_order_counts=tp_order_counts
                                )
                            else:
                                logging.info(f"Initial short entry order not filled for {symbol}")
                        elif short_pos_qty > 0 and mfi_signal_short and current_price > short_pos_price and not self.entry_order_exists(open_orders, "sell"):
                            if entry_during_autoreduce or not self.auto_reduce_active_short.get(symbol, False):
                                logging.info(f"Placing additional short entry order for {symbol}")
                                self.place_postonly_order_bybit(symbol, "sell", short_dynamic_amount, best_ask_price, positionIdx=2, reduceOnly=False)
                                time.sleep(1)
                                if short_pos_qty > 0:
                                    logging.info(f"Additional short entry order filled for {symbol}, placing take-profit order")
                                    self.place_short_tp_order(symbol, best_bid_price, short_pos_price, short_pos_qty, short_take_profit, open_orders)
                                    # Update TP for short position
                                    self.next_short_tp_update = self.update_quickscalp_tp(
                                        symbol=symbol,
                                        pos_qty=short_pos_qty,
                                        upnl_profit_pct=upnl_profit_pct,
                                        short_pos_price=short_pos_price,
                                        long_pos_price=long_pos_price,
                                        positionIdx=2,
                                        order_side="buy",
                                        last_tp_update=self.next_short_tp_update,
                                        tp_order_counts=tp_order_counts
                                    )
                                else:
                                    logging.info(f"Additional short entry order not filled for {symbol}")
                            else:
                                logging.info(f"Skipping additional short entry for {symbol} due to active auto-reduce.")
                        else:
                            logging.info(f"Conditions not met for short entry on {symbol}")
                    else:
                        logging.info(f"Auto-reduce for short position on {symbol} is active, skipping entry")
                else:
                    logging.info(f"Volume check failed for {symbol}, skipping entry")

                time.sleep(5)
        except Exception as e:
            logging.info(f"Exception caught in quickscalp trend: {e}")
            
    def bybit_1m_mfi_quickscalp_ema_trend(self, open_orders: list, symbol: str, min_vol: float, one_minute_volume: float, mfirsi: str, ema_trend: str, long_dynamic_amount: float, short_dynamic_amount: float, long_pos_qty: float, short_pos_qty: float, long_pos_price: float, short_pos_price: float, entry_during_autoreduce: bool, volume_check: bool, long_take_profit: float, short_take_profit: float, upnl_profit_pct: float, tp_order_counts: dict):
        try:
            if symbol not in self.symbol_locks:
                self.symbol_locks[symbol] = threading.Lock()

            with self.symbol_locks[symbol]:
                current_price = self.exchange.get_current_price(symbol)
                logging.info(f"Current price for {symbol}: {current_price}")

                order_book = self.exchange.get_orderbook(symbol)
                best_ask_price = order_book['asks'][0][0] if 'asks' in order_book else self.last_known_ask.get(symbol)
                best_bid_price = order_book['bids'][0][0] if 'bids' in order_book else self.last_known_bid.get(symbol)

                mfi_signal_long = mfirsi.lower() == "long"
                mfi_signal_short = mfirsi.lower() == "short"

                logging.info(f"MFI signal for {symbol}: Long={mfi_signal_long}, Short={mfi_signal_short}")
                logging.info(f"EMA trend for {symbol}: {ema_trend}")
                logging.info(f"Volume check for {symbol}: Enabled={volume_check}, One-minute volume={one_minute_volume}, Min volume={min_vol}")

                # Check if volume check is enabled or not
                if not volume_check or (one_minute_volume > min_vol):
                    if not self.auto_reduce_active_long.get(symbol, False):
                        logging.info(f"Auto-reduce for long position on {symbol} is not active")
                        if long_pos_qty == 0 and mfi_signal_long and ema_trend == "long" and not self.entry_order_exists(open_orders, "buy"):
                            logging.info(f"Placing initial long entry order for {symbol}")
                            self.place_postonly_order_bybit(symbol, "buy", long_dynamic_amount, best_bid_price, positionIdx=1, reduceOnly=False)
                            time.sleep(1)
                            if long_pos_qty > 0:
                                logging.info(f"Initial long entry order filled for {symbol}, placing take-profit order")
                                self.place_long_tp_order(symbol, best_ask_price, long_pos_price, long_pos_qty, long_take_profit, open_orders)
                                # Update TP for long position
                                self.next_long_tp_update = self.update_quickscalp_tp(
                                    symbol=symbol,
                                    pos_qty=long_pos_qty,
                                    upnl_profit_pct=upnl_profit_pct,
                                    short_pos_price=short_pos_price,
                                    long_pos_price=long_pos_price,
                                    positionIdx=1,
                                    order_side="sell",
                                    last_tp_update=self.next_long_tp_update,
                                    tp_order_counts=tp_order_counts
                                )
                            else:
                                logging.info(f"Initial long entry order not filled for {symbol}")
                        elif long_pos_qty > 0 and mfi_signal_long and current_price < long_pos_price and not self.entry_order_exists(open_orders, "buy"):
                            if entry_during_autoreduce or not self.auto_reduce_active_long.get(symbol, False):
                                logging.info(f"Placing additional long entry order for {symbol}")
                                self.place_postonly_order_bybit(symbol, "buy", long_dynamic_amount, best_bid_price, positionIdx=1, reduceOnly=False)
                                time.sleep(1)
                                if long_pos_qty > 0:
                                    logging.info(f"Additional long entry order filled for {symbol}, placing take-profit order")
                                    self.place_long_tp_order(symbol, best_ask_price, long_pos_price, long_pos_qty, long_take_profit, open_orders)
                                    # Update TP for long position
                                    self.next_long_tp_update = self.update_quickscalp_tp(
                                        symbol=symbol,
                                        pos_qty=long_pos_qty,
                                        upnl_profit_pct=upnl_profit_pct,
                                        short_pos_price=short_pos_price,
                                        long_pos_price=long_pos_price,
                                        positionIdx=1,
                                        order_side="sell",
                                        last_tp_update=self.next_long_tp_update,
                                        tp_order_counts=tp_order_counts
                                    )
                                else:
                                    logging.info(f"Additional long entry order not filled for {symbol}")
                            else:
                                logging.info(f"Skipping additional long entry for {symbol} due to active auto-reduce.")
                        else:
                            logging.info(f"Conditions not met for long entry on {symbol}")
                    else:
                        logging.info(f"Auto-reduce for long position on {symbol} is active, skipping entry")

                    if not self.auto_reduce_active_short.get(symbol, False):
                        logging.info(f"Auto-reduce for short position on {symbol} is not active")
                        if short_pos_qty == 0 and mfi_signal_short and ema_trend == "short" and not self.entry_order_exists(open_orders, "sell"):
                            logging.info(f"Placing initial short entry order for {symbol}")
                            self.place_postonly_order_bybit(symbol, "sell", short_dynamic_amount, best_ask_price, positionIdx=2, reduceOnly=False)
                            time.sleep(1)
                            if short_pos_qty > 0:
                                logging.info(f"Initial short entry order filled for {symbol}, placing take-profit order")
                                self.place_short_tp_order(symbol, best_bid_price, short_pos_price, short_pos_qty, short_take_profit, open_orders)
                                # Update TP for short position
                                self.next_short_tp_update = self.update_quickscalp_tp(
                                    symbol=symbol,
                                    pos_qty=short_pos_qty,
                                    upnl_profit_pct=upnl_profit_pct,
                                    short_pos_price=short_pos_price,
                                    long_pos_price=long_pos_price,
                                    positionIdx=2,
                                    order_side="buy",
                                    last_tp_update=self.next_short_tp_update,
                                    tp_order_counts=tp_order_counts
                                )
                            else:
                                logging.info(f"Initial short entry order not filled for {symbol}")
                        elif short_pos_qty > 0 and mfi_signal_short and current_price > short_pos_price and not self.entry_order_exists(open_orders, "sell"):
                            if entry_during_autoreduce or not self.auto_reduce_active_short.get(symbol, False):
                                logging.info(f"Placing additional short entry order for {symbol}")
                                self.place_postonly_order_bybit(symbol, "sell", short_dynamic_amount, best_ask_price, positionIdx=2, reduceOnly=False)
                                time.sleep(1)
                                if short_pos_qty > 0:
                                    logging.info(f"Additional short entry order filled for {symbol}, placing take-profit order")
                                    self.place_short_tp_order(symbol, best_bid_price, short_pos_price, short_pos_qty, short_take_profit, open_orders)
                                    # Update TP for short position
                                    self.next_short_tp_update = self.update_quickscalp_tp(
                                        symbol=symbol,
                                        pos_qty=short_pos_qty,
                                        upnl_profit_pct=upnl_profit_pct,
                                        short_pos_price=short_pos_price,
                                        long_pos_price=long_pos_price,
                                        positionIdx=2,
                                        order_side="buy",
                                        last_tp_update=self.next_short_tp_update,
                                        tp_order_counts=tp_order_counts
                                    )
                                else:
                                    logging.info(f"Additional short entry order not filled for {symbol}")
                            else:
                                logging.info(f"Skipping additional short entry for {symbol} due to active auto-reduce.")
                        else:
                            logging.info(f"Conditions not met for short entry on {symbol}")
                    else:
                        logging.info(f"Auto-reduce for short position on {symbol} is active, skipping entry")
                else:
                    logging.info(f"Volume check failed for {symbol}, skipping entry")

                time.sleep(5)
        except Exception as e:
            logging.info(f"Exception caught in quickscalp trend: {e}")
            
    def bybit_1m_mfi_quickscalp_trend_eri(self, open_orders: list, symbol: str, min_vol: float, one_minute_volume: float, mfirsi: str, eri_trend: str, long_dynamic_amount: float, short_dynamic_amount: float, long_pos_qty: float, short_pos_qty: float, long_pos_price: float, short_pos_price: float, entry_during_autoreduce: bool, volume_check: bool, long_take_profit: float, short_take_profit: float, upnl_profit_pct: float, tp_order_counts: dict):
        try:
            if symbol not in self.symbol_locks:
                self.symbol_locks[symbol] = threading.Lock()

            with self.symbol_locks[symbol]:
                current_price = self.exchange.get_current_price(symbol)
                logging.info(f"Current price for {symbol}: {current_price}")

                order_book = self.exchange.get_orderbook(symbol)
                best_ask_price = order_book['asks'][0][0] if 'asks' in order_book else self.last_known_ask.get(symbol)
                best_bid_price = order_book['bids'][0][0] if 'bids' in order_book else self.last_known_bid.get(symbol)

                mfi_signal_long = mfirsi.lower() == "long"
                mfi_signal_short = mfirsi.lower() == "short"

                logging.info(f"MFI signal for {symbol}: Long={mfi_signal_long}, Short={mfi_signal_short}")
                logging.info(f"ERI trend for {symbol}: {eri_trend}")
                logging.info(f"Volume check for {symbol}: Enabled={volume_check}, One-minute volume={one_minute_volume}, Min volume={min_vol}")

                # Check if volume check is enabled or not
                if not volume_check or (one_minute_volume > min_vol):
                    if not self.auto_reduce_active_long.get(symbol, False):
                        logging.info(f"Auto-reduce for long position on {symbol} is not active")
                        if long_pos_qty == 0 and mfi_signal_long and eri_trend == "bullish" and not self.entry_order_exists(open_orders, "buy"):
                            logging.info(f"Placing initial long entry order for {symbol}")
                            self.place_postonly_order_bybit(symbol, "buy", long_dynamic_amount, best_bid_price, positionIdx=1, reduceOnly=False)
                            time.sleep(1)
                            if long_pos_qty > 0:
                                logging.info(f"Initial long entry order filled for {symbol}, placing take-profit order")
                                self.place_long_tp_order(symbol, best_ask_price, long_pos_price, long_pos_qty, long_take_profit, open_orders)
                                # Update TP for long position
                                self.next_long_tp_update = self.update_quickscalp_tp(
                                    symbol=symbol,
                                    pos_qty=long_pos_qty,
                                    upnl_profit_pct=upnl_profit_pct,
                                    short_pos_price=short_pos_price,
                                    long_pos_price=long_pos_price,
                                    positionIdx=1,
                                    order_side="sell",
                                    last_tp_update=self.next_long_tp_update,
                                    tp_order_counts=tp_order_counts
                                )
                            else:
                                logging.info(f"Initial long entry order not filled for {symbol}")
                        elif long_pos_qty > 0 and mfi_signal_long and current_price < long_pos_price and not self.entry_order_exists(open_orders, "buy"):
                            if entry_during_autoreduce or not self.auto_reduce_active_long.get(symbol, False):
                                logging.info(f"Placing additional long entry order for {symbol}")
                                self.place_postonly_order_bybit(symbol, "buy", long_dynamic_amount, best_bid_price, positionIdx=1, reduceOnly=False)
                                time.sleep(1)
                                if long_pos_qty > 0:
                                    logging.info(f"Additional long entry order filled for {symbol}, placing take-profit order")
                                    self.place_long_tp_order(symbol, best_ask_price, long_pos_price, long_pos_qty, long_take_profit, open_orders)
                                    # Update TP for long position
                                    self.next_long_tp_update = self.update_quickscalp_tp(
                                        symbol=symbol,
                                        pos_qty=long_pos_qty,
                                        upnl_profit_pct=upnl_profit_pct,
                                        short_pos_price=short_pos_price,
                                        long_pos_price=long_pos_price,
                                        positionIdx=1,
                                        order_side="sell",
                                        last_tp_update=self.next_long_tp_update,
                                        tp_order_counts=tp_order_counts
                                    )
                                else:
                                    logging.info(f"Additional long entry order not filled for {symbol}")
                            else:
                                logging.info(f"Skipping additional long entry for {symbol} due to active auto-reduce.")
                        else:
                            logging.info(f"Conditions not met for long entry on {symbol}")
                    else:
                        logging.info(f"Auto-reduce for long position on {symbol} is active, skipping entry")

                    if not self.auto_reduce_active_short.get(symbol, False):
                        logging.info(f"Auto-reduce for short position on {symbol} is not active")
                        if short_pos_qty == 0 and mfi_signal_short and eri_trend == "bearish" and not self.entry_order_exists(open_orders, "sell"):
                            logging.info(f"Placing initial short entry order for {symbol}")
                            self.place_postonly_order_bybit(symbol, "sell", short_dynamic_amount, best_ask_price, positionIdx=2, reduceOnly=False)
                            time.sleep(1)
                            if short_pos_qty > 0:
                                logging.info(f"Initial short entry order filled for {symbol}, placing take-profit order")
                                self.place_short_tp_order(symbol, best_bid_price, short_pos_price, short_pos_qty, short_take_profit, open_orders)
                                # Update TP for short position
                                self.next_short_tp_update = self.update_quickscalp_tp(
                                    symbol=symbol,
                                    pos_qty=short_pos_qty,
                                    upnl_profit_pct=upnl_profit_pct,
                                    short_pos_price=short_pos_price,
                                    long_pos_price=long_pos_price,
                                    positionIdx=2,
                                    order_side="buy",
                                    last_tp_update=self.next_short_tp_update,
                                    tp_order_counts=tp_order_counts
                                )
                            else:
                                logging.info(f"Initial short entry order not filled for {symbol}")
                        elif short_pos_qty > 0 and mfi_signal_short and current_price > short_pos_price and not self.entry_order_exists(open_orders, "sell"):
                            if entry_during_autoreduce or not self.auto_reduce_active_short.get(symbol, False):
                                logging.info(f"Placing additional short entry order for {symbol}")
                                self.place_postonly_order_bybit(symbol, "sell", short_dynamic_amount, best_ask_price, positionIdx=2, reduceOnly=False)
                                time.sleep(1)
                                if short_pos_qty > 0:
                                    logging.info(f"Additional short entry order filled for {symbol}, placing take-profit order")
                                    self.place_short_tp_order(symbol, best_bid_price, short_pos_price, short_pos_qty, short_take_profit, open_orders)
                                    # Update TP for short position
                                    self.next_short_tp_update = self.update_quickscalp_tp(
                                        symbol=symbol,
                                        pos_qty=short_pos_qty,
                                        upnl_profit_pct=upnl_profit_pct,
                                        short_pos_price=short_pos_price,
                                        long_pos_price=long_pos_price,
                                        positionIdx=2,
                                        order_side="buy",
                                        last_tp_update=self.next_short_tp_update,
                                        tp_order_counts=tp_order_counts
                                    )
                                else:
                                    logging.info(f"Additional short entry order not filled for {symbol}")
                            else:
                                logging.info(f"Skipping additional short entry for {symbol} due to active auto-reduce.")
                        else:
                            logging.info(f"Conditions not met for short entry on {symbol}")
                    else:
                        logging.info(f"Auto-reduce for short position on {symbol} is active, skipping entry")
                else:
                    logging.info(f"Volume check failed for {symbol}, skipping entry")

                time.sleep(5)
        except Exception as e:
            logging.info(f"Exception caught in quickscalp trend: {e}")
            
    def bybit_1m_mfi_quickscalp_trend_long_only(self, open_orders: list, symbol: str, min_vol: float, one_minute_volume: float, mfirsi: str, long_dynamic_amount: float, long_pos_qty: float, long_pos_price: float, volume_check: bool, long_take_profit: float, upnl_profit_pct: float, tp_order_counts: dict):
        try:
            if symbol not in self.symbol_locks:
                self.symbol_locks[symbol] = threading.Lock()

            with self.symbol_locks[symbol]:
                current_price = self.exchange.get_current_price(symbol)
                logging.info(f"Current price for {symbol}: {current_price}")

                order_book = self.exchange.get_orderbook(symbol)
                best_ask_price = order_book['asks'][0][0] if 'asks' in order_book else self.last_known_ask.get(symbol)
                best_bid_price = order_book['bids'][0][0] if 'bids' in order_book else self.last_known_bid.get(symbol)

                mfi_signal_long = mfirsi.lower() == "long"

                if not volume_check or (one_minute_volume > min_vol):
                    if long_pos_qty == 0 and mfi_signal_long and not self.entry_order_exists(open_orders, "buy"):
                        self.place_postonly_order_bybit(symbol, "buy", long_dynamic_amount, best_bid_price, positionIdx=1, reduceOnly=False)
                        time.sleep(1)
                        if long_pos_qty > 0:
                            self.place_long_tp_order(symbol, best_ask_price, long_pos_price, long_pos_qty, long_take_profit, open_orders)
                            # Update TP for long position
                            self.next_long_tp_update = self.update_quickscalp_tp(
                                symbol=symbol,
                                pos_qty=long_pos_qty,
                                upnl_profit_pct=upnl_profit_pct,
                                long_pos_price=long_pos_price,
                                positionIdx=1,
                                order_side="sell",
                                last_tp_update=self.next_long_tp_update,
                                tp_order_counts=tp_order_counts
                            )
                    elif long_pos_qty > 0 and mfi_signal_long and current_price < long_pos_price and not self.entry_order_exists(open_orders, "buy"):
                        self.place_postonly_order_bybit(symbol, "buy", long_dynamic_amount, best_bid_price, positionIdx=1, reduceOnly=False)
                        time.sleep(1)
                        if long_pos_qty > 0:
                            self.place_long_tp_order(symbol, best_ask_price, long_pos_price, long_pos_qty, long_take_profit, open_orders)
                            # Update TP for long position
                            self.next_long_tp_update = self.update_quickscalp_tp(
                                symbol=symbol,
                                pos_qty=long_pos_qty,
                                upnl_profit_pct=upnl_profit_pct,
                                long_pos_price=long_pos_price,
                                positionIdx=1,
                                order_side="sell",
                                last_tp_update=self.next_long_tp_update,
                                tp_order_counts=tp_order_counts
                            )
                else:
                    logging.info(f"Volume check is disabled or conditions not met for {symbol}, proceeding without volume check.")

                time.sleep(5)
        except Exception as e:
            logging.info(f"Exception caught in quickscalp trend long only: {e}")

    def bybit_1m_mfi_quickscalp_trend_short_only(self, open_orders: list, symbol: str, min_vol: float, one_minute_volume: float, mfirsi: str, short_dynamic_amount: float, short_pos_qty: float, short_pos_price: float, volume_check: bool, short_take_profit: float, upnl_profit_pct: float, tp_order_counts: dict):
        try:
            if symbol not in self.symbol_locks:
                self.symbol_locks[symbol] = threading.Lock()

            with self.symbol_locks[symbol]:
                current_price = self.exchange.get_current_price(symbol)
                logging.info(f"Current price for {symbol}: {current_price}")

                order_book = self.exchange.get_orderbook(symbol)
                best_ask_price = order_book['asks'][0][0] if 'asks' in order_book else self.last_known_ask.get(symbol)
                best_bid_price = order_book['bids'][0][0] if 'bids' in order_book else self.last_known_bid.get(symbol)

                mfi_signal_short = mfirsi.lower() == "short"

                if not volume_check or (one_minute_volume > min_vol):
                    if short_pos_qty == 0 and mfi_signal_short and not self.entry_order_exists(open_orders, "sell"):
                        self.place_postonly_order_bybit(symbol, "sell", short_dynamic_amount, best_ask_price, positionIdx=2, reduceOnly=False)
                        time.sleep(1)
                        if short_pos_qty > 0:
                            self.place_short_tp_order(symbol, best_bid_price, short_pos_price, short_pos_qty, short_take_profit, open_orders)
                            # Update TP for short position
                            self.next_short_tp_update = self.update_quickscalp_tp(
                                symbol=symbol,
                                pos_qty=short_pos_qty,
                                upnl_profit_pct=upnl_profit_pct,
                                short_pos_price=short_pos_price,
                                positionIdx=2,
                                order_side="buy",
                                last_tp_update=self.next_short_tp_update,
                                tp_order_counts=tp_order_counts
                            )
                    elif short_pos_qty > 0 and mfi_signal_short and current_price > short_pos_price and not self.entry_order_exists(open_orders, "sell"):
                        self.place_postonly_order_bybit(symbol, "sell", short_dynamic_amount, best_ask_price, positionIdx=2, reduceOnly=False)
                        time.sleep(1)
                        if short_pos_qty > 0:
                            self.place_short_tp_order(symbol, best_bid_price, short_pos_price, short_pos_qty, short_take_profit, open_orders)
                            # Update TP for short position
                            self.next_short_tp_update = self.update_quickscalp_tp(
                                symbol=symbol,
                                pos_qty=short_pos_qty,
                                upnl_profit_pct=upnl_profit_pct,
                                short_pos_price=short_pos_price,
                                positionIdx=2,
                                order_side="buy",
                                last_tp_update=self.next_short_tp_update,
                                tp_order_counts=tp_order_counts
                            )
                else:
                    logging.info(f"Volume check is disabled or conditions not met for {symbol}, proceeding without volume check.")

                time.sleep(5)
        except Exception as e:
            logging.info(f"Exception caught in quickscalp trend short only: {e}")

    def bybit_hedge_entry_maker_mfirsitrenderi(self, symbol, data, min_vol, min_dist, one_minute_volume, five_minute_distance, 
                                           eri_trend, open_orders, long_pos_qty, should_add_to_long, 
                                           max_long_trade_qty, best_bid_price, long_pos_price, long_dynamic_amount,
                                           short_pos_qty, should_add_to_short, max_short_trade_qty, 
                                           best_ask_price, short_pos_price, short_dynamic_amount):

        if one_minute_volume is not None and five_minute_distance is not None:
            if one_minute_volume > min_vol and five_minute_distance > min_dist:
                mfi = self.manager.get_asset_value(symbol, data, "MFI")
                trend = self.manager.get_asset_value(symbol, data, "Trend")

                if mfi is not None and isinstance(mfi, str):
                    if mfi.lower() == "neutral":
                        mfi = trend

                    # Place long orders when MFI is long and ERI trend is bearish
                    if (mfi.lower() == "long" and eri_trend.lower() == "bearish") or (mfi.lower() == "long" and trend.lower() == "long"):
                        existing_order = next((o for o in open_orders if o['side'] == 'Buy' and o['position_idx'] == 1), None)
                        if long_pos_qty == 0 or (should_add_to_long and long_pos_qty < max_long_trade_qty and best_bid_price < long_pos_price):
                            if existing_order is None or existing_order['price'] != best_bid_price:
                                if existing_order is not None:
                                    self.exchange.cancel_order_by_id(existing_order['id'], symbol)
                                logging.info(f"Placing long entry")
                                self.postonly_limit_order_bybit(symbol, "buy", long_dynamic_amount, best_bid_price, positionIdx=1, reduceOnly=False)
                                logging.info(f"Placed long entry")

                    # Place short orders when MFI is short and ERI trend is bullish
                    if (mfi.lower() == "short" and eri_trend.lower() == "bullish") or (mfi.lower() == "short" and trend.lower() == "short"):
                        existing_order = next((o for o in open_orders if o['side'] == 'Sell' and o['position_idx'] == 2), None)
                        if short_pos_qty == 0 or (should_add_to_short and short_pos_qty < max_short_trade_qty and best_ask_price > short_pos_price):
                            if existing_order is None or existing_order['price'] != best_ask_price:
                                if existing_order is not None:
                                    self.exchange.cancel_order_by_id(existing_order['id'], symbol)
                                logging.info(f"Placing short entry")
                                self.postonly_limit_order_bybit(symbol, "sell", short_dynamic_amount, best_ask_price, positionIdx=2, reduceOnly=False)
                                logging.info(f"Placed short entry")

    def bybit_hedge_entry_maker_mfirsitrend(self, symbol, data, min_vol, min_dist, one_minute_volume, five_minute_distance, 
                                            open_orders, long_pos_qty, should_add_to_long, 
                                           max_long_trade_qty, best_bid_price, long_pos_price, long_dynamic_amount,
                                           short_pos_qty, should_long: bool, should_short: bool, should_add_to_short, max_short_trade_qty, 
                                           best_ask_price, short_pos_price, short_dynamic_amount):

        if one_minute_volume is not None and five_minute_distance is not None:
            if one_minute_volume > min_vol and five_minute_distance > min_dist:
                mfi = self.manager.get_asset_value(symbol, data, "MFI")
                trend = self.manager.get_asset_value(symbol, data, "Trend")

                if mfi is not None and isinstance(mfi, str):
                    if mfi.lower() == "neutral":
                        mfi = trend

                    # Place long orders when MFI is long and ERI trend is bearish
                    if (mfi.lower() == "long" and trend.lower() == "long"):
                        existing_order = next((o for o in open_orders if o['side'] == 'Buy' and o['position_idx'] == 1), None)
                        if (should_long and long_pos_qty == 0) or (should_add_to_long and long_pos_qty < max_long_trade_qty and best_bid_price < long_pos_price):
                            if existing_order is None or existing_order['price'] != best_bid_price:
                                if existing_order is not None:
                                    self.exchange.cancel_order_by_id(existing_order['id'], symbol)
                                logging.info(f"Placing long entry")
                                self.postonly_limit_order_bybit(symbol, "buy", long_dynamic_amount, best_bid_price, positionIdx=1, reduceOnly=False)
                                logging.info(f"Placed long entry")

                    # Place short orders when MFI is short and ERI trend is bullish
                    if (mfi.lower() == "short" and trend.lower() == "short"):
                        existing_order = next((o for o in open_orders if o['side'] == 'Sell' and o['position_idx'] == 2), None)
                        if (should_short and short_pos_qty == 0) or (should_add_to_short and short_pos_qty < max_short_trade_qty and best_ask_price > short_pos_price):
                            if existing_order is None or existing_order['price'] != best_ask_price:
                                if existing_order is not None:
                                    self.exchange.cancel_order_by_id(existing_order['id'], symbol)
                                logging.info(f"Placing short entry")
                                self.postonly_limit_order_bybit(symbol, "sell", short_dynamic_amount, best_ask_price, positionIdx=2, reduceOnly=False)
                                logging.info(f"Placed short entry")

    def bybit_hedge_entry_maker_mfirsi(self, symbol, data, min_vol, min_dist, one_minute_volume, five_minute_distance, 
                                       long_pos_qty, max_long_trade_qty, best_bid_price, long_pos_price, long_dynamic_amount,
                                       short_pos_qty, max_short_trade_qty, best_ask_price, short_pos_price, short_dynamic_amount):
        if one_minute_volume is not None and five_minute_distance is not None:
            if one_minute_volume > min_vol and five_minute_distance > min_dist:
                mfi = self.manager.get_asset_value(symbol, data, "MFI")

                max_long_trade_qty_for_symbol = self.max_long_trade_qty_per_symbol.get(symbol, 0)  # Get value for symbol or default to 0
                max_short_trade_qty_for_symbol = self.max_short_trade_qty_per_symbol.get(symbol, 0)  # Get value for symbol or default to 0


                if mfi is not None and isinstance(mfi, str):
                    if mfi.lower() == "long" and long_pos_qty == 0:
                        logging.info(f"Placing initial long entry with post-only order")
                        self.postonly_limit_order_bybit(symbol, "buy", long_dynamic_amount, best_bid_price, positionIdx=1)
                        logging.info(f"Placed initial long entry with post-only order")
                    elif mfi.lower() == "long" and long_pos_qty < max_long_trade_qty_for_symbol and best_bid_price < long_pos_price:
                        logging.info(f"Placing additional long entry with post-only order")
                        self.postonly_limit_order_bybit(symbol, "buy", long_dynamic_amount, best_bid_price, positionIdx=1)
                    elif mfi.lower() == "short" and short_pos_qty == 0:
                        logging.info(f"Placing initial short entry with post-only order")
                        self.postonly_limit_order_bybit(symbol, "sell", short_dynamic_amount, best_ask_price, positionIdx=2)
                        logging.info(f"Placed initial short entry with post-only order")
                    elif mfi.lower() == "short" and short_pos_qty < max_short_trade_qty_for_symbol and best_ask_price > short_pos_price:
                        logging.info(f"Placing additional short entry with post-only order")
                        self.postonly_limit_order_bybit(symbol, "sell", short_dynamic_amount, best_ask_price, positionIdx=2)

    def bybit_1m_mfi_quickscalp_trend_long_only_spot(self, open_orders: list, symbol: str, min_vol: float, one_minute_volume: float, mfirsi: str, long_dynamic_amount: float, spot_position_qty: float, spot_position_price: float, volume_check: bool, upnl_profit_pct: float):
        try:
            if symbol not in self.symbol_locks:
                self.symbol_locks[symbol] = threading.Lock()

            with self.symbol_locks[symbol]:
                current_price = self.exchange.get_current_price(symbol)
                logging.info(f"Current price for {symbol}: {current_price}")

                order_book = self.exchange.get_orderbook(symbol)
                best_ask_price = order_book['asks'][0][0] if 'asks' in order_book else self.last_known_ask.get(symbol)
                best_bid_price = order_book['bids'][0][0] if 'bids' in order_book else self.last_known_bid.get(symbol)

                mfi_signal_long = mfirsi.lower() == "long"

                if not volume_check or (one_minute_volume > min_vol):
                    if spot_position_qty == 0 and mfi_signal_long and not self.entry_order_exists(open_orders, "buy"):
                        logging.info(f"Placing spot market buy order for {symbol} with amount {long_dynamic_amount}")
                        self.place_spot_market_order(symbol, "buy", long_dynamic_amount)
                        time.sleep(1)

                    if spot_position_qty > 0:
                        take_profit_price = spot_position_price * (1 + upnl_profit_pct)
                        logging.info(f"Placing spot limit sell order for {symbol} with amount {spot_position_qty} and take profit price {take_profit_price}")
                        self.place_spot_limit_order(symbol, "sell", spot_position_qty, take_profit_price)

                    elif spot_position_qty > 0 and mfi_signal_long and current_price < spot_position_price and not self.entry_order_exists(open_orders, "buy"):
                        logging.info(f"Placing spot market buy order to add to existing position for {symbol} with amount {long_dynamic_amount}")
                        self.place_spot_market_order(symbol, "buy", long_dynamic_amount)
                        time.sleep(1)

                else:
                    logging.info(f"Volume check is disabled or conditions not met for {symbol}, proceeding without volume check.")
                    time.sleep(5)

        except Exception as e:
            logging.info(f"Exception caught in bybit_1m_mfi_quickscalp_trend_long_only_spot: {e}")

    def linear_grid_handle_positions_mfirsi_persistent_notional_dynamic_buffer_qs(self, symbol: str, open_symbols: list, total_equity: float, long_pos_price: float, short_pos_price: float, long_pos_qty: float, short_pos_qty: float, levels: int, strength: float, outer_price_distance: float, reissue_threshold: float, wallet_exposure_limit: float, wallet_exposure_limit_long: float, wallet_exposure_limit_short: float, user_defined_leverage_long: float, user_defined_leverage_short: float, long_mode: bool, short_mode: bool, min_buffer_percentage: float, max_buffer_percentage: float, symbols_allowed: int, enforce_full_grid: bool, mfirsi_signal: str, upnl_profit_pct: float, tp_order_counts: dict, entry_during_autoreduce: bool):
        try:
            if symbol not in self.symbol_locks:
                self.symbol_locks[symbol] = threading.Lock()

            with self.symbol_locks[symbol]:
                should_reissue_long, should_reissue_short = self.should_reissue_orders_revised(symbol, reissue_threshold, long_pos_qty, short_pos_qty)
                open_orders = self.retry_api_call(self.exchange.get_open_orders, symbol)
                #logging.info(f"Open orders: {open_orders}")

                if symbol not in self.filled_levels:
                    self.filled_levels[symbol] = {"buy": set(), "sell": set()}

                long_grid_active = symbol in self.active_grids and "buy" in self.filled_levels[symbol]
                short_grid_active = symbol in self.active_grids and "sell" in self.filled_levels[symbol]

                current_price = self.exchange.get_current_price(symbol)
                logging.info(f"[{symbol}] Current price: {current_price}")

                # Handle non-existing positions by applying default buffer percentages if positions are not active
                default_buffer = min_buffer_percentage  # Could set to a reasonable default like the min_buffer_percentage

                if long_pos_price and long_pos_price > 0:
                    long_distance_from_entry = abs(current_price - long_pos_price) / long_pos_price
                    buffer_percentage_long = min_buffer_percentage + (max_buffer_percentage - min_buffer_percentage) * long_distance_from_entry
                else:
                    buffer_percentage_long = default_buffer  # Use default buffer if no valid long position

                if short_pos_price and short_pos_price > 0:
                    short_distance_from_entry = abs(current_price - short_pos_price) / short_pos_price
                    buffer_percentage_short = min_buffer_percentage + (max_buffer_percentage - min_buffer_percentage) * short_distance_from_entry
                else:
                    buffer_percentage_short = default_buffer  # Use default buffer if no valid short position

                buffer_distance_long = current_price * buffer_percentage_long
                buffer_distance_short = current_price * buffer_percentage_short

                logging.info(f"[{symbol}] Long buffer distance: {buffer_distance_long}, Short buffer distance: {buffer_distance_short}")

                order_book = self.exchange.get_orderbook(symbol)
                best_ask_price = order_book['asks'][0][0] if 'asks' in order_book else self.last_known_ask.get(symbol, current_price)
                best_bid_price = order_book['bids'][0][0] if 'bids' in order_book else self.last_known_bid.get(symbol, current_price)
                logging.info(f"[{symbol}] Best ask price: {best_ask_price}, Best bid price: {best_bid_price}")

                outer_price_long = current_price * (1 - outer_price_distance)
                outer_price_short = current_price * (1 + outer_price_distance)
                logging.info(f"[{symbol}] Outer price long: {outer_price_long}, Outer price short: {outer_price_short}")

                price_range_long = current_price - outer_price_long
                price_range_short = outer_price_short - current_price

                factors = np.linspace(0.0, 1.0, num=levels) ** strength
                grid_levels_long = [current_price - buffer_distance_long - price_range_long * factor for factor in factors]
                grid_levels_short = [current_price + buffer_distance_short + price_range_short * factor for factor in factors]

                # Check if long and short grid levels overlap
                if grid_levels_long[-1] >= grid_levels_short[0]:
                    logging.warning(f"[{symbol}] Long and short grid levels overlap. Adjusting outer_price_distance.")
                    # Adjust outer_price_distance to prevent overlap
                    outer_price_distance = (grid_levels_short[0] - grid_levels_long[-1]) / (2 * current_price)
                    # Recalculate grid levels
                    outer_price_long = current_price * (1 - outer_price_distance)
                    outer_price_short = current_price * (1 + outer_price_distance)
                    price_range_long = current_price - outer_price_long
                    price_range_short = outer_price_short - current_price
                    grid_levels_long = [current_price - buffer_distance_long - price_range_long * factor for factor in factors]
                    grid_levels_short = [current_price + buffer_distance_short + price_range_short * factor for factor in factors]

                logging.info(f"[{symbol}] Long grid levels: {grid_levels_long}")
                logging.info(f"[{symbol}] Short grid levels: {grid_levels_short}")

                # Check if grid levels need to be updated based on dynamic buffer
                replace_long_grid, replace_short_grid = self.should_replace_grid_updated_buffer(
                    symbol,
                    long_pos_price,
                    short_pos_price,
                    long_pos_qty,
                    short_pos_qty,
                    min_buffer_percentage,
                    max_buffer_percentage
                )

                if replace_long_grid:
                    long_distance_from_entry = abs(current_price - long_pos_price) / long_pos_price
                    buffer_percentage_long = min_buffer_percentage + (max_buffer_percentage - min_buffer_percentage) * long_distance_from_entry
                    buffer_distance_long = current_price * buffer_percentage_long
                    price_range_long = current_price - outer_price_long
                    grid_levels_long = [current_price - buffer_distance_long - price_range_long * factor for factor in factors]
                    logging.info(f"[{symbol}] Recalculated long grid levels with updated buffer: {grid_levels_long}")

                if replace_short_grid:
                    short_distance_from_entry = abs(current_price - short_pos_price) / short_pos_price
                    buffer_percentage_short = min_buffer_percentage + (max_buffer_percentage - min_buffer_percentage) * short_distance_from_entry
                    buffer_distance_short = current_price * buffer_percentage_short
                    price_range_short = outer_price_short - current_price
                    grid_levels_short = [current_price + buffer_distance_short + price_range_short * factor for factor in factors]
                    logging.info(f"[{symbol}] Recalculated short grid levels with updated buffer: {grid_levels_short}")
                    
                qty_precision = self.exchange.get_symbol_precision_bybit(symbol)[1]
                min_qty = float(self.get_market_data_with_retry(symbol, max_retries=100, retry_delay=5)["min_qty"])
                logging.info(f"[{symbol}] Quantity precision: {qty_precision}, Minimum quantity: {min_qty}")

                # total_amount_long = self.calculate_total_amount_notional(symbol, total_equity, best_ask_price, best_bid_price, wallet_exposure_limit, user_defined_leverage_long, "buy", levels, enforce_full_grid) if long_mode else 0
                # total_amount_short = self.calculate_total_amount_notional(symbol, total_equity, best_ask_price, best_bid_price, wallet_exposure_limit, user_defined_leverage_short, "sell", levels, enforce_full_grid) if short_mode else 0
                # logging.info(f"[{symbol}] Total amount long: {total_amount_long}, Total amount short: {total_amount_short}")

                # total_amount_long = self.calculate_total_amount_notional_ls(symbol, total_equity, best_ask_price, best_bid_price, wallet_exposure_limit_long, wallet_exposure_limit_short, user_defined_leverage_long, "buy", levels, enforce_full_grid) if long_mode else 0
                # total_amount_short = self.calculate_total_amount_notional_ls(symbol, total_equity, best_ask_price, best_bid_price, wallet_exposure_limit_long, wallet_exposure_limit_short, user_defined_leverage_short, "sell", levels, enforce_full_grid) if short_mode else 0

                total_amount_long = self.calculate_total_amount_notional_ls(
                    symbol=symbol,
                    total_equity=total_equity,
                    best_ask_price=best_ask_price,
                    best_bid_price=best_bid_price,
                    wallet_exposure_limit_long=wallet_exposure_limit_long,
                    wallet_exposure_limit_short=wallet_exposure_limit_short,  # This is passed but not used directly for buying
                    side="buy",
                    levels=levels,
                    enforce_full_grid=enforce_full_grid,
                    user_defined_leverage_long=user_defined_leverage_long,  # Assuming there's a variable holding this value
                    user_defined_leverage_short=None  # Not used for buying, so set to None
                ) if long_mode else 0

                total_amount_short = self.calculate_total_amount_notional_ls(
                    symbol=symbol,
                    total_equity=total_equity,
                    best_ask_price=best_ask_price,
                    best_bid_price=best_bid_price,
                    wallet_exposure_limit_long=wallet_exposure_limit_long,  # This is passed but not used directly for selling
                    wallet_exposure_limit_short=wallet_exposure_limit_short,
                    side="sell",
                    levels=levels,
                    enforce_full_grid=enforce_full_grid,
                    user_defined_leverage_long=None,  # Not used for selling, so set to None
                    user_defined_leverage_short=user_defined_leverage_short  # Assuming there's a variable holding this value
                ) if short_mode else 0

                logging.info(f"[{symbol}] Total amount long: {total_amount_long}, Total amount short: {total_amount_short}")

                amounts_long = self.calculate_order_amounts_notional(symbol, total_amount_long, levels, strength, qty_precision, enforce_full_grid)
                amounts_short = self.calculate_order_amounts_notional(symbol, total_amount_short, levels, strength, qty_precision, enforce_full_grid)
                logging.info(f"[{symbol}] Long order amounts: {amounts_long}")
                logging.info(f"[{symbol}] Short order amounts: {amounts_short}")

                trading_allowed = self.can_trade_new_symbol(open_symbols, symbols_allowed, symbol)
                logging.info(f"Checking trading for symbol {symbol}. Can trade: {trading_allowed}")
                logging.info(f"Symbol: {symbol}, In open_symbols: {symbol in open_symbols}, Trading allowed: {trading_allowed}")

                mfi_signal_long = mfirsi_signal.lower() == "long"
                mfi_signal_short = mfirsi_signal.lower() == "short"

                if self.should_reissue_orders_revised(symbol, reissue_threshold, long_pos_qty, short_pos_qty):
                    open_orders = self.retry_api_call(self.exchange.get_open_orders, symbol)
                    logging.info(f"Open orders for {symbol}: {open_orders}")

                    # Flags to check existence of buy or sell orders, excluding reduce-only orders
                    has_open_long_order = any(order['side'].lower() == 'buy' and not order['reduceOnly'] for order in open_orders)
                    has_open_short_order = any(order['side'].lower() == 'sell' and not order['reduceOnly'] for order in open_orders)

                    # Handling reissue for long orders
                    if not long_pos_qty and long_mode:  # Only enter this block if there are no long positions and long trading is enabled
                        if symbol in self.active_grids and "buy" in self.filled_levels[symbol] and has_open_long_order:
                            logging.info(f"[{symbol}] Reissuing long orders due to price movement beyond the threshold.")
                            self.clear_grid(symbol, 'buy')  # Clear existing long orders
                            logging.info(f"[{symbol}] Placing new long orders.")
                            self.issue_grid_orders(symbol, "buy", grid_levels_long, amounts_long, True, self.filled_levels[symbol]["buy"])
                        elif symbol not in self.active_grids:
                            logging.info(f"[{symbol}] No active long grid for the symbol. Skipping long grid reissue.")

                    # Handling reissue for short orders
                    if not short_pos_qty and short_mode:  # Only enter this block if there are no short positions and short trading is enabled
                        if symbol in self.active_grids and "sell" in self.filled_levels[symbol] and has_open_short_order:
                            logging.info(f"[{symbol}] Reissuing short orders due to price movement beyond the threshold.")
                            self.clear_grid(symbol, 'sell')  # Clear existing short orders
                            logging.info(f"[{symbol}] Placing new short orders.")
                            self.issue_grid_orders(symbol, "sell", grid_levels_short, amounts_short, False, self.filled_levels[symbol]["sell"])
                        elif symbol not in self.active_grids:
                            logging.info(f"[{symbol}] No active short grid for the symbol. Skipping short grid reissue.")

                if symbol in open_symbols or trading_allowed:
                    # Check if there are open positions and no active grids
                    if (long_pos_qty > 0 and not long_grid_active) or (short_pos_qty > 0 and not short_grid_active):
                        logging.info(f"[{symbol}] Open positions found without active grids. Issuing grid orders.")
                        if long_pos_qty > 0 and not long_grid_active:
                            logging.info(f"[{symbol}] Placing long grid orders for existing open position.")
                            self.clear_grid(symbol, 'buy')  # Ensure no previous orders conflict
                            self.issue_grid_orders(symbol, "buy", grid_levels_long, amounts_long, True, self.filled_levels[symbol]["buy"])
                            self.active_grids.add(symbol)  # Mark the symbol as having an active grid
                        if short_pos_qty > 0 and not short_grid_active:
                            logging.info(f"[{symbol}] Placing short grid orders for existing open position.")
                            self.clear_grid(symbol, 'sell')  # Ensure no previous orders conflict
                            self.issue_grid_orders(symbol, "sell", grid_levels_short, amounts_short, False, self.filled_levels[symbol]["sell"])
                            self.active_grids.add(symbol)  # Mark the symbol as having an active grid

                    if not long_pos_qty and not short_pos_qty and symbol in self.active_grids:
                        logging.info(f"[{symbol}] No open positions. Canceling leftover grid orders.")
                        self.clear_grid(symbol, 'buy')  # Clear any lingering buy grid orders
                        self.clear_grid(symbol, 'sell')  # Clear any lingering sell grid orders
                        self.active_grids.discard(symbol)  # Remove the symbol from active grids


                    if not self.auto_reduce_active_long.get(symbol, False) and not self.auto_reduce_active_short.get(symbol, False):
                        logging.info(f"Auto-reduce for long and short positions on {symbol} is not active")
                        if long_mode and short_mode and ((mfi_signal_long or long_pos_qty > 0) and (mfi_signal_short or short_pos_qty > 0)):
                            if should_reissue_long or should_reissue_short or (long_pos_qty > 0 and not any(order['side'].lower() == 'buy' and not order['reduceOnly'] for order in open_orders)) or (short_pos_qty > 0 and not any(order['side'].lower() == 'sell' and not order['reduceOnly'] for order in open_orders)):
                                # Cancel existing long and short grid orders if should_reissue or positions exist but no corresponding orders
                                self.cancel_grid_orders(symbol, "buy")
                                self.cancel_grid_orders(symbol, "sell")
                                self.filled_levels[symbol]["buy"].clear()
                                self.filled_levels[symbol]["sell"].clear()

                            # Place new long and short grid orders only if there are no existing orders (excluding TP orders) and no active grids
                            if not any(order['side'].lower() == 'buy' and not order['reduceOnly'] for order in open_orders) and not any(order['side'].lower() == 'sell' and not order['reduceOnly'] for order in open_orders) and not long_grid_active and not short_grid_active:
                                logging.info(f"[{symbol}] Placing new long and short grid orders.")
                                self.issue_grid_orders(symbol, "buy", grid_levels_long, amounts_long, True, self.filled_levels[symbol]["buy"])
                                self.issue_grid_orders(symbol, "sell", grid_levels_short, amounts_short, False, self.filled_levels[symbol]["sell"])
                                self.active_grids.add(symbol)  # Mark the symbol as having active grids
                        else:
                            if long_mode and (mfi_signal_long or long_pos_qty > 0):
                                if should_reissue_long or (long_pos_qty > 0 and not any(order['side'].lower() == 'buy' and not order['reduceOnly'] for order in open_orders)):
                                    # Cancel existing long grid orders if should_reissue or long position exists but no buy orders (excluding TP orders)
                                    self.cancel_grid_orders(symbol, "buy")
                                    self.filled_levels[symbol]["buy"].clear()

                                # Place new long grid orders if there are no existing buy orders (excluding TP orders) and no active long grid, or if there is a long position
                                if not any(order['side'].lower() == 'buy' and not order['reduceOnly'] for order in open_orders) and not long_grid_active:
                                    logging.info(f"[{symbol}] Placing new long grid orders.")
                                    self.issue_grid_orders(symbol, "buy", grid_levels_long, amounts_long, True, self.filled_levels[symbol]["buy"])
                                    self.active_grids.add(symbol)  # Mark the symbol as having an active grid

                            if short_mode and (mfi_signal_short or short_pos_qty > 0):
                                if should_reissue_short or (short_pos_qty > 0 and not any(order['side'].lower() == 'sell' and not order['reduceOnly'] for order in open_orders)):
                                    # Cancel existing short grid orders if should_reissue or short position exists but no sell orders (excluding TP orders)
                                    self.cancel_grid_orders(symbol, "sell")
                                    self.filled_levels[symbol]["sell"].clear()

                                # Place new short grid orders if there are no existing sell orders (excluding TP orders) and no active short grid, or if there is a short position
                                if not any(order['side'].lower() == 'sell' and not order['reduceOnly'] for order in open_orders) and not short_grid_active:
                                    logging.info(f"[{symbol}] Placing new short grid orders.")
                                    self.issue_grid_orders(symbol, "sell", grid_levels_short, amounts_short, False, self.filled_levels[symbol]["sell"])
                                    self.active_grids.add(symbol)  # Mark the symbol as having an active grid
                                    
                    else:
                        if not self.auto_reduce_active_long.get(symbol, False):
                            logging.info(f"Auto-reduce for long position on {symbol} is not active")
                            if long_mode and (mfi_signal_long or (long_pos_qty > 0 and not long_grid_active)):
                                if should_reissue_long or (long_pos_qty > 0 and not any(order['side'].lower() == 'buy' and not order['reduceOnly'] for order in open_orders)):
                                    # Cancel existing long grid orders if should_reissue or long position exists but no buy orders (excluding TP orders)
                                    self.cancel_grid_orders(symbol, "buy")
                                    self.filled_levels[symbol]["buy"].clear()

                                # Place new long grid orders if there are no existing buy orders (excluding TP orders) and no active long grid, or if there is a long position
                                if (not any(order['side'].lower() == 'buy' and not order['reduceOnly'] for order in open_orders) and not long_grid_active) or long_pos_qty > 0:
                                    if entry_during_autoreduce:
                                        logging.info(f"[{symbol}] Placing new long grid orders (entry during auto-reduce).")
                                        self.issue_grid_orders(symbol, "buy", grid_levels_long, amounts_long, True, self.filled_levels[symbol]["buy"])
                                        self.active_grids.add(symbol)  # Mark the symbol as having an active grid
                                    else:
                                        logging.info(f"[{symbol}] Skipping new long grid orders due to active auto-reduce.")
                        else:
                            logging.info(f"Auto-reduce for long position on {symbol} is active, skipping entry")

                        if not self.auto_reduce_active_short.get(symbol, False):
                            logging.info(f"Auto-reduce for short position on {symbol} is not active")
                            if short_mode and (mfi_signal_short or (short_pos_qty > 0 and not short_grid_active)):
                                if should_reissue_short or (short_pos_qty > 0 and not any(order['side'].lower() == 'sell' and not order['reduceOnly'] for order in open_orders)):
                                    # Cancel existing short grid orders if should_reissue or short position exists but no sell orders (excluding TP orders)
                                    self.cancel_grid_orders(symbol, "sell")
                                    self.filled_levels[symbol]["sell"].clear()

                                # Place new short grid orders if there are no existing sell orders (excluding TP orders) and no active short grid, or if there is a short position
                                if (not any(order['side'].lower() == 'sell' and not order['reduceOnly'] for order in open_orders) and not short_grid_active) or short_pos_qty > 0:
                                    if entry_during_autoreduce:
                                        logging.info(f"[{symbol}] Placing new short grid orders (entry during auto-reduce).")
                                        self.issue_grid_orders(symbol, "sell", grid_levels_short, amounts_short, False, self.filled_levels[symbol]["sell"])
                                        self.active_grids.add(symbol)  # Mark the symbol as having an active grid
                                    else:
                                        logging.info(f"[{symbol}] Skipping new short grid orders due to active auto-reduce.")
                        else:
                            logging.info(f"Auto-reduce for short position on {symbol} is active, skipping entry")

                    # Check if there is room for trading new symbols
                    logging.info(f"[{symbol}] Number of open symbols: {len(open_symbols)}, Symbols allowed: {symbols_allowed}")
                    if len(open_symbols) < symbols_allowed and symbol not in self.active_grids:
                        logging.info(f"[{symbol}] No active grids. Checking for new symbols to trade.")
                        # Place grid orders for the new symbol
                        if long_mode and mfi_signal_long:
                            if entry_during_autoreduce or not self.auto_reduce_active_long.get(symbol, False):
                                logging.info(f"[{symbol}] Placing new long orders.")
                                self.issue_grid_orders(symbol, "buy", grid_levels_long, amounts_long, True, self.filled_levels[symbol]["buy"])
                                self.active_grids.add(symbol)  # Mark the symbol as having an active grid
                            else:
                                logging.info(f"[{symbol}] Skipping new long orders due to active auto-reduce.")
                        if short_mode and mfi_signal_short:
                            if entry_during_autoreduce or not self.auto_reduce_active_short.get(symbol, False):
                                logging.info(f"[{symbol}] Placing new short orders.")
                                self.issue_grid_orders(symbol, "sell", grid_levels_short, amounts_short, False, self.filled_levels[symbol]["sell"])
                                self.active_grids.add(symbol)  # Mark the symbol as having an active grid
                            else:
                                logging.info(f"[{symbol}] Skipping new short orders due to active auto-reduce.")

                    short_take_profit = None
                    long_take_profit = None

                    # Calculate take profit for short and long positions using quickscalp method
                    short_take_profit = self.calculate_quickscalp_short_take_profit(short_pos_price, symbol, upnl_profit_pct)
                    long_take_profit = self.calculate_quickscalp_long_take_profit(long_pos_price, symbol, upnl_profit_pct)
                    
                    # Update TP for long position
                    if long_pos_qty > 0:
                        self.next_long_tp_update = self.update_quickscalp_tp(
                            symbol=symbol,
                            pos_qty=long_pos_qty,
                            upnl_profit_pct=upnl_profit_pct,
                            short_pos_price=short_pos_price,
                            long_pos_price=long_pos_price,
                            positionIdx=1,
                            order_side="sell",
                            last_tp_update=self.next_long_tp_update,
                            tp_order_counts=tp_order_counts
                        )

                    # Update TP for short position
                    if short_pos_qty > 0:
                        self.next_short_tp_update = self.update_quickscalp_tp(
                            symbol=symbol,
                            pos_qty=short_pos_qty,
                            upnl_profit_pct=upnl_profit_pct,
                            short_pos_price=short_pos_price,
                            long_pos_price=long_pos_price,
                            positionIdx=2,
                            order_side="buy",
                            last_tp_update=self.next_short_tp_update,
                            tp_order_counts=tp_order_counts
                        )

                else:
                    logging.info(f"[{symbol}] Trading not allowed. Skipping grid placement.")
                    time.sleep(5)
        except Exception as e:
            logging.info(f"Exception caught in grid {e}")

    def linear_grid_handle_positions_mfirsi_persistent_notional_dynamic_buffer_qs_dynamictp(self, symbol: str, open_symbols: list, total_equity: float, long_pos_price: float, short_pos_price: float, long_pos_qty: float, short_pos_qty: float, levels: int, strength: float, outer_price_distance: float, reissue_threshold: float, wallet_exposure_limit: float, wallet_exposure_limit_long: float, wallet_exposure_limit_short: float, user_defined_leverage_long: float, user_defined_leverage_short: float, long_mode: bool, short_mode: bool, min_buffer_percentage: float, max_buffer_percentage: float, symbols_allowed: int, enforce_full_grid: bool, mfirsi_signal: str, upnl_profit_pct: float, max_upnl_profit_pct: float, tp_order_counts: dict, entry_during_autoreduce: bool):
        try:
            if symbol not in self.symbol_locks:
                self.symbol_locks[symbol] = threading.Lock()

            with self.symbol_locks[symbol]:
                should_reissue_long, should_reissue_short = self.should_reissue_orders_revised(symbol, reissue_threshold, long_pos_qty, short_pos_qty)
                open_orders = self.retry_api_call(self.exchange.get_open_orders, symbol)
                #logging.info(f"Open orders: {open_orders}")

                if symbol not in self.filled_levels:
                    self.filled_levels[symbol] = {"buy": set(), "sell": set()}

                long_grid_active = symbol in self.active_grids and "buy" in self.filled_levels[symbol]
                short_grid_active = symbol in self.active_grids and "sell" in self.filled_levels[symbol]

                current_price = self.exchange.get_current_price(symbol)
                logging.info(f"[{symbol}] Current price: {current_price}")

                # Handle non-existing positions by applying default buffer percentages if positions are not active
                default_buffer = min_buffer_percentage  # Could set to a reasonable default like the min_buffer_percentage

                if long_pos_price and long_pos_price > 0:
                    long_distance_from_entry = abs(current_price - long_pos_price) / long_pos_price
                    buffer_percentage_long = min_buffer_percentage + (max_buffer_percentage - min_buffer_percentage) * long_distance_from_entry
                else:
                    buffer_percentage_long = default_buffer  # Use default buffer if no valid long position

                if short_pos_price and short_pos_price > 0:
                    short_distance_from_entry = abs(current_price - short_pos_price) / short_pos_price
                    buffer_percentage_short = min_buffer_percentage + (max_buffer_percentage - min_buffer_percentage) * short_distance_from_entry
                else:
                    buffer_percentage_short = default_buffer  # Use default buffer if no valid short position

                buffer_distance_long = current_price * buffer_percentage_long
                buffer_distance_short = current_price * buffer_percentage_short

                logging.info(f"[{symbol}] Long buffer distance: {buffer_distance_long}, Short buffer distance: {buffer_distance_short}")

                order_book = self.exchange.get_orderbook(symbol)
                best_ask_price = order_book['asks'][0][0] if 'asks' in order_book else self.last_known_ask.get(symbol, current_price)
                best_bid_price = order_book['bids'][0][0] if 'bids' in order_book else self.last_known_bid.get(symbol, current_price)
                logging.info(f"[{symbol}] Best ask price: {best_ask_price}, Best bid price: {best_bid_price}")

                outer_price_long = current_price * (1 - outer_price_distance)
                outer_price_short = current_price * (1 + outer_price_distance)
                logging.info(f"[{symbol}] Outer price long: {outer_price_long}, Outer price short: {outer_price_short}")

                price_range_long = current_price - outer_price_long
                price_range_short = outer_price_short - current_price

                factors = np.linspace(0.0, 1.0, num=levels) ** strength
                grid_levels_long = [current_price - buffer_distance_long - price_range_long * factor for factor in factors]
                grid_levels_short = [current_price + buffer_distance_short + price_range_short * factor for factor in factors]

                # Check if long and short grid levels overlap
                if grid_levels_long[-1] >= grid_levels_short[0]:
                    logging.warning(f"[{symbol}] Long and short grid levels overlap. Adjusting outer_price_distance.")
                    # Adjust outer_price_distance to prevent overlap
                    outer_price_distance = (grid_levels_short[0] - grid_levels_long[-1]) / (2 * current_price)
                    # Recalculate grid levels
                    outer_price_long = current_price * (1 - outer_price_distance)
                    outer_price_short = current_price * (1 + outer_price_distance)
                    price_range_long = current_price - outer_price_long
                    price_range_short = outer_price_short - current_price
                    grid_levels_long = [current_price - buffer_distance_long - price_range_long * factor for factor in factors]
                    grid_levels_short = [current_price + buffer_distance_short + price_range_short * factor for factor in factors]

                logging.info(f"[{symbol}] Long grid levels: {grid_levels_long}")
                logging.info(f"[{symbol}] Short grid levels: {grid_levels_short}")

                # Check if grid levels need to be updated based on dynamic buffer
                replace_long_grid, replace_short_grid = self.should_replace_grid_updated_buffer(
                    symbol,
                    long_pos_price,
                    short_pos_price,
                    long_pos_qty,
                    short_pos_qty,
                    min_buffer_percentage,
                    max_buffer_percentage
                )

                if replace_long_grid:
                    long_distance_from_entry = abs(current_price - long_pos_price) / long_pos_price
                    buffer_percentage_long = min_buffer_percentage + (max_buffer_percentage - min_buffer_percentage) * long_distance_from_entry
                    buffer_distance_long = current_price * buffer_percentage_long
                    price_range_long = current_price - outer_price_long
                    grid_levels_long = [current_price - buffer_distance_long - price_range_long * factor for factor in factors]
                    logging.info(f"[{symbol}] Recalculated long grid levels with updated buffer: {grid_levels_long}")

                if replace_short_grid:
                    short_distance_from_entry = abs(current_price - short_pos_price) / short_pos_price
                    buffer_percentage_short = min_buffer_percentage + (max_buffer_percentage - min_buffer_percentage) * short_distance_from_entry
                    buffer_distance_short = current_price * buffer_percentage_short
                    price_range_short = outer_price_short - current_price
                    grid_levels_short = [current_price + buffer_distance_short + price_range_short * factor for factor in factors]
                    logging.info(f"[{symbol}] Recalculated short grid levels with updated buffer: {grid_levels_short}")
                    
                qty_precision = self.exchange.get_symbol_precision_bybit(symbol)[1]
                min_qty = float(self.get_market_data_with_retry(symbol, max_retries=100, retry_delay=5)["min_qty"])
                logging.info(f"[{symbol}] Quantity precision: {qty_precision}, Minimum quantity: {min_qty}")

                total_amount_long = self.calculate_total_amount_notional_ls(
                    symbol=symbol,
                    total_equity=total_equity,
                    best_ask_price=best_ask_price,
                    best_bid_price=best_bid_price,
                    wallet_exposure_limit_long=wallet_exposure_limit_long,
                    wallet_exposure_limit_short=wallet_exposure_limit_short,  # This is passed but not used directly for buying
                    side="buy",
                    levels=levels,
                    enforce_full_grid=enforce_full_grid,
                    user_defined_leverage_long=user_defined_leverage_long,  # Assuming there's a variable holding this value
                    user_defined_leverage_short=None  # Not used for buying, so set to None
                ) if long_mode else 0

                total_amount_short = self.calculate_total_amount_notional_ls(
                    symbol=symbol,
                    total_equity=total_equity,
                    best_ask_price=best_ask_price,
                    best_bid_price=best_bid_price,
                    wallet_exposure_limit_long=wallet_exposure_limit_long,  # This is passed but not used directly for selling
                    wallet_exposure_limit_short=wallet_exposure_limit_short,
                    side="sell",
                    levels=levels,
                    enforce_full_grid=enforce_full_grid,
                    user_defined_leverage_long=None,  # Not used for selling, so set to None
                    user_defined_leverage_short=user_defined_leverage_short  # Assuming there's a variable holding this value
                ) if short_mode else 0

                logging.info(f"[{symbol}] Total amount long: {total_amount_long}, Total amount short: {total_amount_short}")

                amounts_long = self.calculate_order_amounts_notional(symbol, total_amount_long, levels, strength, qty_precision, enforce_full_grid)
                amounts_short = self.calculate_order_amounts_notional(symbol, total_amount_short, levels, strength, qty_precision, enforce_full_grid)
                logging.info(f"[{symbol}] Long order amounts: {amounts_long}")
                logging.info(f"[{symbol}] Short order amounts: {amounts_short}")

                trading_allowed = self.can_trade_new_symbol(open_symbols, symbols_allowed, symbol)
                logging.info(f"Checking trading for symbol {symbol}. Can trade: {trading_allowed}")
                logging.info(f"Symbol: {symbol}, In open_symbols: {symbol in open_symbols}, Trading allowed: {trading_allowed}")

                mfi_signal_long = mfirsi_signal.lower() == "long"
                mfi_signal_short = mfirsi_signal.lower() == "short"

                if self.should_reissue_orders_revised(symbol, reissue_threshold, long_pos_qty, short_pos_qty):
                    open_orders = self.retry_api_call(self.exchange.get_open_orders, symbol)
                    logging.info(f"Open orders for {symbol}: {open_orders}")

                    # Flags to check existence of buy or sell orders, excluding reduce-only orders
                    has_open_long_order = any(order['side'].lower() == 'buy' and not order['reduceOnly'] for order in open_orders)
                    has_open_short_order = any(order['side'].lower() == 'sell' and not order['reduceOnly'] for order in open_orders)

                    # Handling reissue for long orders
                    if not long_pos_qty and long_mode:  # Only enter this block if there are no long positions and long trading is enabled
                        if symbol in self.active_grids and "buy" in self.filled_levels[symbol] and has_open_long_order:
                            logging.info(f"[{symbol}] Reissuing long orders due to price movement beyond the threshold.")
                            self.clear_grid(symbol, 'buy')  # Clear existing long orders
                            logging.info(f"[{symbol}] Placing new long orders.")
                            self.issue_grid_orders(symbol, "buy", grid_levels_long, amounts_long, True, self.filled_levels[symbol]["buy"])
                        elif symbol not in self.active_grids:
                            logging.info(f"[{symbol}] No active long grid for the symbol. Skipping long grid reissue.")

                    # Handling reissue for short orders
                    if not short_pos_qty and short_mode:  # Only enter this block if there are no short positions and short trading is enabled
                        if symbol in self.active_grids and "sell" in self.filled_levels[symbol] and has_open_short_order:
                            logging.info(f"[{symbol}] Reissuing short orders due to price movement beyond the threshold.")
                            self.clear_grid(symbol, 'sell')  # Clear existing short orders
                            logging.info(f"[{symbol}] Placing new short orders.")
                            self.issue_grid_orders(symbol, "sell", grid_levels_short, amounts_short, False, self.filled_levels[symbol]["sell"])
                        elif symbol not in self.active_grids:
                            logging.info(f"[{symbol}] No active short grid for the symbol. Skipping short grid reissue.")

                if symbol in open_symbols or trading_allowed:
                    # Check if there are open positions and no active grids
                    if (long_pos_qty > 0 and not long_grid_active) or (short_pos_qty > 0 and not short_grid_active):
                        logging.info(f"[{symbol}] Open positions found without active grids. Issuing grid orders.")
                        if long_pos_qty > 0 and not long_grid_active:
                            logging.info(f"[{symbol}] Placing long grid orders for existing open position.")
                            self.clear_grid(symbol, 'buy')  # Ensure no previous orders conflict
                            self.issue_grid_orders(symbol, "buy", grid_levels_long, amounts_long, True, self.filled_levels[symbol]["buy"])
                            self.active_grids.add(symbol)  # Mark the symbol as having an active grid
                        if short_pos_qty > 0 and not short_grid_active:
                            logging.info(f"[{symbol}] Placing short grid orders for existing open position.")
                            self.clear_grid(symbol, 'sell')  # Ensure no previous orders conflict
                            self.issue_grid_orders(symbol, "sell", grid_levels_short, amounts_short, False, self.filled_levels[symbol]["sell"])
                            self.active_grids.add(symbol)  # Mark the symbol as having an active grid

                    if not long_pos_qty and not short_pos_qty and symbol in self.active_grids:
                        logging.info(f"[{symbol}] No open positions. Canceling leftover grid orders.")
                        self.clear_grid(symbol, 'buy')  # Clear any lingering buy grid orders
                        self.clear_grid(symbol, 'sell')  # Clear any lingering sell grid orders
                        self.active_grids.discard(symbol)  # Remove the symbol from active grids


                    if not self.auto_reduce_active_long.get(symbol, False) and not self.auto_reduce_active_short.get(symbol, False):
                        logging.info(f"Auto-reduce for long and short positions on {symbol} is not active")
                        if long_mode and short_mode and ((mfi_signal_long or long_pos_qty > 0) and (mfi_signal_short or short_pos_qty > 0)):
                            if should_reissue_long or should_reissue_short or (long_pos_qty > 0 and not any(order['side'].lower() == 'buy' and not order['reduceOnly'] for order in open_orders)) or (short_pos_qty > 0 and not any(order['side'].lower() == 'sell' and not order['reduceOnly'] for order in open_orders)):
                                # Cancel existing long and short grid orders if should_reissue or positions exist but no corresponding orders
                                self.cancel_grid_orders(symbol, "buy")
                                self.cancel_grid_orders(symbol, "sell")
                                self.filled_levels[symbol]["buy"].clear()
                                self.filled_levels[symbol]["sell"].clear()

                            # Place new long and short grid orders only if there are no existing orders (excluding TP orders) and no active grids
                            if not any(order['side'].lower() == 'buy' and not order['reduceOnly'] for order in open_orders) and not any(order['side'].lower() == 'sell' and not order['reduceOnly'] for order in open_orders) and not long_grid_active and not short_grid_active:
                                logging.info(f"[{symbol}] Placing new long and short grid orders.")
                                self.issue_grid_orders(symbol, "buy", grid_levels_long, amounts_long, True, self.filled_levels[symbol]["buy"])
                                self.issue_grid_orders(symbol, "sell", grid_levels_short, amounts_short, False, self.filled_levels[symbol]["sell"])
                                self.active_grids.add(symbol)  # Mark the symbol as having active grids
                        else:
                            if long_mode and (mfi_signal_long or long_pos_qty > 0):
                                if should_reissue_long or (long_pos_qty > 0 and not any(order['side'].lower() == 'buy' and not order['reduceOnly'] for order in open_orders)):
                                    # Cancel existing long grid orders if should_reissue or long position exists but no buy orders (excluding TP orders)
                                    self.cancel_grid_orders(symbol, "buy")
                                    self.filled_levels[symbol]["buy"].clear()

                                # Place new long grid orders if there are no existing buy orders (excluding TP orders) and no active long grid, or if there is a long position
                                if not any(order['side'].lower() == 'buy' and not order['reduceOnly'] for order in open_orders) and not long_grid_active:
                                    logging.info(f"[{symbol}] Placing new long grid orders.")
                                    self.issue_grid_orders(symbol, "buy", grid_levels_long, amounts_long, True, self.filled_levels[symbol]["buy"])
                                    self.active_grids.add(symbol)  # Mark the symbol as having an active grid

                            if short_mode and (mfi_signal_short or short_pos_qty > 0):
                                if should_reissue_short or (short_pos_qty > 0 and not any(order['side'].lower() == 'sell' and not order['reduceOnly'] for order in open_orders)):
                                    # Cancel existing short grid orders if should_reissue or short position exists but no sell orders (excluding TP orders)
                                    self.cancel_grid_orders(symbol, "sell")
                                    self.filled_levels[symbol]["sell"].clear()

                                # Place new short grid orders if there are no existing sell orders (excluding TP orders) and no active short grid, or if there is a short position
                                if not any(order['side'].lower() == 'sell' and not order['reduceOnly'] for order in open_orders) and not short_grid_active:
                                    logging.info(f"[{symbol}] Placing new short grid orders.")
                                    self.issue_grid_orders(symbol, "sell", grid_levels_short, amounts_short, False, self.filled_levels[symbol]["sell"])
                                    self.active_grids.add(symbol)  # Mark the symbol as having an active grid
                                    
                    else:
                        if not self.auto_reduce_active_long.get(symbol, False):
                            logging.info(f"Auto-reduce for long position on {symbol} is not active")
                            if long_mode and (mfi_signal_long or (long_pos_qty > 0 and not long_grid_active)):
                                if should_reissue_long or (long_pos_qty > 0 and not any(order['side'].lower() == 'buy' and not order['reduceOnly'] for order in open_orders)):
                                    # Cancel existing long grid orders if should_reissue or long position exists but no buy orders (excluding TP orders)
                                    self.cancel_grid_orders(symbol, "buy")
                                    self.filled_levels[symbol]["buy"].clear()

                                # Place new long grid orders if there are no existing buy orders (excluding TP orders) and no active long grid, or if there is a long position
                                if (not any(order['side'].lower() == 'buy' and not order['reduceOnly'] for order in open_orders) and not long_grid_active) or long_pos_qty > 0:
                                    if entry_during_autoreduce:
                                        logging.info(f"[{symbol}] Placing new long grid orders (entry during auto-reduce).")
                                        self.issue_grid_orders(symbol, "buy", grid_levels_long, amounts_long, True, self.filled_levels[symbol]["buy"])
                                        self.active_grids.add(symbol)  # Mark the symbol as having an active grid
                                    else:
                                        logging.info(f"[{symbol}] Skipping new long grid orders due to active auto-reduce.")
                        else:
                            logging.info(f"Auto-reduce for long position on {symbol} is active, skipping entry")

                        if not self.auto_reduce_active_short.get(symbol, False):
                            logging.info(f"Auto-reduce for short position on {symbol} is not active")
                            if short_mode and (mfi_signal_short or (short_pos_qty > 0 and not short_grid_active)):
                                if should_reissue_short or (short_pos_qty > 0 and not any(order['side'].lower() == 'sell' and not order['reduceOnly'] for order in open_orders)):
                                    # Cancel existing short grid orders if should_reissue or short position exists but no sell orders (excluding TP orders)
                                    self.cancel_grid_orders(symbol, "sell")
                                    self.filled_levels[symbol]["sell"].clear()

                                # Place new short grid orders if there are no existing sell orders (excluding TP orders) and no active short grid, or if there is a short position
                                if (not any(order['side'].lower() == 'sell' and not order['reduceOnly'] for order in open_orders) and not short_grid_active) or short_pos_qty > 0:
                                    if entry_during_autoreduce:
                                        logging.info(f"[{symbol}] Placing new short grid orders (entry during auto-reduce).")
                                        self.issue_grid_orders(symbol, "sell", grid_levels_short, amounts_short, False, self.filled_levels[symbol]["sell"])
                                        self.active_grids.add(symbol)  # Mark the symbol as having an active grid
                                    else:
                                        logging.info(f"[{symbol}] Skipping new short grid orders due to active auto-reduce.")
                        else:
                            logging.info(f"Auto-reduce for short position on {symbol} is active, skipping entry")

                    # Check if there is room for trading new symbols
                    logging.info(f"[{symbol}] Number of open symbols: {len(open_symbols)}, Symbols allowed: {symbols_allowed}")
                    if len(open_symbols) < symbols_allowed and symbol not in self.active_grids:
                        logging.info(f"[{symbol}] No active grids. Checking for new symbols to trade.")
                        # Place grid orders for the new symbol
                        if long_mode and mfi_signal_long:
                            if entry_during_autoreduce or not self.auto_reduce_active_long.get(symbol, False):
                                logging.info(f"[{symbol}] Placing new long orders.")
                                self.issue_grid_orders(symbol, "buy", grid_levels_long, amounts_long, True, self.filled_levels[symbol]["buy"])
                                self.active_grids.add(symbol)  # Mark the symbol as having an active grid
                            else:
                                logging.info(f"[{symbol}] Skipping new long orders due to active auto-reduce.")
                        if short_mode and mfi_signal_short:
                            if entry_during_autoreduce or not self.auto_reduce_active_short.get(symbol, False):
                                logging.info(f"[{symbol}] Placing new short orders.")
                                self.issue_grid_orders(symbol, "sell", grid_levels_short, amounts_short, False, self.filled_levels[symbol]["sell"])
                                self.active_grids.add(symbol)  # Mark the symbol as having an active grid
                            else:
                                logging.info(f"[{symbol}] Skipping new short orders due to active auto-reduce.")

                    short_take_profit = None
                    long_take_profit = None

                    # Calculate take profit for short and long positions using quickscalp method
                    short_take_profit = self.calculate_quickscalp_short_take_profit_dynamic_distance(short_pos_price, symbol, min_upnl_profit_pct=upnl_profit_pct, max_upnl_profit_pct=max_upnl_profit_pct)
                    long_take_profit = self.calculate_quickscalp_long_take_profit_dynamic_distance(long_pos_price, symbol, min_upnl_profit_pct=upnl_profit_pct, max_upnl_profit_pct=max_upnl_profit_pct)

                    # Update TP for long position
                    if long_pos_qty > 0:
                        self.next_long_tp_update = self.update_quickscalp_tp_dynamic(
                            symbol=symbol,
                            pos_qty=long_pos_qty,
                            upnl_profit_pct=upnl_profit_pct,  # Minimum desired profit percentage
                            max_upnl_profit_pct=max_upnl_profit_pct,  # Maximum desired profit percentage for scaling
                            short_pos_price=None,  # Not relevant for long TP settings
                            long_pos_price=long_pos_price,
                            positionIdx=1,
                            order_side="sell",
                            last_tp_update=self.next_long_tp_update,
                            tp_order_counts=tp_order_counts
                        )

                    # Update TP for short position
                    if short_pos_qty > 0:
                        self.next_short_tp_update = self.update_quickscalp_tp_dynamic(
                            symbol=symbol,
                            pos_qty=short_pos_qty,
                            upnl_profit_pct=upnl_profit_pct,  # Minimum desired profit percentage
                            max_upnl_profit_pct=max_upnl_profit_pct,  # Maximum desired profit percentage for scaling
                            short_pos_price=short_pos_price,
                            long_pos_price=None,  # Not relevant for short TP settings
                            positionIdx=2,
                            order_side="buy",
                            last_tp_update=self.next_short_tp_update,
                            tp_order_counts=tp_order_counts
                        )

                else:
                    logging.info(f"[{symbol}] Trading not allowed. Skipping grid placement.")
                    time.sleep(5)
        except Exception as e:
            logging.info(f"Exception caught in grid {e}")

    def linear_grid_handle_positions_mfirsi_persistent_notional_dynamic_buffer(self, symbol: str, open_symbols: list, total_equity: float, long_pos_price: float, short_pos_price: float, long_pos_qty: float, short_pos_qty: float, levels: int, strength: float, outer_price_distance: float, reissue_threshold: float, wallet_exposure_limit: float, user_defined_leverage_long: float, user_defined_leverage_short: float, long_mode: bool, short_mode: bool, min_buffer_percentage: float, max_buffer_percentage: float, symbols_allowed: int, enforce_full_grid: bool, mfirsi_signal: str, upnl_profit_pct: float, tp_order_counts: dict, entry_during_autoreduce: bool):
        try:
            if symbol not in self.symbol_locks:
                self.symbol_locks[symbol] = threading.Lock()

            with self.symbol_locks[symbol]:
                should_reissue = self.should_reissue_orders(symbol, reissue_threshold)
                open_orders = self.retry_api_call(self.exchange.get_open_orders, symbol)
                #logging.info(f"Open orders: {open_orders}")

                if symbol not in self.filled_levels:
                    self.filled_levels[symbol] = {"buy": set(), "sell": set()}

                long_grid_active = symbol in self.active_grids and "buy" in self.filled_levels[symbol]
                short_grid_active = symbol in self.active_grids and "sell" in self.filled_levels[symbol]

                current_price = self.exchange.get_current_price(symbol)
                logging.info(f"[{symbol}] Current price: {current_price}")

                # Handle non-existing positions by applying default buffer percentages if positions are not active
                default_buffer = min_buffer_percentage  # Could set to a reasonable default like the min_buffer_percentage

                if long_pos_price and long_pos_price > 0:
                    long_distance_from_entry = abs(current_price - long_pos_price) / long_pos_price
                    buffer_percentage_long = min_buffer_percentage + (max_buffer_percentage - min_buffer_percentage) * long_distance_from_entry
                else:
                    buffer_percentage_long = default_buffer  # Use default buffer if no valid long position

                if short_pos_price and short_pos_price > 0:
                    short_distance_from_entry = abs(current_price - short_pos_price) / short_pos_price
                    buffer_percentage_short = min_buffer_percentage + (max_buffer_percentage - min_buffer_percentage) * short_distance_from_entry
                else:
                    buffer_percentage_short = default_buffer  # Use default buffer if no valid short position

                buffer_distance_long = current_price * buffer_percentage_long
                buffer_distance_short = current_price * buffer_percentage_short

                logging.info(f"[{symbol}] Long buffer distance: {buffer_distance_long}, Short buffer distance: {buffer_distance_short}")

                order_book = self.exchange.get_orderbook(symbol)
                best_ask_price = order_book['asks'][0][0] if 'asks' in order_book else self.last_known_ask.get(symbol, current_price)
                best_bid_price = order_book['bids'][0][0] if 'bids' in order_book else self.last_known_bid.get(symbol, current_price)
                logging.info(f"[{symbol}] Best ask price: {best_ask_price}, Best bid price: {best_bid_price}")

                outer_price_long = current_price * (1 - outer_price_distance)
                outer_price_short = current_price * (1 + outer_price_distance)
                logging.info(f"[{symbol}] Outer price long: {outer_price_long}, Outer price short: {outer_price_short}")

                price_range_long = current_price - outer_price_long
                price_range_short = outer_price_short - current_price

                factors = np.linspace(0.0, 1.0, num=levels) ** strength
                grid_levels_long = [current_price - buffer_distance_long - price_range_long * factor for factor in factors]
                grid_levels_short = [current_price + buffer_distance_short + price_range_short * factor for factor in factors]

                # Check if long and short grid levels overlap
                if grid_levels_long[-1] >= grid_levels_short[0]:
                    logging.warning(f"[{symbol}] Long and short grid levels overlap. Adjusting outer_price_distance.")
                    # Adjust outer_price_distance to prevent overlap
                    outer_price_distance = (grid_levels_short[0] - grid_levels_long[-1]) / (2 * current_price)
                    # Recalculate grid levels
                    outer_price_long = current_price * (1 - outer_price_distance)
                    outer_price_short = current_price * (1 + outer_price_distance)
                    price_range_long = current_price - outer_price_long
                    price_range_short = outer_price_short - current_price
                    grid_levels_long = [current_price - buffer_distance_long - price_range_long * factor for factor in factors]
                    grid_levels_short = [current_price + buffer_distance_short + price_range_short * factor for factor in factors]

                logging.info(f"[{symbol}] Long grid levels: {grid_levels_long}")
                logging.info(f"[{symbol}] Short grid levels: {grid_levels_short}")

                qty_precision = self.exchange.get_symbol_precision_bybit(symbol)[1]
                min_qty = float(self.get_market_data_with_retry(symbol, max_retries=100, retry_delay=5)["min_qty"])
                logging.info(f"[{symbol}] Quantity precision: {qty_precision}, Minimum quantity: {min_qty}")

                total_amount_long = self.calculate_total_amount_notional(symbol, total_equity, best_ask_price, best_bid_price, wallet_exposure_limit, user_defined_leverage_long, "buy", levels, enforce_full_grid) if long_mode else 0
                total_amount_short = self.calculate_total_amount_notional(symbol, total_equity, best_ask_price, best_bid_price, wallet_exposure_limit, user_defined_leverage_short, "sell", levels, enforce_full_grid) if short_mode else 0
                logging.info(f"[{symbol}] Total amount long: {total_amount_long}, Total amount short: {total_amount_short}")

                amounts_long = self.calculate_order_amounts_notional(symbol, total_amount_long, levels, strength, qty_precision, enforce_full_grid)
                amounts_short = self.calculate_order_amounts_notional(symbol, total_amount_short, levels, strength, qty_precision, enforce_full_grid)
                logging.info(f"[{symbol}] Long order amounts: {amounts_long}")
                logging.info(f"[{symbol}] Short order amounts: {amounts_short}")

                trading_allowed = self.can_trade_new_symbol(open_symbols, symbols_allowed, symbol)
                logging.info(f"Checking trading for symbol {symbol}. Can trade: {trading_allowed}")
                logging.info(f"Symbol: {symbol}, In open_symbols: {symbol in open_symbols}, Trading allowed: {trading_allowed}")

                mfi_signal_long = mfirsi_signal.lower() == "long"
                mfi_signal_short = mfirsi_signal.lower() == "short"

                if self.should_reissue_orders(symbol, reissue_threshold):
                    open_orders = self.retry_api_call(self.exchange.get_open_orders, symbol)
                    logging.info(f"Open orders for {symbol}: {open_orders}")

                    # Flags to check existence of buy or sell orders, excluding reduce-only orders
                    has_open_long_order = any(order['side'].lower() == 'buy' and not order['reduceOnly'] for order in open_orders)
                    has_open_short_order = any(order['side'].lower() == 'sell' and not order['reduceOnly'] for order in open_orders)

                    if not long_pos_qty and long_mode:  # Only enter this block if there are no long positions and long trading is enabled
                        if symbol in self.active_grids and "buy" in self.filled_levels[symbol] and has_open_long_order:
                            logging.info(f"[{symbol}] Reissuing long orders due to price movement beyond the threshold.")
                            self.cancel_grid_orders(symbol, "buy")
                            self.filled_levels[symbol]["buy"].clear()
                            logging.info(f"[{symbol}] Placing new long orders.")
                            self.issue_grid_orders(symbol, "buy", grid_levels_long, amounts_long, True, self.filled_levels[symbol]["buy"])
                        elif symbol not in self.active_grids:
                            logging.info(f"[{symbol}] No active long grid for the symbol. Skipping long grid reissue.")

                    if not short_pos_qty and short_mode:  # Only enter this block if there are no short positions and short trading is enabled
                        if symbol in self.active_grids and "sell" in self.filled_levels[symbol] and has_open_short_order:
                            logging.info(f"[{symbol}] Reissuing short orders due to price movement beyond the threshold.")
                            self.cancel_grid_orders(symbol, "sell")
                            self.filled_levels[symbol]["sell"].clear()
                            logging.info(f"[{symbol}] Placing new short orders.")
                            self.issue_grid_orders(symbol, "sell", grid_levels_short, amounts_short, False, self.filled_levels[symbol]["sell"])
                        elif symbol not in self.active_grids:
                            logging.info(f"[{symbol}] No active short grid for the symbol. Skipping short grid reissue.")
                            
                if symbol in open_symbols or trading_allowed:
                    if not self.auto_reduce_active_long.get(symbol, False) and not self.auto_reduce_active_short.get(symbol, False):
                        logging.info(f"Auto-reduce for long and short positions on {symbol} is not active")
                        if long_mode and short_mode and ((mfi_signal_long or long_pos_qty > 0) and (mfi_signal_short or short_pos_qty > 0)):
                            if should_reissue or (long_pos_qty > 0 and not any(order['side'].lower() == 'buy' and not order['reduceOnly'] for order in open_orders)) or (short_pos_qty > 0 and not any(order['side'].lower() == 'sell' and not order['reduceOnly'] for order in open_orders)):
                                # Cancel existing long and short grid orders if should_reissue or positions exist but no corresponding orders
                                self.cancel_grid_orders(symbol, "buy")
                                self.cancel_grid_orders(symbol, "sell")
                                self.filled_levels[symbol]["buy"].clear()
                                self.filled_levels[symbol]["sell"].clear()

                            # Place new long and short grid orders only if there are no existing orders (excluding TP orders) and no active grids
                            if not any(order['side'].lower() == 'buy' and not order['reduceOnly'] for order in open_orders) and not any(order['side'].lower() == 'sell' and not order['reduceOnly'] for order in open_orders) and not long_grid_active and not short_grid_active:
                                logging.info(f"[{symbol}] Placing new long and short grid orders.")
                                self.issue_grid_orders(symbol, "buy", grid_levels_long, amounts_long, True, self.filled_levels[symbol]["buy"])
                                self.issue_grid_orders(symbol, "sell", grid_levels_short, amounts_short, False, self.filled_levels[symbol]["sell"])
                                self.active_grids.add(symbol)  # Mark the symbol as having active grids
                        else:
                            if long_mode and (mfi_signal_long or long_pos_qty > 0):
                                if should_reissue or (long_pos_qty > 0 and not any(order['side'].lower() == 'buy' and not order['reduceOnly'] for order in open_orders)):
                                    # Cancel existing long grid orders if should_reissue or long position exists but no buy orders (excluding TP orders)
                                    self.cancel_grid_orders(symbol, "buy")
                                    self.filled_levels[symbol]["buy"].clear()

                                # Place new long grid orders if there are no existing buy orders (excluding TP orders) and no active long grid, or if there is a long position
                                if not any(order['side'].lower() == 'buy' and not order['reduceOnly'] for order in open_orders) and not long_grid_active:
                                    logging.info(f"[{symbol}] Placing new long grid orders.")
                                    self.issue_grid_orders(symbol, "buy", grid_levels_long, amounts_long, True, self.filled_levels[symbol]["buy"])
                                    self.active_grids.add(symbol)  # Mark the symbol as having an active grid

                            if short_mode and (mfi_signal_short or short_pos_qty > 0):
                                if should_reissue or (short_pos_qty > 0 and not any(order['side'].lower() == 'sell' and not order['reduceOnly'] for order in open_orders)):
                                    # Cancel existing short grid orders if should_reissue or short position exists but no sell orders (excluding TP orders)
                                    self.cancel_grid_orders(symbol, "sell")
                                    self.filled_levels[symbol]["sell"].clear()

                                # Place new short grid orders if there are no existing sell orders (excluding TP orders) and no active short grid, or if there is a short position
                                if not any(order['side'].lower() == 'sell' and not order['reduceOnly'] for order in open_orders) and not short_grid_active:
                                    logging.info(f"[{symbol}] Placing new short grid orders.")
                                    self.issue_grid_orders(symbol, "sell", grid_levels_short, amounts_short, False, self.filled_levels[symbol]["sell"])
                                    self.active_grids.add(symbol)  # Mark the symbol as having an active grid
                    else:
                        if not self.auto_reduce_active_long.get(symbol, False):
                            logging.info(f"Auto-reduce for long position on {symbol} is not active")
                            if long_mode and (mfi_signal_long or (long_pos_qty > 0 and not long_grid_active)):
                                if should_reissue or (long_pos_qty > 0 and not any(order['side'].lower() == 'buy' and not order['reduceOnly'] for order in open_orders)):
                                    # Cancel existing long grid orders if should_reissue or long position exists but no buy orders (excluding TP orders)
                                    self.cancel_grid_orders(symbol, "buy")
                                    self.filled_levels[symbol]["buy"].clear()

                                # Place new long grid orders if there are no existing buy orders (excluding TP orders) and no active long grid, or if there is a long position
                                if (not any(order['side'].lower() == 'buy' and not order['reduceOnly'] for order in open_orders) and not long_grid_active) or long_pos_qty > 0:
                                    if entry_during_autoreduce:
                                        logging.info(f"[{symbol}] Placing new long grid orders (entry during auto-reduce).")
                                        self.issue_grid_orders(symbol, "buy", grid_levels_long, amounts_long, True, self.filled_levels[symbol]["buy"])
                                        self.active_grids.add(symbol)  # Mark the symbol as having an active grid
                                    else:
                                        logging.info(f"[{symbol}] Skipping new long grid orders due to active auto-reduce.")
                        else:
                            logging.info(f"Auto-reduce for long position on {symbol} is active, skipping entry")

                        if not self.auto_reduce_active_short.get(symbol, False):
                            logging.info(f"Auto-reduce for short position on {symbol} is not active")
                            if short_mode and (mfi_signal_short or (short_pos_qty > 0 and not short_grid_active)):
                                if should_reissue or (short_pos_qty > 0 and not any(order['side'].lower() == 'sell' and not order['reduceOnly'] for order in open_orders)):
                                    # Cancel existing short grid orders if should_reissue or short position exists but no sell orders (excluding TP orders)
                                    self.cancel_grid_orders(symbol, "sell")
                                    self.filled_levels[symbol]["sell"].clear()

                                # Place new short grid orders if there are no existing sell orders (excluding TP orders) and no active short grid, or if there is a short position
                                if (not any(order['side'].lower() == 'sell' and not order['reduceOnly'] for order in open_orders) and not short_grid_active) or short_pos_qty > 0:
                                    if entry_during_autoreduce:
                                        logging.info(f"[{symbol}] Placing new short grid orders (entry during auto-reduce).")
                                        self.issue_grid_orders(symbol, "sell", grid_levels_short, amounts_short, False, self.filled_levels[symbol]["sell"])
                                        self.active_grids.add(symbol)  # Mark the symbol as having an active grid
                                    else:
                                        logging.info(f"[{symbol}] Skipping new short grid orders due to active auto-reduce.")
                        else:
                            logging.info(f"Auto-reduce for short position on {symbol} is active, skipping entry")

                    # Check if there is room for trading new symbols
                    logging.info(f"[{symbol}] Number of open symbols: {len(open_symbols)}, Symbols allowed: {symbols_allowed}")
                    if len(open_symbols) < symbols_allowed and symbol not in self.active_grids:
                        logging.info(f"[{symbol}] No active grids. Checking for new symbols to trade.")
                        # Place grid orders for the new symbol
                        if long_mode and mfi_signal_long:
                            if entry_during_autoreduce or not self.auto_reduce_active_long.get(symbol, False):
                                logging.info(f"[{symbol}] Placing new long orders.")
                                self.issue_grid_orders(symbol, "buy", grid_levels_long, amounts_long, True, self.filled_levels[symbol]["buy"])
                                self.active_grids.add(symbol)  # Mark the symbol as having an active grid
                            else:
                                logging.info(f"[{symbol}] Skipping new long orders due to active auto-reduce.")
                        if short_mode and mfi_signal_short:
                            if entry_during_autoreduce or not self.auto_reduce_active_short.get(symbol, False):
                                logging.info(f"[{symbol}] Placing new short orders.")
                                self.issue_grid_orders(symbol, "sell", grid_levels_short, amounts_short, False, self.filled_levels[symbol]["sell"])
                                self.active_grids.add(symbol)  # Mark the symbol as having an active grid
                            else:
                                logging.info(f"[{symbol}] Skipping new short orders due to active auto-reduce.")

                    short_take_profit = None
                    long_take_profit = None

                    # Calculate take profit for short and long positions using quickscalp method
                    short_take_profit = self.calculate_quickscalp_short_take_profit(short_pos_price, symbol, upnl_profit_pct)
                    long_take_profit = self.calculate_quickscalp_long_take_profit(long_pos_price, symbol, upnl_profit_pct)
                    
                    self.place_long_tp_order(
                        symbol,
                        best_ask_price,
                        long_pos_price,
                        long_pos_qty,
                        long_take_profit,
                        open_orders
                    )

                    self.place_short_tp_order(
                        symbol,
                        best_bid_price,
                        short_pos_price,
                        short_pos_qty,
                        short_take_profit,
                        open_orders
                    )

                    # Update TP for long position
                    if long_pos_qty > 0:
                        self.next_long_tp_update = self.update_quickscalp_tp(
                            symbol=symbol,
                            pos_qty=long_pos_qty,
                            upnl_profit_pct=upnl_profit_pct,
                            short_pos_price=short_pos_price,
                            long_pos_price=long_pos_price,
                            positionIdx=1,
                            order_side="sell",
                            last_tp_update=self.next_long_tp_update,
                            tp_order_counts=tp_order_counts
                        )

                    # Update TP for short position
                    if short_pos_qty > 0:
                        self.next_short_tp_update = self.update_quickscalp_tp(
                            symbol=symbol,
                            pos_qty=short_pos_qty,
                            upnl_profit_pct=upnl_profit_pct,
                            short_pos_price=short_pos_price,
                            long_pos_price=long_pos_price,
                            positionIdx=2,
                            order_side="buy",
                            last_tp_update=self.next_short_tp_update,
                            tp_order_counts=tp_order_counts
                        )

                else:
                    logging.info(f"[{symbol}] Trading not allowed. Skipping grid placement.")
                    time.sleep(5)
        except Exception as e:
            logging.info(f"Exception caught in grid {e}")
            
            
    def linear_grid_handle_positions_mfirsi_persistent_notional(self, symbol: str, open_symbols: list, total_equity: float, long_pos_price: float, short_pos_price: float, long_pos_qty: float, short_pos_qty: float, levels: int, strength: float, outer_price_distance: float, reissue_threshold: float, wallet_exposure_limit: float, user_defined_leverage_long: float, user_defined_leverage_short: float, long_mode: bool, short_mode: bool, buffer_percentage: float, symbols_allowed: int, enforce_full_grid: bool, mfirsi_signal: str, upnl_profit_pct: float, tp_order_counts: dict, entry_during_autoreduce: bool):
        try:
            if symbol not in self.symbol_locks:
                self.symbol_locks[symbol] = threading.Lock()

            with self.symbol_locks[symbol]:
                should_reissue = self.should_reissue_orders(symbol, reissue_threshold)
                open_orders = self.retry_api_call(self.exchange.get_open_orders, symbol)

                #logging.info(f"Open orders: {open_orders}")

                # Initialize filled_levels dictionary for the current symbol if it doesn't exist
                if symbol not in self.filled_levels:
                    self.filled_levels[symbol] = {"buy": set(), "sell": set()}

                # Check if long and short grids are active separately
                long_grid_active = symbol in self.active_grids and "buy" in self.filled_levels[symbol]
                short_grid_active = symbol in self.active_grids and "sell" in self.filled_levels[symbol]

                current_price = self.exchange.get_current_price(symbol)
                logging.info(f"[{symbol}] Current price: {current_price}")

                order_book = self.exchange.get_orderbook(symbol)
                best_ask_price = order_book['asks'][0][0] if 'asks' in order_book else self.last_known_ask.get(symbol, current_price)
                best_bid_price = order_book['bids'][0][0] if 'bids' in order_book else self.last_known_bid.get(symbol, current_price)
                logging.info(f"[{symbol}] Best ask price: {best_ask_price}, Best bid price: {best_bid_price}")

                outer_price_long = current_price * (1 - outer_price_distance)
                outer_price_short = current_price * (1 + outer_price_distance)
                logging.info(f"[{symbol}] Outer price long: {outer_price_long}, Outer price short: {outer_price_short}")

                price_range_long = current_price - outer_price_long
                price_range_short = outer_price_short - current_price

                buffer_distance_long = current_price * buffer_percentage / 100
                buffer_distance_short = current_price * buffer_percentage / 100

                factors = np.linspace(0.0, 1.0, num=levels) ** strength
                grid_levels_long = [current_price - buffer_distance_long - price_range_long * factor for factor in factors]
                grid_levels_short = [current_price + buffer_distance_short + price_range_short * factor for factor in factors]

                # Check if long and short grid levels overlap
                if grid_levels_long[-1] >= grid_levels_short[0]:
                    logging.warning(f"[{symbol}] Long and short grid levels overlap. Adjusting outer_price_distance.")
                    # Adjust outer_price_distance to prevent overlap
                    outer_price_distance = (grid_levels_short[0] - grid_levels_long[-1]) / (2 * current_price)
                    # Recalculate grid levels
                    outer_price_long = current_price * (1 - outer_price_distance)
                    outer_price_short = current_price * (1 + outer_price_distance)
                    price_range_long = current_price - outer_price_long
                    price_range_short = outer_price_short - current_price
                    grid_levels_long = [current_price - buffer_distance_long - price_range_long * factor for factor in factors]
                    grid_levels_short = [current_price + buffer_distance_short + price_range_short * factor for factor in factors]

                logging.info(f"[{symbol}] Long grid levels: {grid_levels_long}")
                logging.info(f"[{symbol}] Short grid levels: {grid_levels_short}")

                qty_precision = self.exchange.get_symbol_precision_bybit(symbol)[1]
                min_qty = float(self.get_market_data_with_retry(symbol, max_retries=100, retry_delay=5)["min_qty"])
                logging.info(f"[{symbol}] Quantity precision: {qty_precision}, Minimum quantity: {min_qty}")

                total_amount_long = self.calculate_total_amount_notional(symbol, total_equity, best_ask_price, best_bid_price, wallet_exposure_limit, user_defined_leverage_long, "buy", levels, enforce_full_grid) if long_mode else 0
                total_amount_short = self.calculate_total_amount_notional(symbol, total_equity, best_ask_price, best_bid_price, wallet_exposure_limit, user_defined_leverage_short, "sell", levels, enforce_full_grid) if short_mode else 0
                logging.info(f"[{symbol}] Total amount long: {total_amount_long}, Total amount short: {total_amount_short}")

                amounts_long = self.calculate_order_amounts_notional(symbol, total_amount_long, levels, strength, qty_precision, enforce_full_grid)
                amounts_short = self.calculate_order_amounts_notional(symbol, total_amount_short, levels, strength, qty_precision, enforce_full_grid)
                logging.info(f"[{symbol}] Long order amounts: {amounts_long}")
                logging.info(f"[{symbol}] Short order amounts: {amounts_short}")

                trading_allowed = self.can_trade_new_symbol(open_symbols, symbols_allowed, symbol)
                logging.info(f"Checking trading for symbol {symbol}. Can trade: {trading_allowed}")
                logging.info(f"Symbol: {symbol}, In open_symbols: {symbol in open_symbols}, Trading allowed: {trading_allowed}")

                mfi_signal_long = mfirsi_signal.lower() == "long"
                mfi_signal_short = mfirsi_signal.lower() == "short"

                if symbol in open_symbols or trading_allowed:
                    if not self.auto_reduce_active_long.get(symbol, False) and not self.auto_reduce_active_short.get(symbol, False):
                        logging.info(f"Auto-reduce for long and short positions on {symbol} is not active")
                        if long_mode and short_mode and ((mfi_signal_long or long_pos_qty > 0) and (mfi_signal_short or short_pos_qty > 0)):
                            if should_reissue or (long_pos_qty > 0 and not any(order['side'].lower() == 'buy' and not order['reduceOnly'] for order in open_orders)) or (short_pos_qty > 0 and not any(order['side'].lower() == 'sell' and not order['reduceOnly'] for order in open_orders)):
                                # Cancel existing long and short grid orders if should_reissue or positions exist but no corresponding orders
                                self.cancel_grid_orders(symbol, "buy")
                                self.cancel_grid_orders(symbol, "sell")
                                self.filled_levels[symbol]["buy"].clear()
                                self.filled_levels[symbol]["sell"].clear()

                            # Place new long and short grid orders only if there are no existing orders (excluding TP orders) and no active grids
                            if not any(order['side'].lower() == 'buy' and not order['reduceOnly'] for order in open_orders) and not any(order['side'].lower() == 'sell' and not order['reduceOnly'] for order in open_orders) and not long_grid_active and not short_grid_active:
                                logging.info(f"[{symbol}] Placing new long and short grid orders.")
                                self.issue_grid_orders(symbol, "buy", grid_levels_long, amounts_long, True, self.filled_levels[symbol]["buy"])
                                self.issue_grid_orders(symbol, "sell", grid_levels_short, amounts_short, False, self.filled_levels[symbol]["sell"])
                                self.active_grids.add(symbol)  # Mark the symbol as having active grids
                        else:
                            if long_mode and (mfi_signal_long or long_pos_qty > 0):
                                if should_reissue or (long_pos_qty > 0 and not any(order['side'].lower() == 'buy' and not order['reduceOnly'] for order in open_orders)):
                                    # Cancel existing long grid orders if should_reissue or long position exists but no buy orders (excluding TP orders)
                                    self.cancel_grid_orders(symbol, "buy")
                                    self.filled_levels[symbol]["buy"].clear()

                                # Place new long grid orders if there are no existing buy orders (excluding TP orders) and no active long grid, or if there is a long position
                                if not any(order['side'].lower() == 'buy' and not order['reduceOnly'] for order in open_orders) and not long_grid_active:
                                    logging.info(f"[{symbol}] Placing new long grid orders.")
                                    self.issue_grid_orders(symbol, "buy", grid_levels_long, amounts_long, True, self.filled_levels[symbol]["buy"])
                                    self.active_grids.add(symbol)  # Mark the symbol as having an active grid

                            if short_mode and (mfi_signal_short or short_pos_qty > 0):
                                if should_reissue or (short_pos_qty > 0 and not any(order['side'].lower() == 'sell' and not order['reduceOnly'] for order in open_orders)):
                                    # Cancel existing short grid orders if should_reissue or short position exists but no sell orders (excluding TP orders)
                                    self.cancel_grid_orders(symbol, "sell")
                                    self.filled_levels[symbol]["sell"].clear()

                                # Place new short grid orders if there are no existing sell orders (excluding TP orders) and no active short grid, or if there is a short position
                                if not any(order['side'].lower() == 'sell' and not order['reduceOnly'] for order in open_orders) and not short_grid_active:
                                    logging.info(f"[{symbol}] Placing new short grid orders.")
                                    self.issue_grid_orders(symbol, "sell", grid_levels_short, amounts_short, False, self.filled_levels[symbol]["sell"])
                                    self.active_grids.add(symbol)  # Mark the symbol as having an active grid
                    else:
                        if not self.auto_reduce_active_long.get(symbol, False):
                            logging.info(f"Auto-reduce for long position on {symbol} is not active")
                            if long_mode and (mfi_signal_long or (long_pos_qty > 0 and not long_grid_active)):
                                if should_reissue or (long_pos_qty > 0 and not any(order['side'].lower() == 'buy' and not order['reduceOnly'] for order in open_orders)):
                                    # Cancel existing long grid orders if should_reissue or long position exists but no buy orders (excluding TP orders)
                                    self.cancel_grid_orders(symbol, "buy")
                                    self.filled_levels[symbol]["buy"].clear()

                                # Place new long grid orders if there are no existing buy orders (excluding TP orders) and no active long grid, or if there is a long position
                                if (not any(order['side'].lower() == 'buy' and not order['reduceOnly'] for order in open_orders) and not long_grid_active) or long_pos_qty > 0:
                                    if entry_during_autoreduce:
                                        logging.info(f"[{symbol}] Placing new long grid orders (entry during auto-reduce).")
                                        self.issue_grid_orders(symbol, "buy", grid_levels_long, amounts_long, True, self.filled_levels[symbol]["buy"])
                                        self.active_grids.add(symbol)  # Mark the symbol as having an active grid
                                    else:
                                        logging.info(f"[{symbol}] Skipping new long grid orders due to active auto-reduce.")
                        else:
                            logging.info(f"Auto-reduce for long position on {symbol} is active, skipping entry")

                        if not self.auto_reduce_active_short.get(symbol, False):
                            logging.info(f"Auto-reduce for short position on {symbol} is not active")
                            if short_mode and (mfi_signal_short or (short_pos_qty > 0 and not short_grid_active)):
                                if should_reissue or (short_pos_qty > 0 and not any(order['side'].lower() == 'sell' and not order['reduceOnly'] for order in open_orders)):
                                    # Cancel existing short grid orders if should_reissue or short position exists but no sell orders (excluding TP orders)
                                    self.cancel_grid_orders(symbol, "sell")
                                    self.filled_levels[symbol]["sell"].clear()

                                # Place new short grid orders if there are no existing sell orders (excluding TP orders) and no active short grid, or if there is a short position
                                if (not any(order['side'].lower() == 'sell' and not order['reduceOnly'] for order in open_orders) and not short_grid_active) or short_pos_qty > 0:
                                    if entry_during_autoreduce:
                                        logging.info(f"[{symbol}] Placing new short grid orders (entry during auto-reduce).")
                                        self.issue_grid_orders(symbol, "sell", grid_levels_short, amounts_short, False, self.filled_levels[symbol]["sell"])
                                        self.active_grids.add(symbol)  # Mark the symbol as having an active grid
                                    else:
                                        logging.info(f"[{symbol}] Skipping new short grid orders due to active auto-reduce.")
                        else:
                            logging.info(f"Auto-reduce for short position on {symbol} is active, skipping entry")

                    # Check if there is room for trading new symbols
                    logging.info(f"[{symbol}] Number of open symbols: {len(open_symbols)}, Symbols allowed: {symbols_allowed}")
                    if len(open_symbols) < symbols_allowed and symbol not in self.active_grids:
                        logging.info(f"[{symbol}] No active grids. Checking for new symbols to trade.")
                        # Place grid orders for the new symbol
                        if long_mode and mfi_signal_long:
                            if entry_during_autoreduce or not self.auto_reduce_active_long.get(symbol, False):
                                logging.info(f"[{symbol}] Placing new long orders.")
                                self.issue_grid_orders(symbol, "buy", grid_levels_long, amounts_long, True, self.filled_levels[symbol]["buy"])
                                self.active_grids.add(symbol)  # Mark the symbol as having an active grid
                            else:
                                logging.info(f"[{symbol}] Skipping new long orders due to active auto-reduce.")
                        if short_mode and mfi_signal_short:
                            if entry_during_autoreduce or not self.auto_reduce_active_short.get(symbol, False):
                                logging.info(f"[{symbol}] Placing new short orders.")
                                self.issue_grid_orders(symbol, "sell", grid_levels_short, amounts_short, False, self.filled_levels[symbol]["sell"])
                                self.active_grids.add(symbol)  # Mark the symbol as having an active grid
                            else:
                                logging.info(f"[{symbol}] Skipping new short orders due to active auto-reduce.")

                    # Update TP for long position
                    if long_pos_qty > 0:
                        self.next_long_tp_update = self.update_quickscalp_tp(
                            symbol=symbol,
                            pos_qty=long_pos_qty,
                            upnl_profit_pct=upnl_profit_pct,
                            short_pos_price=short_pos_price,
                            long_pos_price=long_pos_price,
                            positionIdx=1,
                            order_side="sell",
                            last_tp_update=self.next_long_tp_update,
                            tp_order_counts=tp_order_counts
                        )

                    # Update TP for short position
                    if short_pos_qty > 0:
                        self.next_short_tp_update = self.update_quickscalp_tp(
                            symbol=symbol,
                            pos_qty=short_pos_qty,
                            upnl_profit_pct=upnl_profit_pct,
                            short_pos_price=short_pos_price,
                            long_pos_price=long_pos_price,
                            positionIdx=2,
                            order_side="buy",
                            last_tp_update=self.next_short_tp_update,
                            tp_order_counts=tp_order_counts
                        )

                else:
                    logging.info(f"[{symbol}] Trading not allowed. Skipping grid placement.")
                    time.sleep(5)
        except Exception as e:
            logging.info(f"Exception caught in grid {e}")

    def linear_grid_handle_positions_mfirsi_persistent(self, symbol: str, open_symbols: list, total_equity: float, long_pos_price: float, short_pos_price: float, long_pos_qty: float, short_pos_qty: float, levels: int, strength: float, outer_price_distance: float, reissue_threshold: float, wallet_exposure_limit: float, user_defined_leverage_long: float, user_defined_leverage_short: float, long_mode: bool, short_mode: bool, buffer_percentage: float, symbols_allowed: int, enforce_full_grid: bool, mfirsi_signal: str, upnl_profit_pct: float, tp_order_counts: dict, entry_during_autoreduce: bool):
        try:
            if symbol not in self.symbol_locks:
                self.symbol_locks[symbol] = threading.Lock()

            with self.symbol_locks[symbol]:
                should_reissue = self.should_reissue_orders(symbol, reissue_threshold)
                open_orders = self.retry_api_call(self.exchange.get_open_orders, symbol)

                #logging.info(f"Open orders: {open_orders}")

                # Initialize filled_levels dictionary for the current symbol if it doesn't exist
                if symbol not in self.filled_levels:
                    self.filled_levels[symbol] = {"buy": set(), "sell": set()}

                # Check if long and short grids are active separately
                long_grid_active = symbol in self.active_grids and "buy" in self.filled_levels[symbol]
                short_grid_active = symbol in self.active_grids and "sell" in self.filled_levels[symbol]

                current_price = self.exchange.get_current_price(symbol)
                logging.info(f"[{symbol}] Current price: {current_price}")

                order_book = self.exchange.get_orderbook(symbol)
                best_ask_price = order_book['asks'][0][0] if 'asks' in order_book else self.last_known_ask.get(symbol, current_price)
                best_bid_price = order_book['bids'][0][0] if 'bids' in order_book else self.last_known_bid.get(symbol, current_price)
                logging.info(f"[{symbol}] Best ask price: {best_ask_price}, Best bid price: {best_bid_price}")

                outer_price_long = current_price * (1 - outer_price_distance)
                outer_price_short = current_price * (1 + outer_price_distance)
                logging.info(f"[{symbol}] Outer price long: {outer_price_long}, Outer price short: {outer_price_short}")

                price_range_long = current_price - outer_price_long
                price_range_short = outer_price_short - current_price

                buffer_distance_long = current_price * buffer_percentage / 100
                buffer_distance_short = current_price * buffer_percentage / 100

                factors = np.linspace(0.0, 1.0, num=levels) ** strength
                grid_levels_long = [current_price - buffer_distance_long - price_range_long * factor for factor in factors]
                grid_levels_short = [current_price + buffer_distance_short + price_range_short * factor for factor in factors]

                # Check if long and short grid levels overlap
                if grid_levels_long[-1] >= grid_levels_short[0]:
                    logging.warning(f"[{symbol}] Long and short grid levels overlap. Adjusting outer_price_distance.")
                    # Adjust outer_price_distance to prevent overlap
                    outer_price_distance = (grid_levels_short[0] - grid_levels_long[-1]) / (2 * current_price)
                    # Recalculate grid levels
                    outer_price_long = current_price * (1 - outer_price_distance)
                    outer_price_short = current_price * (1 + outer_price_distance)
                    price_range_long = current_price - outer_price_long
                    price_range_short = outer_price_short - current_price
                    grid_levels_long = [current_price - buffer_distance_long - price_range_long * factor for factor in factors]
                    grid_levels_short = [current_price + buffer_distance_short + price_range_short * factor for factor in factors]

                logging.info(f"[{symbol}] Long grid levels: {grid_levels_long}")
                logging.info(f"[{symbol}] Short grid levels: {grid_levels_short}")

                qty_precision = self.exchange.get_symbol_precision_bybit(symbol)[1]
                min_qty = float(self.get_market_data_with_retry(symbol, max_retries=100, retry_delay=5)["min_qty"])
                logging.info(f"[{symbol}] Quantity precision: {qty_precision}, Minimum quantity: {min_qty}")

                total_amount_long = self.calculate_total_amount(symbol, total_equity, best_ask_price, best_bid_price, wallet_exposure_limit, user_defined_leverage_long, "buy", levels, min_qty, enforce_full_grid) if long_mode else 0
                total_amount_short = self.calculate_total_amount(symbol, total_equity, best_ask_price, best_bid_price, wallet_exposure_limit, user_defined_leverage_short, "sell", levels, min_qty, enforce_full_grid) if short_mode else 0
                logging.info(f"[{symbol}] Total amount long: {total_amount_long}, Total amount short: {total_amount_short}")

                amounts_long = self.calculate_order_amounts(symbol, total_amount_long, levels, strength, qty_precision, min_qty, enforce_full_grid)
                amounts_short = self.calculate_order_amounts(symbol, total_amount_short, levels, strength, qty_precision, min_qty, enforce_full_grid)
                logging.info(f"[{symbol}] Long order amounts: {amounts_long}")
                logging.info(f"[{symbol}] Short order amounts: {amounts_short}")

                trading_allowed = self.can_trade_new_symbol(open_symbols, symbols_allowed, symbol)
                logging.info(f"Checking trading for symbol {symbol}. Can trade: {trading_allowed}")
                logging.info(f"Symbol: {symbol}, In open_symbols: {symbol in open_symbols}, Trading allowed: {trading_allowed}")

                mfi_signal_long = mfirsi_signal.lower() == "long"
                mfi_signal_short = mfirsi_signal.lower() == "short"

                if symbol in open_symbols or trading_allowed:
                    if not self.auto_reduce_active_long.get(symbol, False) and not self.auto_reduce_active_short.get(symbol, False):
                        logging.info(f"Auto-reduce for long and short positions on {symbol} is not active")
                        if long_mode and short_mode and ((mfi_signal_long or long_pos_qty > 0) and (mfi_signal_short or short_pos_qty > 0)):
                            if should_reissue or (long_pos_qty > 0 and not any(order['side'].lower() == 'buy' and not order['reduceOnly'] for order in open_orders)) or (short_pos_qty > 0 and not any(order['side'].lower() == 'sell' and not order['reduceOnly'] for order in open_orders)):
                                # Cancel existing long and short grid orders if should_reissue or positions exist but no corresponding orders
                                self.cancel_grid_orders(symbol, "buy")
                                self.cancel_grid_orders(symbol, "sell")
                                self.filled_levels[symbol]["buy"].clear()
                                self.filled_levels[symbol]["sell"].clear()

                            # Place new long and short grid orders only if there are no existing orders (excluding TP orders) and no active grids
                            if not any(order['side'].lower() == 'buy' and not order['reduceOnly'] for order in open_orders) and not any(order['side'].lower() == 'sell' and not order['reduceOnly'] for order in open_orders) and not long_grid_active and not short_grid_active:
                                logging.info(f"[{symbol}] Placing new long and short grid orders.")
                                self.issue_grid_orders(symbol, "buy", grid_levels_long, amounts_long, True, self.filled_levels[symbol]["buy"])
                                self.issue_grid_orders(symbol, "sell", grid_levels_short, amounts_short, False, self.filled_levels[symbol]["sell"])
                                self.active_grids.add(symbol)  # Mark the symbol as having active grids
                        else:
                            if long_mode:
                                if mfi_signal_long:
                                    logging.info(f"[{symbol}] MFI signal is long.")
                                    # Cancel existing long grid orders
                                    self.cancel_grid_orders(symbol, "buy")
                                    self.filled_levels[symbol]["buy"].clear()

                                    # Place new long grid orders
                                    logging.info(f"[{symbol}] Placing new long grid orders.")
                                    self.issue_grid_orders(symbol, "buy", grid_levels_long, amounts_long, True, self.filled_levels[symbol]["buy"])
                                    self.active_grids.add(symbol)  # Mark the symbol as having an active grid
                                elif long_pos_qty > 0 and not long_grid_active:
                                    if should_reissue or not any(order['side'].lower() == 'buy' and not order['reduceOnly'] for order in open_orders):
                                        # Cancel existing long grid orders if should_reissue or long position exists but no buy orders (excluding TP orders)
                                        self.cancel_grid_orders(symbol, "buy")
                                        self.filled_levels[symbol]["buy"].clear()

                                    # Place new long grid orders if there are no existing buy orders (excluding TP orders) and no active long grid, or if there is a long position
                                    if (not any(order['side'].lower() == 'buy' and not order['reduceOnly'] for order in open_orders) and not long_grid_active) or long_pos_qty > 0:
                                        logging.info(f"[{symbol}] Placing new long grid orders.")
                                        self.issue_grid_orders(symbol, "buy", grid_levels_long, amounts_long, True, self.filled_levels[symbol]["buy"])
                                        self.active_grids.add(symbol)  # Mark the symbol as having an active grid

                            if short_mode:
                                if mfi_signal_short:
                                    logging.info(f"[{symbol}] MFI signal is short.")
                                    # Cancel existing short grid orders
                                    self.cancel_grid_orders(symbol, "sell")
                                    self.filled_levels[symbol]["sell"].clear()

                                    # Place new short grid orders
                                    logging.info(f"[{symbol}] Placing new short grid orders.")
                                    self.issue_grid_orders(symbol, "sell", grid_levels_short, amounts_short, False, self.filled_levels[symbol]["sell"])
                                    self.active_grids.add(symbol)  # Mark the symbol as having an active grid
                                elif short_pos_qty > 0 and not short_grid_active:
                                    if should_reissue or not any(order['side'].lower() == 'sell' and not order['reduceOnly'] for order in open_orders):
                                        # Cancel existing short grid orders if should_reissue or short position exists but no sell orders (excluding TP orders)
                                        self.cancel_grid_orders(symbol, "sell")
                                        self.filled_levels[symbol]["sell"].clear()

                                    # Place new short grid orders if there are no existing sell orders (excluding TP orders) and no active short grid, or if there is a short position
                                    if (not any(order['side'].lower() == 'sell' and not order['reduceOnly'] for order in open_orders) and not short_grid_active) or short_pos_qty > 0:
                                        logging.info(f"[{symbol}] Placing new short grid orders.")
                                        self.issue_grid_orders(symbol, "sell", grid_levels_short, amounts_short, False, self.filled_levels[symbol]["sell"])
                                        self.active_grids.add(symbol)  # Mark the symbol as having an active grid
                    else:
                        if not self.auto_reduce_active_long.get(symbol, False):
                            logging.info(f"Auto-reduce for long position on {symbol} is not active")
                            if long_mode and (mfi_signal_long or (long_pos_qty > 0 and not long_grid_active)):
                                if should_reissue or (long_pos_qty > 0 and not any(order['side'].lower() == 'buy' and not order['reduceOnly'] for order in open_orders)):
                                    # Cancel existing long grid orders if should_reissue or long position exists but no buy orders (excluding TP orders)
                                    self.cancel_grid_orders(symbol, "buy")
                                    self.filled_levels[symbol]["buy"].clear()

                                # Place new long grid orders if there are no existing buy orders (excluding TP orders) and no active long grid, or if there is a long position
                                if (not any(order['side'].lower() == 'buy' and not order['reduceOnly'] for order in open_orders) and not long_grid_active) or long_pos_qty > 0:
                                    if entry_during_autoreduce:
                                        logging.info(f"[{symbol}] Placing new long grid orders (entry during auto-reduce).")
                                        self.issue_grid_orders(symbol, "buy", grid_levels_long, amounts_long, True, self.filled_levels[symbol]["buy"])
                                        self.active_grids.add(symbol)  # Mark the symbol as having an active grid
                                    else:
                                        logging.info(f"[{symbol}] Skipping new long grid orders due to active auto-reduce.")
                        else:
                            logging.info(f"Auto-reduce for long position on {symbol} is active, skipping entry")

                        if not self.auto_reduce_active_short.get(symbol, False):
                            logging.info(f"Auto-reduce for short position on {symbol} is not active")
                            if short_mode and (mfi_signal_short or (short_pos_qty > 0 and not short_grid_active)):
                                if should_reissue or (short_pos_qty > 0 and not any(order['side'].lower() == 'sell' and not order['reduceOnly'] for order in open_orders)):
                                    # Cancel existing short grid orders if should_reissue or short position exists but no sell orders (excluding TP orders)
                                    self.cancel_grid_orders(symbol, "sell")
                                    self.filled_levels[symbol]["sell"].clear()

                                # Place new short grid orders if there are no existing sell orders (excluding TP orders) and no active short grid, or if there is a short position
                                if (not any(order['side'].lower() == 'sell' and not order['reduceOnly'] for order in open_orders) and not short_grid_active) or short_pos_qty > 0:
                                    if entry_during_autoreduce:
                                        logging.info(f"[{symbol}] Placing new short grid orders (entry during auto-reduce).")
                                        self.issue_grid_orders(symbol, "sell", grid_levels_short, amounts_short, False, self.filled_levels[symbol]["sell"])
                                        self.active_grids.add(symbol)  # Mark the symbol as having an active grid
                                    else:
                                        logging.info(f"[{symbol}] Skipping new short grid orders due to active auto-reduce.")
                        else:
                            logging.info(f"Auto-reduce for short position on {symbol} is active, skipping entry")

                    # Check if there is room for trading new symbols
                    logging.info(f"[{symbol}] Number of open symbols: {len(open_symbols)}, Symbols allowed: {symbols_allowed}")
                    if len(open_symbols) < symbols_allowed and symbol not in self.active_grids:
                        logging.info(f"[{symbol}] No active grids. Checking for new symbols to trade.")
                        # Place grid orders for the new symbol
                        if long_mode and mfi_signal_long:
                            if entry_during_autoreduce or not self.auto_reduce_active_long.get(symbol, False):
                                logging.info(f"[{symbol}] Placing new long orders.")
                                self.issue_grid_orders(symbol, "buy", grid_levels_long, amounts_long, True, self.filled_levels[symbol]["buy"])
                                self.active_grids.add(symbol)  # Mark the symbol as having an active grid
                            else:
                                logging.info(f"[{symbol}] Skipping new long orders due to active auto-reduce.")
                        if short_mode and mfi_signal_short:
                            if entry_during_autoreduce or not self.auto_reduce_active_short.get(symbol, False):
                                logging.info(f"[{symbol}] Placing new short orders.")
                                self.issue_grid_orders(symbol, "sell", grid_levels_short, amounts_short, False, self.filled_levels[symbol]["sell"])
                                self.active_grids.add(symbol)  # Mark the symbol as having an active grid
                            else:
                                logging.info(f"[{symbol}] Skipping new short orders due to active auto-reduce.")

                    # Update TP for long position
                    if long_pos_qty > 0:
                        self.next_long_tp_update = self.update_quickscalp_tp(
                            symbol=symbol,
                            pos_qty=long_pos_qty,
                            upnl_profit_pct=upnl_profit_pct,
                            short_pos_price=short_pos_price,
                            long_pos_price=long_pos_price,
                            positionIdx=1,
                            order_side="sell",
                            last_tp_update=self.next_long_tp_update,
                            tp_order_counts=tp_order_counts
                        )

                    # Update TP for short position
                    if short_pos_qty > 0:
                        self.next_short_tp_update = self.update_quickscalp_tp(
                            symbol=symbol,
                            pos_qty=short_pos_qty,
                            upnl_profit_pct=upnl_profit_pct,
                            short_pos_price=short_pos_price,
                            long_pos_price=long_pos_price,
                            positionIdx=2,
                            order_side="buy",
                            last_tp_update=self.next_short_tp_update,
                            tp_order_counts=tp_order_counts
                        )

                else:
                    logging.info(f"[{symbol}] Trading not allowed. Skipping grid placement.")
                    time.sleep(5)
        except Exception as e:
            logging.info(f"Exception caught in grid {e}")

    def linear_grid_handle_positions_mfirsi(self, symbol: str, open_symbols: list, total_equity: float, long_pos_qty: float, short_pos_qty: float, levels: int, strength: float, outer_price_distance: float, reissue_threshold: float, wallet_exposure_limit: float, user_defined_leverage_long: float, user_defined_leverage_short: float, long_mode: bool, short_mode: bool, buffer_percentage: float, symbols_allowed: int, enforce_full_grid: bool, mfirsi_signal: str):
        try:
            if symbol not in self.symbol_locks:
                self.symbol_locks[symbol] = threading.Lock()

            with self.symbol_locks[symbol]:
                should_reissue = self.should_reissue_orders(symbol, reissue_threshold)
                open_orders = self.retry_api_call(self.exchange.get_open_orders, symbol)

                # Initialize filled_levels dictionary for the current symbol if it doesn't exist
                if symbol not in self.filled_levels:
                    self.filled_levels[symbol] = {"buy": set(), "sell": set()}

                # Check if long and short grids are active separately
                long_grid_active = symbol in self.active_grids and "buy" in self.filled_levels[symbol]
                short_grid_active = symbol in self.active_grids and "sell" in self.filled_levels[symbol]

                if (long_grid_active or short_grid_active) and not should_reissue:
                    logging.info(f"[{symbol}] Grid already active and reissue threshold not met. Skipping grid placement.")
                    return

                current_price = self.exchange.get_current_price(symbol)
                logging.info(f"[{symbol}] Current price: {current_price}")

                order_book = self.exchange.get_orderbook(symbol)
                best_ask_price = order_book['asks'][0][0] if 'asks' in order_book else self.last_known_ask.get(symbol, current_price)
                best_bid_price = order_book['bids'][0][0] if 'bids' in order_book else self.last_known_bid.get(symbol, current_price)
                logging.info(f"[{symbol}] Best ask price: {best_ask_price}, Best bid price: {best_bid_price}")

                outer_price_long = current_price * (1 - outer_price_distance)
                outer_price_short = current_price * (1 + outer_price_distance)
                logging.info(f"[{symbol}] Outer price long: {outer_price_long}, Outer price short: {outer_price_short}")

                price_range_long = current_price - outer_price_long
                price_range_short = outer_price_short - current_price

                buffer_distance_long = current_price * buffer_percentage / 100
                buffer_distance_short = current_price * buffer_percentage / 100

                factors = np.linspace(0.0, 1.0, num=levels) ** strength
                grid_levels_long = [current_price - buffer_distance_long - price_range_long * factor for factor in factors]
                grid_levels_short = [current_price + buffer_distance_short + price_range_short * factor for factor in factors]

                logging.info(f"[{symbol}] Long grid levels: {grid_levels_long}")
                logging.info(f"[{symbol}] Short grid levels: {grid_levels_short}")

                qty_precision = self.exchange.get_symbol_precision_bybit(symbol)[1]
                min_qty = float(self.get_market_data_with_retry(symbol, max_retries=100, retry_delay=5)["min_qty"])
                logging.info(f"[{symbol}] Quantity precision: {qty_precision}, Minimum quantity: {min_qty}")

                total_amount_long = self.calculate_total_amount(symbol, total_equity, best_ask_price, best_bid_price, wallet_exposure_limit, user_defined_leverage_long, "buy", levels, min_qty, enforce_full_grid) if long_mode else 0
                total_amount_short = self.calculate_total_amount(symbol, total_equity, best_ask_price, best_bid_price, wallet_exposure_limit, user_defined_leverage_short, "sell", levels, min_qty, enforce_full_grid) if short_mode else 0
                logging.info(f"[{symbol}] Total amount long: {total_amount_long}, Total amount short: {total_amount_short}")

                amounts_long = self.calculate_order_amounts(symbol, total_amount_long, levels, strength, qty_precision, min_qty, enforce_full_grid)
                amounts_short = self.calculate_order_amounts(symbol, total_amount_short, levels, strength, qty_precision, min_qty, enforce_full_grid)
                logging.info(f"[{symbol}] Long order amounts: {amounts_long}")
                logging.info(f"[{symbol}] Short order amounts: {amounts_short}")

                trading_allowed = self.can_trade_new_symbol(open_symbols, symbols_allowed, symbol)
                logging.info(f"Checking trading for symbol {symbol}. Can trade: {trading_allowed}")
                logging.info(f"Symbol: {symbol}, In open_symbols: {symbol in open_symbols}, Trading allowed: {trading_allowed}")

                mfi_signal_long = mfirsi_signal.lower() == "long"
                mfi_signal_short = mfirsi_signal.lower() == "short"

                if symbol in open_symbols or trading_allowed:
                    if long_mode and (not long_grid_active or long_pos_qty > 0) and (mfi_signal_long or long_pos_qty > 0):
                        if should_reissue or (long_pos_qty > 0 and not any(order['side'].lower() == 'buy' for order in open_orders)):
                            logging.info(f"[{symbol}] Placing new long grid orders.")
                            self.issue_grid_orders(symbol, "buy", grid_levels_long, amounts_long, True, self.filled_levels[symbol]["buy"])
                            self.active_grids.add(symbol)  # Mark the symbol as having an active grid

                    if short_mode and (not short_grid_active or short_pos_qty > 0) and (mfi_signal_short or short_pos_qty > 0):
                        if should_reissue or (short_pos_qty > 0 and not any(order['side'].lower() == 'sell' for order in open_orders)):
                            logging.info(f"[{symbol}] Placing new short grid orders.")
                            self.issue_grid_orders(symbol, "sell", grid_levels_short, amounts_short, False, self.filled_levels[symbol]["sell"])
                            self.active_grids.add(symbol)  # Mark the symbol as having an active grid

                    # Check if there is room for trading new symbols
                    logging.info(f"[{symbol}] Number of open symbols: {len(open_symbols)}, Symbols allowed: {symbols_allowed}")
                    if len(open_symbols) < symbols_allowed and symbol not in self.active_grids:
                        logging.info(f"[{symbol}] No active grids. Checking for new symbols to trade.")
                        # Place grid orders for the new symbol
                        if long_mode and mfi_signal_long:
                            logging.info(f"[{symbol}] Placing new long orders.")
                            self.issue_grid_orders(symbol, "buy", grid_levels_long, amounts_long, True, self.filled_levels[symbol]["buy"])
                            self.active_grids.add(symbol)  # Mark the symbol as having an active grid
                        if short_mode and mfi_signal_short:
                            logging.info(f"[{symbol}] Placing new short orders.")
                            self.issue_grid_orders(symbol, "sell", grid_levels_short, amounts_short, False, self.filled_levels[symbol]["sell"])
                            self.active_grids.add(symbol)  # Mark the symbol as having an active grid
                else:
                    logging.info(f"[{symbol}] Trading not allowed. Skipping grid placement.")

                time.sleep(5)
        except Exception as e:
            logging.info(f"Exception caught in grid {e}")
            
            
    def linear_grid_handle_positions(self, symbol: str, open_symbols: list, total_equity: float, long_pos_qty: float, short_pos_qty: float, levels: int, strength: float, outer_price_distance: float, reissue_threshold: float, wallet_exposure_limit: float, user_defined_leverage_long: float, user_defined_leverage_short: float, long_mode: bool, short_mode: bool, buffer_percentage: float, symbols_allowed: int, enforce_full_grid: bool):
        try:
            if symbol not in self.symbol_locks:
                self.symbol_locks[symbol] = threading.Lock()

            with self.symbol_locks[symbol]:
                should_reissue = self.should_reissue_orders(symbol, reissue_threshold)
                open_orders = self.retry_api_call(self.exchange.get_open_orders, symbol)

                # Initialize filled_levels dictionary for the current symbol if it doesn't exist
                if symbol not in self.filled_levels:
                    self.filled_levels[symbol] = {"buy": set(), "sell": set()}

                # Check if long and short grids are active separately
                long_grid_active = symbol in self.active_grids and "buy" in self.filled_levels[symbol]
                short_grid_active = symbol in self.active_grids and "sell" in self.filled_levels[symbol]

                if (long_grid_active or short_grid_active) and not should_reissue:
                    logging.info(f"[{symbol}] Grid already active and reissue threshold not met. Skipping grid placement.")
                    return

                current_price = self.exchange.get_current_price(symbol)
                logging.info(f"[{symbol}] Current price: {current_price}")

                order_book = self.exchange.get_orderbook(symbol)
                best_ask_price = order_book['asks'][0][0] if 'asks' in order_book else self.last_known_ask.get(symbol, current_price)
                best_bid_price = order_book['bids'][0][0] if 'bids' in order_book else self.last_known_bid.get(symbol, current_price)
                logging.info(f"[{symbol}] Best ask price: {best_ask_price}, Best bid price: {best_bid_price}")

                outer_price_long = current_price * (1 - outer_price_distance)
                outer_price_short = current_price * (1 + outer_price_distance)
                logging.info(f"[{symbol}] Outer price long: {outer_price_long}, Outer price short: {outer_price_short}")

                price_range_long = current_price - outer_price_long
                price_range_short = outer_price_short - current_price

                buffer_distance_long = current_price * buffer_percentage / 100
                buffer_distance_short = current_price * buffer_percentage / 100

                factors = np.linspace(0.0, 1.0, num=levels) ** strength
                grid_levels_long = [current_price - buffer_distance_long - price_range_long * factor for factor in factors]
                grid_levels_short = [current_price + buffer_distance_short + price_range_short * factor for factor in factors]

                logging.info(f"[{symbol}] Long grid levels: {grid_levels_long}")
                logging.info(f"[{symbol}] Short grid levels: {grid_levels_short}")

                qty_precision = self.exchange.get_symbol_precision_bybit(symbol)[1]
                min_qty = float(self.get_market_data_with_retry(symbol, max_retries=100, retry_delay=5)["min_qty"])
                logging.info(f"[{symbol}] Quantity precision: {qty_precision}, Minimum quantity: {min_qty}")

                total_amount_long = self.calculate_total_amount(symbol, total_equity, best_ask_price, best_bid_price, wallet_exposure_limit, user_defined_leverage_long, "buy", levels, min_qty, enforce_full_grid) if long_mode else 0
                total_amount_short = self.calculate_total_amount(symbol, total_equity, best_ask_price, best_bid_price, wallet_exposure_limit, user_defined_leverage_short, "sell", levels, min_qty, enforce_full_grid) if short_mode else 0
                logging.info(f"[{symbol}] Total amount long: {total_amount_long}, Total amount short: {total_amount_short}")

                amounts_long = self.calculate_order_amounts(symbol, total_amount_long, levels, strength, qty_precision, min_qty, enforce_full_grid)
                amounts_short = self.calculate_order_amounts(symbol, total_amount_short, levels, strength, qty_precision, min_qty, enforce_full_grid)
                logging.info(f"[{symbol}] Long order amounts: {amounts_long}")
                logging.info(f"[{symbol}] Short order amounts: {amounts_short}")

                trading_allowed = self.can_trade_new_symbol(open_symbols, symbols_allowed, symbol)
                logging.info(f"Checking trading for symbol {symbol}. Can trade: {trading_allowed}")
                logging.info(f"Symbol: {symbol}, In open_symbols: {symbol in open_symbols}, Trading allowed: {trading_allowed}")

                if symbol in open_symbols or trading_allowed:
                    if long_mode and not long_grid_active:
                        if should_reissue or (long_pos_qty > 0 and not any(order['side'].lower() == 'buy' for order in open_orders)):
                            logging.info(f"[{symbol}] Placing new long grid orders.")
                            self.issue_grid_orders(symbol, "buy", grid_levels_long, amounts_long, True, self.filled_levels[symbol]["buy"])
                            self.active_grids.add(symbol)  # Mark the symbol as having an active grid

                    if short_mode and not short_grid_active:
                        if should_reissue or (short_pos_qty > 0 and not any(order['side'].lower() == 'sell' for order in open_orders)):
                            logging.info(f"[{symbol}] Placing new short grid orders.")
                            self.issue_grid_orders(symbol, "sell", grid_levels_short, amounts_short, False, self.filled_levels[symbol]["sell"])
                            self.active_grids.add(symbol)  # Mark the symbol as having an active grid

                    # Check if there is room for trading new symbols
                    logging.info(f"[{symbol}] Number of open symbols: {len(open_symbols)}, Symbols allowed: {symbols_allowed}")
                    if len(open_symbols) < symbols_allowed and symbol not in self.active_grids:
                        logging.info(f"[{symbol}] No active grids. Checking for new symbols to trade.")
                        # Place grid orders for the new symbol
                        if long_mode:
                            logging.info(f"[{symbol}] Placing new long orders.")
                            self.issue_grid_orders(symbol, "buy", grid_levels_long, amounts_long, True, self.filled_levels[symbol]["buy"])
                            self.active_grids.add(symbol)  # Mark the symbol as having an active grid
                        if short_mode:
                            logging.info(f"[{symbol}] Placing new short orders.")
                            self.issue_grid_orders(symbol, "sell", grid_levels_short, amounts_short, False, self.filled_levels[symbol]["sell"])
                            self.active_grids.add(symbol)  # Mark the symbol as having an active grid
                else:
                    logging.info(f"[{symbol}] Trading not allowed. Skipping grid placement.")

                time.sleep(5)
        except Exception as e:
            logging.info(f"Exception caught in grid {e}")

    def should_replace_grid_updated_buffer(self, symbol: str, long_pos_price: float, short_pos_price: float, long_pos_qty: float, short_pos_qty: float, min_buffer_percentage: float, max_buffer_percentage: float) -> tuple:
        try:
            current_price = self.exchange.get_current_price(symbol)
            
            replace_long_grid = False
            replace_short_grid = False
            
            if long_pos_qty > 0:
                long_distance_from_entry = abs(current_price - long_pos_price) / long_pos_price
                buffer_percentage_long = min_buffer_percentage + (max_buffer_percentage - min_buffer_percentage) * long_distance_from_entry
                buffer_distance_long = current_price * buffer_percentage_long
                
                if abs(current_price - long_pos_price) > buffer_distance_long:
                    replace_long_grid = True
                    logging.info(f"[{symbol}] Price change exceeds updated buffer distance for long position. Replacing long grid.")
                else:
                    logging.info(f"[{symbol}] Price change does not exceed updated buffer distance for long position. No need to replace long grid.")
            
            if short_pos_qty > 0:
                short_distance_from_entry = abs(current_price - short_pos_price) / short_pos_price
                buffer_percentage_short = min_buffer_percentage + (max_buffer_percentage - min_buffer_percentage) * short_distance_from_entry
                buffer_distance_short = current_price * buffer_percentage_short
                
                if abs(current_price - short_pos_price) > buffer_distance_short:
                    replace_short_grid = True
                    logging.info(f"[{symbol}] Price change exceeds updated buffer distance for short position. Replacing short grid.")
                else:
                    logging.info(f"[{symbol}] Price change does not exceed updated buffer distance for short position. No need to replace short grid.")
            
            return replace_long_grid, replace_short_grid
        
        except Exception as e:
            logging.exception(f"Exception caught in should_replace_grid_updated_buffer: {e}")
            return False, False
        
    def should_reissue_orders_revised(self, symbol: str, reissue_threshold: float, long_pos_qty: float, short_pos_qty: float) -> tuple:
        try:
            current_price = self.exchange.get_current_price(symbol)
            last_price = self.last_price.get(symbol)
            
            if last_price is None:
                self.last_price[symbol] = current_price
                logging.info(f"[{symbol}] No last price recorded. Current price {current_price} set as last price. No reissue required.")
                return False, False
            
            price_change_percentage = abs(current_price - last_price) / last_price * 100
            logging.info(f"[{symbol}] Last recorded price: {last_price}, Current price: {current_price}, Price change: {price_change_percentage:.2f}%")
            
            reissue_long = long_pos_qty == 0 and price_change_percentage >= reissue_threshold * 100
            reissue_short = short_pos_qty == 0 and price_change_percentage >= reissue_threshold * 100
            
            if reissue_long or reissue_short:
                self.last_price[symbol] = current_price
            
            if reissue_long:
                logging.info(f"[{symbol}] Price change ({price_change_percentage:.2f}%) exceeds reissue threshold ({reissue_threshold*100:.2f}%) and no open long position. Reissuing long orders.")
            else:
                logging.info(f"[{symbol}] Price change ({price_change_percentage:.2f}%) does not exceed reissue threshold ({reissue_threshold*100:.2f}%) or there is an open long position. No reissue required for long orders.")
            
            if reissue_short:
                logging.info(f"[{symbol}] Price change ({price_change_percentage:.2f}%) exceeds reissue threshold ({reissue_threshold*100:.2f}%) and no open short position. Reissuing short orders.")
            else:
                logging.info(f"[{symbol}] Price change ({price_change_percentage:.2f}%) does not exceed reissue threshold ({reissue_threshold*100:.2f}%) or there is an open short position. No reissue required for short orders.")
            
            return reissue_long, reissue_short
        
        except Exception as e:
            logging.exception(f"Exception caught in should_reissue_orders: {e}")
            return False, False            

    def should_reissue_orders(self, symbol: str, reissue_threshold: float) -> bool:
        try:
            current_price = self.exchange.get_current_price(symbol)
            last_price = self.last_price.get(symbol)

            if last_price is None:
                self.last_price[symbol] = current_price
                logging.info(f"[{symbol}] No last price recorded. Current price {current_price} set as last price. No reissue required.")
                return False

            price_change_percentage = abs(current_price - last_price) / last_price * 100
            logging.info(f"[{symbol}] Last recorded price: {last_price}, Current price: {current_price}, Price change: {price_change_percentage:.2f}%")

            if price_change_percentage >= reissue_threshold * 100:
                self.last_price[symbol] = current_price
                logging.info(f"[{symbol}] Price change ({price_change_percentage:.2f}%) exceeds reissue threshold ({reissue_threshold*100:.2f}%). Reissuing orders.")
                return True
            else:
                logging.info(f"[{symbol}] Price change ({price_change_percentage:.2f}%) does not exceed reissue threshold ({reissue_threshold*100:.2f}%). No reissue required.")
                return False
        except Exception as e:
            logging.exception(f"Exception caught in should_reissue_orders: {e}")
            return False

    def clear_grid(self, symbol, side):
        """Clear all orders and internal states for a specific grid side."""
        if side == 'buy':
            self.cancel_grid_orders(symbol, "buy")
            self.filled_levels[symbol]["buy"].clear()
        elif side == 'sell':
            self.cancel_grid_orders(symbol, "sell")
            self.filled_levels[symbol]["sell"].clear()
        logging.info(f"Cleared {side} grid for {symbol}.")       

    def generate_order_link_id(self, symbol, side, level):
        """
        Generates a unique, short, and descriptive OrderLinkedID for Bybit orders.
        """
        timestamp = int(time.time() * 1000) % 100000  # Use last 5 digits of current timestamp for uniqueness
        level_str = f"{level:.5f}".replace('.', '')[:5]  # Convert level to string, remove '.', and use first 5 characters
        unique_id = f"{symbol[:3]}_{side[0]}_{level_str}_{timestamp}"  # Build a compact OrderLinkedID
        return unique_id[:45]  # Ensure the ID does not exceed 45 characters

    def issue_grid_orders(self, symbol: str, side: str, grid_levels: list, amounts: list, is_long: bool, filled_levels: set):
        """
        Check the status of existing grid orders and place new orders for unfilled levels.
        """
        open_orders = self.retry_api_call(self.exchange.get_open_orders, symbol)
        logging.info(f"Open orders data for {symbol}: {open_orders}")

        # Clear the filled_levels set before placing new orders
        filled_levels.clear()

        # Place new grid orders for unfilled levels
        for level, amount in zip(grid_levels, amounts):
            order_exists = any(order['price'] == level and order['side'].lower() == side.lower() for order in open_orders)
            if not order_exists:
                order_link_id = self.generate_order_link_id(symbol, side, level)
                position_idx = 1 if is_long else 2
                try:
                    order = self.exchange.create_tagged_limit_order_bybit(symbol, side, amount, level, positionIdx=position_idx, orderLinkId=order_link_id)
                    if order and 'id' in order:
                        logging.info(f"Placed {side} order at level {level} for {symbol} with amount {amount}")
                        filled_levels.add(level)  # Add the level to filled_levels
                    else:
                        logging.info(f"Failed to place {side} order at level {level} for {symbol} with amount {amount}")
                except Exception as e:
                    logging.info(f"Exception when placing {side} order at level {level} for {symbol}: {e}")
            else:
                logging.info(f"Skipping {side} order at level {level} for {symbol} as it already exists.")

        logging.info(f"[{symbol}] {side.capitalize()} grid orders issued for unfilled levels.")
        
    def cancel_grid_orders(self, symbol: str, side: str):
        try:
            open_orders = self.retry_api_call(self.exchange.get_open_orders, symbol)
            logging.info(f"Open orders data for {symbol}: {open_orders}")

            orders_canceled = 0
            for order in open_orders:
                if order['side'].lower() == side.lower():
                    self.exchange.cancel_order_by_id(order['id'], symbol)
                    orders_canceled += 1
                    logging.info(f"Canceled order for {symbol}: {order}")

            if orders_canceled > 0:
                logging.info(f"Canceled {orders_canceled} {side} grid orders for {symbol}")
            else:
                logging.info(f"No {side} grid orders found for {symbol}")

            # Remove the symbol from active_grids
            self.active_grids.discard(symbol)
            logging.info(f"Removed {symbol} from active_grids")

        except Exception as e:
            logging.info(f"Exception in cancel_grid_orders {e}")
            
        
    def calculate_total_amount(self, symbol: str, total_equity: float, best_ask_price: float, best_bid_price: float, wallet_exposure_limit: float, user_defined_leverage: float, side: str, levels: int, min_qty: float, enforce_full_grid: bool) -> float:
        logging.info(f"Calculating total amount for {symbol} with total_equity: {total_equity}, best_ask_price: {best_ask_price}, best_bid_price: {best_bid_price}, wallet_exposure_limit: {wallet_exposure_limit}, user_defined_leverage: {user_defined_leverage}, side: {side}, levels: {levels}, min_qty: {min_qty}, enforce_full_grid: {enforce_full_grid}")
        
        # Fetch market data to get the minimum trade quantity for the symbol
        market_data = self.get_market_data_with_retry(symbol, max_retries=100, retry_delay=5)
        logging.info(f"Minimum quantity for {symbol}: {min_qty}")
        
        # Calculate the minimum quantity in USD value based on the side
        if side == "buy":
            min_qty_usd_value = min_qty * best_ask_price
        elif side == "sell":
            min_qty_usd_value = min_qty * best_bid_price
        else:
            raise ValueError(f"Invalid side: {side}")
        logging.info(f"Minimum quantity USD value for {symbol}: {min_qty_usd_value}")
        
        # Calculate the maximum position value based on total equity, wallet exposure limit, and user-defined leverage
        max_position_value = total_equity * wallet_exposure_limit * user_defined_leverage
        logging.info(f"Maximum position value for {symbol}: {max_position_value}")
        
        if enforce_full_grid:
            # Calculate the total amount based on the maximum position value and number of levels
            total_amount = max(max_position_value // levels, min_qty_usd_value) * levels
        else:
            # Calculate the total amount as a multiple of the minimum quantity USD value
            total_amount = max(max_position_value // min_qty_usd_value, 1) * min_qty_usd_value
        
        logging.info(f"Calculated total amount for {symbol}: {total_amount}")
        
        return total_amount

    def calculate_order_amounts(self, symbol: str, total_amount: float, levels: int, strength: float, qty_precision: float, min_qty: float, enforce_full_grid: bool) -> List[float]:
        logging.info(f"Calculating order amounts for {symbol} with total_amount: {total_amount}, levels: {levels}, strength: {strength}, qty_precision: {qty_precision}, min_qty: {min_qty}, enforce_full_grid: {enforce_full_grid}")
        
        # Calculate the order amounts based on the strength
        amounts = []
        total_ratio = sum([(j + 1) ** strength for j in range(levels)])
        remaining_amount = total_amount
        logging.info(f"Total ratio: {total_ratio}, Remaining amount: {remaining_amount}")
        
        for i in range(levels):
            ratio = (i + 1) ** strength
            amount = total_amount * (ratio / total_ratio)
            logging.info(f"Level {i+1} - Ratio: {ratio}, Amount: {amount}")
            
            if enforce_full_grid:
                # Round the order amount to the nearest multiple of min_qty
                rounded_amount = round(amount / min_qty) * min_qty
            else:
                # Round the order amount to the nearest multiple of qty_precision or min_qty, whichever is larger
                rounded_amount = round(amount / max(qty_precision, min_qty)) * max(qty_precision, min_qty)
            
            logging.info(f"Level {i+1} - Rounded amount: {rounded_amount}")
            
            # Ensure the order amount is greater than or equal to the minimum quantity
            adjusted_amount = max(rounded_amount, min_qty * (i + 1))
            logging.info(f"Level {i+1} - Adjusted amount: {adjusted_amount}")
            
            amounts.append(adjusted_amount)
            remaining_amount -= adjusted_amount
            logging.info(f"Level {i+1} - Remaining amount: {remaining_amount}")
        
        # If enforce_full_grid is True and there is remaining amount, distribute it among the levels
        if enforce_full_grid and remaining_amount > 0:
            # Sort the amounts in ascending order
            sorted_amounts = sorted(amounts)
            
            # Iterate over the sorted amounts and add the remaining amount until it is fully distributed
            for i in range(len(sorted_amounts)):
                if remaining_amount <= 0:
                    break
                
                # Calculate the additional amount to add to the current level
                additional_amount = min(remaining_amount, min_qty)
                
                # Find the index of the current amount in the original amounts list
                index = amounts.index(sorted_amounts[i])
                
                # Update the amount in the original amounts list
                amounts[index] += additional_amount
                
                remaining_amount -= additional_amount
        
        logging.info(f"Calculated order amounts: {amounts}")
        return amounts

    def calculate_total_amount_notional_ls(self, symbol, total_equity, best_ask_price, best_bid_price, 
                                            wallet_exposure_limit_long, wallet_exposure_limit_short, 
                                            side, levels, enforce_full_grid, 
                                            user_defined_leverage_long=None, user_defined_leverage_short=None):
        logging.info(f"Calculating total amount for {symbol} with total_equity: {total_equity}, side: {side}, levels: {levels}, enforce_full_grid: {enforce_full_grid}")
        

        current_leverage = self.exchange.get_current_max_leverage_bybit(symbol)
        logging.info(f"Current leverage for {symbol} : {current_leverage}")

        # Fetch the current maximum leverage from the exchange if user-defined leverage is set to 0 or not provided
        if side == 'buy':
            leverage_used = user_defined_leverage_long if user_defined_leverage_long not in (0, None) else self.exchange.get_current_max_leverage_bybit(symbol)
        else:
            leverage_used = user_defined_leverage_short if user_defined_leverage_short not in (0, None) else self.exchange.get_current_max_leverage_bybit(symbol)
        
        logging.info(f"Using leverage for {symbol}: {leverage_used}")

        # Calculate the wallet exposure limit based on the side
        wallet_exposure_limit = wallet_exposure_limit_long if side == 'buy' else wallet_exposure_limit_short
        max_position_value = total_equity * wallet_exposure_limit * leverage_used
        logging.info(f"Maximum position value for {symbol}: {max_position_value}")

        # Calculate the total required notional amount if enforcing a full grid
        required_notional = sum(self.min_notional(i, symbol) for i in range(levels)) if enforce_full_grid else max_position_value
        total_notional_amount = min(required_notional, max_position_value)

        logging.info(f"Calculated total notional amount for {symbol}: {total_notional_amount}")
        return total_notional_amount

    def min_notional(self, level, symbol):
        base_notional_values = {"BTCUSDT": 100.5, "ETHUSDT": 20.1, "default": 6}
        base_notional = base_notional_values.get(symbol, base_notional_values["default"])
        return base_notional * (level + 1)

    def calculate_order_amounts_notional(self, symbol: str, total_amount: float, levels: int, strength: float, qty_precision: float, enforce_full_grid: bool) -> List[float]:
        logging.info(f"Calculating order amounts for {symbol} with total_amount: {total_amount}, levels: {levels}, strength: {strength}, qty_precision: {qty_precision}, enforce_full_grid: {enforce_full_grid}")
        
        current_price = self.exchange.get_current_price(symbol)
        amounts = []
        total_ratio = sum([(i + 1) ** strength for i in range(levels)])  # Total sum of ratios for normalization
        level_notional = [(i + 1) ** strength for i in range(levels)]  # Individual level ratios
        
        for i in range(levels):
            notional_amount = (level_notional[i] / total_ratio) * total_amount
            quantity = notional_amount / current_price
            min_level_notional = self.min_notional(i, symbol) / current_price  # Calculate min quantity for this level based on notional
            rounded_quantity = max(round(quantity / qty_precision) * qty_precision, min_level_notional)
            amounts.append(rounded_quantity)

        logging.info(f"Calculated order amounts for {symbol}: {amounts}")
        return amounts

    def initiate_spread_entry(self, symbol, open_orders, long_dynamic_amount, short_dynamic_amount, long_pos_qty, short_pos_qty):
        order_book = self.exchange.get_orderbook(symbol)
        best_ask_price = order_book['asks'][0][0]
        best_bid_price = order_book['bids'][0][0]
        
        long_dynamic_amount = self.m_order_amount(symbol, "long", long_dynamic_amount)
        short_dynamic_amount = self.m_order_amount(symbol, "short", short_dynamic_amount)
        
        # Calculate order book imbalance
        depth = self.ORDER_BOOK_DEPTH
        top_bids = order_book['bids'][:depth]
        total_bids = sum([bid[1] for bid in top_bids])
        top_asks = order_book['asks'][:depth]
        total_asks = sum([ask[1] for ask in top_asks])
        
        if total_bids > total_asks:
            imbalance = "buy_wall"
        elif total_asks > total_bids:
            imbalance = "sell_wall"
        else:
            imbalance = "neutral"
        
        # Entry Logic
        if imbalance == "buy_wall" and not self.entry_order_exists(open_orders, "buy") and long_pos_qty <= 0:
            self.postonly_limit_order_bybit(symbol, "buy", long_dynamic_amount, best_bid_price, positionIdx=1, reduceOnly=False)
        elif imbalance == "sell_wall" and not self.entry_order_exists(open_orders, "sell") and short_pos_qty <= 0:
            self.postonly_limit_order_bybit(symbol, "sell", short_dynamic_amount, best_ask_price, positionIdx=2, reduceOnly=False)

    def get_order_book_imbalance(self, symbol):
        order_book = self.exchange.get_orderbook(symbol)
        
        depth = self.ORDER_BOOK_DEPTH
        top_bids = order_book['bids'][:depth]
        total_bids = sum([bid[1] for bid in top_bids])
        
        top_asks = order_book['asks'][:depth]
        total_asks = sum([ask[1] for ask in top_asks])
        
        if total_bids > total_asks:
            return "buy_wall"
        elif total_asks > total_bids:
            return "sell_wall"
        else:
            return "neutral"

    def identify_walls(self, order_book, type="buy"):
        # Threshold for what constitutes a wall (this can be adjusted)
        WALL_THRESHOLD = 5.0  # for example, 5 times the average size of top orders
        
        if type == "buy":
            orders = order_book['bids']
        else:
            orders = order_book['asks']

        avg_size = sum([order[1] for order in orders[:10]]) / 10  # average size of top 10 orders
        
        walls = []
        for price, size in orders:
            if size > avg_size * WALL_THRESHOLD:
                walls.append(price)
        
        return walls
    
    def print_order_book_imbalance(self, symbol):
        imbalance = self.get_order_book_imbalance(symbol)
        print(f"Order Book Imbalance for {symbol}: {imbalance}")

    def log_order_book_walls(self, symbol, interval_in_seconds):
        """
        Log the presence of buy/sell walls every 'interval_in_seconds'.
        """
        # Initialize counters for buy and sell wall occurrences
        buy_wall_count = 0
        sell_wall_count = 0

        start_time = time.time()

        while True:
            # Fetch the current order book for the symbol
            order_book = self.exchange.get_orderbook(symbol)
            
            # Identify buy and sell walls
            buy_walls = self.identify_walls(order_book, type="buy")
            sell_walls = self.identify_walls(order_book, type="sell")

            if buy_walls:
                buy_wall_count += 1
            if sell_walls:
                sell_wall_count += 1

            elapsed_time = time.time() - start_time

            # Log the counts every 'interval_in_seconds'
            if elapsed_time >= interval_in_seconds:
                logging.info(f"Buy Walls detected in the last {interval_in_seconds/60} minutes: {buy_wall_count}")
                logging.info(f"Sell Walls detected in the last {interval_in_seconds/60} minutes: {sell_wall_count}")

                # Reset the counters and start time
                buy_wall_count = 0
                sell_wall_count = 0
                start_time = time.time()

            time.sleep(60)  # Check every minute

    def start_wall_logging(self, symbol):
        """
        Start logging buy/sell walls at different intervals.
        """
        intervals = [300, 600, 1800, 3600]  # 5 minutes, 10 minutes, 30 minutes, 1 hour in seconds

        # Start a new thread for each interval
        for interval in intervals:
            t = threading.Thread(target=self.log_order_book_walls, args=(symbol, interval))
            t.start()

    def bybit_turbocharged_entry_maker_walls(self, symbol, trend, mfi, one_minute_volume, five_minute_distance, min_vol, min_dist, take_profit_long, take_profit_short, long_dynamic_amount, short_dynamic_amount, long_pos_qty, short_pos_qty, long_pos_price, short_pos_price):
        if one_minute_volume is None or five_minute_distance is None or one_minute_volume <= min_vol or five_minute_distance <= min_dist:
            logging.warning(f"Either 'one_minute_volume' or 'five_minute_distance' does not meet the criteria for symbol {symbol}. Skipping current execution...")
            return

        order_book = self.exchange.get_orderbook(symbol)

        best_ask_price = order_book['asks'][0][0]
        best_bid_price = order_book['bids'][0][0]

        market_data = self.get_market_data_with_retry(symbol, max_retries=5, retry_delay=5)
        min_qty = float(market_data["min_qty"])

        largest_bid = max(order_book['bids'], key=lambda x: x[1])
        largest_ask = min(order_book['asks'], key=lambda x: x[1])

        spread = best_ask_price - best_bid_price

        # Adjusting the multiplier based on the size of the wall
        bid_wall_size_multiplier = 0.05 + (0.02 if largest_bid[1] > 10 * min_qty else 0)
        ask_wall_size_multiplier = 0.05 + (0.02 if largest_ask[1] > 10 * min_qty else 0)

        front_run_bid_price = round(largest_bid[0] + (spread * bid_wall_size_multiplier), 4)
        front_run_ask_price = round(largest_ask[0] - (spread * ask_wall_size_multiplier), 4)

        # Check for long position and ensure take_profit_long is not None
        if long_pos_qty > 0 and take_profit_long:
            distance_to_tp_long = take_profit_long - best_bid_price
            dynamic_long_amount = distance_to_tp_long * 5
            if trend.lower() == "long" and mfi.lower() == "long" and best_bid_price < long_pos_price:
                self.postonly_limit_order_bybit(symbol, "buy", dynamic_long_amount, front_run_bid_price, positionIdx=1, reduceOnly=False)
                logging.info(f"Turbocharged Additional Long Entry Placed at {front_run_bid_price} with {dynamic_long_amount} amount!")

        # Check for short position and ensure take_profit_short is not None
        if short_pos_qty > 0 and take_profit_short:
            distance_to_tp_short = best_ask_price - take_profit_short
            dynamic_short_amount = distance_to_tp_short * 5
            if trend.lower() == "short" and mfi.lower() == "short" and best_ask_price > short_pos_price:
                self.postonly_limit_order_bybit(symbol, "sell", dynamic_short_amount, front_run_ask_price, positionIdx=2, reduceOnly=False)
                logging.info(f"Turbocharged Additional Short Entry Placed at {front_run_ask_price} with {dynamic_short_amount} amount!")

        # Entries for when there's no position yet
        if long_pos_qty == 0:
            if trend.lower() == "long" or mfi.lower() == "long":
                self.postonly_limit_order_bybit(symbol, "buy", long_dynamic_amount, front_run_bid_price, positionIdx=1, reduceOnly=False)
                logging.info(f"Turbocharged Long Entry Placed at {front_run_bid_price} with {long_dynamic_amount} amount!")

        if short_pos_qty == 0:
            if trend.lower() == "short" or mfi.lower() == "short":
                self.postonly_limit_order_bybit(symbol, "sell", short_dynamic_amount, front_run_ask_price, positionIdx=2, reduceOnly=False)
                logging.info(f"Turbocharged Short Entry Placed at {front_run_ask_price} with {short_dynamic_amount} amount!")

    def bybit_turbocharged_entry_maker(self, open_orders, symbol, trend, mfi, one_minute_volume: float, five_minute_distance: float, min_vol, min_dist, take_profit_long, take_profit_short, long_dynamic_amount, short_dynamic_amount, long_pos_qty, short_pos_qty, long_pos_price, short_pos_price, should_long, should_add_to_long, should_short, should_add_to_short):

        if not (one_minute_volume and five_minute_distance) or one_minute_volume <= min_vol or five_minute_distance <= min_dist:
            logging.warning(f"Either 'one_minute_volume' or 'five_minute_distance' does not meet the criteria for symbol {symbol}. Skipping current execution...")
            return

        current_price = self.exchange.get_current_price(symbol)
        logging.info(f"[{symbol}] Current price: {current_price}")

        order_book = self.exchange.get_orderbook(symbol)
        best_ask_price = order_book['asks'][0][0] if 'asks' in order_book else self.last_known_ask.get(symbol, current_price)
        best_bid_price = order_book['bids'][0][0] if 'bids' in order_book else self.last_known_bid.get(symbol, current_price)
    
        spread = best_ask_price - best_bid_price
        front_run_bid_price = round(max(order_book['bids'], key=lambda x: x[1])[0] + spread * 0.05, 4)
        front_run_ask_price = round(min(order_book['asks'], key=lambda x: x[1])[0] - spread * 0.05, 4)

        min_qty = float(self.get_market_data_with_retry(symbol, max_retries=5, retry_delay=5)["min_qty"])

        long_dynamic_amount += max((take_profit_long - best_bid_price) if take_profit_long else 0, min_qty)
        short_dynamic_amount += max((best_ask_price - take_profit_short) if take_profit_short else 0, min_qty)

        if not trend or not mfi:
            logging.warning(f"Either 'trend' or 'mfi' is None for symbol {symbol}. Skipping current execution...")
            return

        if trend.lower() == "long" and mfi.lower() == "long":
            if long_pos_qty == 0 and should_long and not self.entry_order_exists(open_orders, "buy"):
                self.postonly_limit_order_bybit(symbol, "buy", long_dynamic_amount, front_run_bid_price, positionIdx=1, reduceOnly=False)
                logging.info(f"Turbocharged Long Entry Placed at {front_run_bid_price} for {symbol} with {long_dynamic_amount} amount!")
            elif should_add_to_long and long_pos_qty > 0 and long_pos_qty < self.max_long_trade_qty_per_symbol[symbol] and best_bid_price < long_pos_price and not self.entry_order_exists(open_orders, "buy"):
                self.postonly_limit_order_bybit(symbol, "buy", long_dynamic_amount, front_run_bid_price, positionIdx=1, reduceOnly=False)
                logging.info(f"Turbocharged Additional Long Entry Placed at {front_run_bid_price} for {symbol} with {long_dynamic_amount} amount!")

        elif trend.lower() == "short" and mfi.lower() == "short":
            if short_pos_qty == 0 and should_short and not self.entry_order_exists(open_orders, "sell"):
                self.postonly_limit_order_bybit(symbol, "sell", short_dynamic_amount, front_run_ask_price, positionIdx=2, reduceOnly=False)
                logging.info(f"Turbocharged Short Entry Placed at {front_run_ask_price} for {symbol} with {short_dynamic_amount} amount!")
            elif should_add_to_short and short_pos_qty > 0 and short_pos_qty < self.max_short_trade_qty_per_symbol[symbol] and best_ask_price > short_pos_price and not self.entry_order_exists(open_orders, "sell"):
                self.postonly_limit_order_bybit(symbol, "sell", short_dynamic_amount, front_run_ask_price, positionIdx=2, reduceOnly=False)
                logging.info(f"Turbocharged Additional Short Entry Placed at {front_run_ask_price} for {symbol} with {short_dynamic_amount} amount!")

    def bybit_hedge_initial_entry_maker_hma(self, open_orders: list, symbol: str, trend: str, hma_trend: str, mfi: str, one_minute_volume: float, five_minute_distance: float, min_vol: float, min_dist: float, long_dynamic_amount: float, short_dynamic_amount: float, long_pos_qty: float, short_pos_qty: float, should_long: bool, should_short: bool):

        if trend is None or mfi is None or hma_trend is None:
            logging.warning(f"Either 'trend', 'mfi', or 'hma_trend' is None for symbol {symbol}. Skipping current execution...")
            return

        if one_minute_volume is not None and five_minute_distance is not None:
            if one_minute_volume > min_vol and five_minute_distance > min_dist:

                best_ask_price = self.exchange.get_orderbook(symbol)['asks'][0][0]
                best_bid_price = self.exchange.get_orderbook(symbol)['bids'][0][0]

                # Check for long entry conditions
                if ((trend.lower() == "long" or hma_trend.lower() == "long") and mfi.lower() == "long") and should_long and long_pos_qty == 0 and long_pos_qty < self.max_long_trade_qty_per_symbol[symbol] and not self.entry_order_exists(open_orders, "buy"):
                    logging.info(f"Placing initial long entry")
                    self.place_postonly_order_bybit(symbol, "buy", long_dynamic_amount, best_bid_price, positionIdx=1, reduceOnly=False)
                    logging.info(f"Placed initial long entry")

                # Check for short entry conditions
                if ((trend.lower() == "short" or hma_trend.lower() == "short") and mfi.lower() == "short") and should_short and short_pos_qty == 0 and short_pos_qty < self.max_short_trade_qty_per_symbol[symbol] and not self.entry_order_exists(open_orders, "sell"):
                    logging.info(f"Placing initial short entry")
                    self.place_postonly_order_bybit(symbol, "sell", short_dynamic_amount, best_ask_price, positionIdx=2, reduceOnly=False)
                    logging.info(f"Placed initial short entry")
                    
    # Revised consistent maker strategy using MA Trend OR MFI as well while maintaining same original MA logic
    def bybit_hedge_entry_maker_v2(self, symbol: str, trend: str, mfi: str, one_minute_volume: float, five_minute_distance: float, min_vol: float, min_dist: float, long_dynamic_amount: float, short_dynamic_amount: float, long_pos_qty: float, short_pos_qty: float, long_pos_price: float, short_pos_price: float, should_long: bool, should_short: bool, should_add_to_long: bool, should_add_to_short: bool):

        if one_minute_volume is not None and five_minute_distance is not None:
            if one_minute_volume > min_vol and five_minute_distance > min_dist:
                open_orders = self.retry_api_call(self.exchange.get_open_orders, symbol)

                best_ask_price = self.exchange.get_orderbook(symbol)['asks'][0][0]
                best_bid_price = self.exchange.get_orderbook(symbol)['bids'][0][0]

                if (trend.lower() == "long" or mfi.lower() == "long") and should_long and long_pos_qty == 0 and not self.entry_order_exists(open_orders, "buy"):
                    logging.info(f"Placing initial long entry")
                    self.postonly_limit_order_bybit(symbol, "buy", long_dynamic_amount, best_bid_price, positionIdx=1, reduceOnly=False)
                    logging.info(f"Placed initial long entry")

                elif (trend.lower() == "long" or mfi.lower() == "long") and should_add_to_long and long_pos_qty < self.max_long_trade_qty and best_bid_price < long_pos_price and not self.entry_order_exists(open_orders, "buy"):
                    logging.info(f"Placing additional long entry")
                    self.postonly_limit_order_bybit(symbol, "buy", long_dynamic_amount, best_bid_price, positionIdx=1, reduceOnly=False)

                if (trend.lower() == "short" or mfi.lower() == "short") and should_short and short_pos_qty == 0 and not self.entry_order_exists(open_orders, "sell"):
                    logging.info(f"Placing initial short entry")
                    self.postonly_limit_order_bybit(symbol, "sell", short_dynamic_amount, best_ask_price, positionIdx=2, reduceOnly=False)
                    logging.info("Placed initial short entry")

                elif (trend.lower() == "short" or mfi.lower() == "short") and should_add_to_short and short_pos_qty < self.max_short_trade_qty and best_ask_price > short_pos_price and not self.entry_order_exists(open_orders, "sell"):
                    logging.info(f"Placing additional short entry")
                    self.postonly_limit_order_bybit(symbol, "sell", short_dynamic_amount, best_ask_price, positionIdx=2, reduceOnly=False)

    # Revised for ERI
    def bybit_hedge_entry_maker_eritrend(self, symbol: str, trend: str, eri: str, one_minute_volume: float, five_minute_distance: float, min_vol: float, min_dist: float, long_dynamic_amount: float, short_dynamic_amount: float, long_pos_qty: float, short_pos_qty: float, long_pos_price: float, short_pos_price: float, should_long: bool, should_short: bool, should_add_to_long: bool, should_add_to_short: bool):

        if one_minute_volume is not None and five_minute_distance is not None:
            if one_minute_volume > min_vol and five_minute_distance > min_dist:

                best_ask_price = self.exchange.get_orderbook(symbol)['asks'][0][0]
                best_bid_price = self.exchange.get_orderbook(symbol)['bids'][0][0]

                if (trend.lower() == "long" or eri.lower() == "short") and should_long and long_pos_qty == 0:
                    logging.info(f"Placing initial long entry")
                    self.postonly_limit_order_bybit(symbol, "buy", long_dynamic_amount, best_bid_price, positionIdx=1, reduceOnly=False)
                    logging.info(f"Placed initial long entry")
                else:
                    if (trend.lower() == "long" or eri.lower() == "short") and should_add_to_long and long_pos_qty < self.max_long_trade_qty and best_bid_price < long_pos_price:
                        logging.info(f"Placing additional long entry")
                        self.postonly_limit_order_bybit(symbol, "buy", long_dynamic_amount, best_bid_price, positionIdx=1, reduceOnly=False)

                if (trend.lower() == "short" or eri.lower() == "long") and should_short and short_pos_qty == 0:
                    logging.info(f"Placing initial short entry")
                    self.postonly_limit_order_bybit(symbol, "sell", short_dynamic_amount, best_ask_price, positionIdx=2, reduceOnly=False)
                    logging.info("Placed initial short entry")
                else:
                    if (trend.lower() == "short" or eri.lower() == "long") and should_add_to_short and short_pos_qty < self.max_short_trade_qty and best_ask_price > short_pos_price:
                        logging.info(f"Placing additional short entry")
                        self.postonly_limit_order_bybit(symbol, "sell", short_dynamic_amount, best_ask_price, positionIdx=2, reduceOnly=False)
