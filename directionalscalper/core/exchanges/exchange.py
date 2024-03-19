import os
import logging
import time
import ta as ta
import uuid
import ccxt
import pandas as pd
import json
import requests, hmac, hashlib
import urllib.parse
import threading
import traceback
from typing import Optional, Tuple, List
from ccxt.base.errors import RateLimitExceeded
from ..strategies.logger import Logger
from requests.exceptions import HTTPError
from datetime import datetime, timedelta
from ccxt.base.errors import NetworkError

logging = Logger(logger_name="Exchange", filename="Exchange.log", stream=True)

class Exchange:
    # Shared class-level cache variables
    open_positions_shared_cache = None
    last_open_positions_time_shared = None
    open_positions_semaphore = threading.Semaphore()

    def __init__(self, exchange_id, api_key, secret_key, passphrase=None, market_type='swap'):
        self.order_timestamps = None
        self.exchange_id = exchange_id
        self.api_key = api_key
        self.secret_key = secret_key
        self.passphrase = passphrase
        self.market_type = market_type  # Store the market type
        self.name = exchange_id
        self.initialise()
        self.symbols = self._get_symbols()
        self.market_precisions = {}
        self.open_positions_cache = None
        self.last_open_positions_time = None

        self.entry_order_ids = {}  # Initialize order history
        self.entry_order_ids_lock = threading.Lock()  # For thread safety
        
    def initialise(self):
        exchange_class = getattr(ccxt, self.exchange_id)
        exchange_params = {
            "apiKey": self.api_key,
            "secret": self.secret_key,
            "enableRateLimit": True,
        }
        if os.environ.get('HTTP_PROXY') and os.environ.get('HTTPS_PROXY'):
            exchange_params["proxies"] = {
                'http': os.environ.get('HTTP_PROXY'),
                'https': os.environ.get('HTTPS_PROXY'),
            }
        if self.passphrase:
            exchange_params["password"] = self.passphrase

        if self.exchange_id.lower() == 'bybit_spot':
            exchange_params['options'] = {
                'defaultType': 'spot',
                'adjustForTimeDifference': True,
            }
            self.exchange_id = 'bybit'  # Change the exchange ID to 'bybit' for CCXT
        elif self.exchange_id.lower() == 'bybit':
            exchange_params['options'] = {
                'defaultType': self.market_type,
                'adjustForTimeDifference': True,
            }
        else:
            exchange_params['options'] = {
                'defaultType': self.market_type,
                'adjustForTimeDifference': True,
            }

        if self.exchange_id.lower() == 'hyperliquid':
            exchange_params['options'] = {
                'sandboxMode': False,
                # Set Liquid-specific options here
            }

        if self.exchange_id.lower().startswith('bybit'):
            exchange_params['options']['brokerId'] = 'Nu000450'

        # Existing condition for Huobi
        if self.exchange_id.lower() == 'huobi' and self.market_type == 'swap':
            exchange_params['options']['defaultSubType'] = 'linear'

        # Initializing the exchange object
        self.exchange = exchange_class(exchange_params)
        
    def update_order_history(self, symbol, order_id, timestamp):
        with self.entry_order_ids_lock:
            # Check if the symbol is already in the order history
            if symbol not in self.entry_order_ids:
                self.entry_order_ids[symbol] = []
                logging.info(f"Creating new order history entry for symbol: {symbol}")

            # Append the new order data
            self.entry_order_ids[symbol].append({'id': order_id, 'timestamp': timestamp})
            logging.info(f"Updated order history for {symbol} with order ID {order_id} at timestamp {timestamp}")

            # Optionally, log the entire current order history for the symbol
            logging.debug(f"Current order history for {symbol}: {self.entry_order_ids[symbol]}")
            
    def set_order_timestamps(self, order_timestamps):
        self.order_timestamps = order_timestamps

    def populate_order_history(self, symbols: list, since: int = None, limit: int = 100):
        for symbol in symbols:
            try:
                logging.info(f"Fetching trades for {symbol}")
                recent_trades = self.exchange.fetch_trades(symbol, since=since, limit=limit)

                # Check if recent_trades is None or empty
                if not recent_trades:
                    logging.info(f"No trade data returned for {symbol}. It might not be a valid symbol or no recent trades.")
                    continue

                last_trade = recent_trades[-1]
                last_trade_time = datetime.fromtimestamp(last_trade['timestamp'] / 1000)  # Convert ms to seconds

                if symbol not in self.order_timestamps:
                    self.order_timestamps[symbol] = []
                self.order_timestamps[symbol].append(last_trade_time)

                logging.info(f"Updated order timestamps for {symbol} with last trade at {last_trade_time}")

            except Exception as e:
                logging.error(f"Exception occurred while processing trades for {symbol}: {e}")

    def _get_symbols(self):
        while True:
            try:
                #self.exchange.set_sandbox_mode(True)
                markets = self.exchange.load_markets()
                symbols = [market['symbol'] for market in markets.values()]
                return symbols
            except ccxt.errors.RateLimitExceeded as e:
                logging.info(f"Rate limit exceeded: {e}, retrying in 10 seconds...")
                time.sleep(10)
            except Exception as e:
                logging.info(f"An error occurred while fetching symbols: {e}, retrying in 10 seconds...")
                time.sleep(10)

    def get_ohlc_data(self, symbol, timeframe='1H', since=None, limit=None):
        """
        Fetches historical OHLC data for the given symbol and timeframe using ccxt's fetch_ohlcv method.
        
        :param str symbol: Symbol of the market to fetch OHLCV data for.
        :param str timeframe: The length of time each candle represents.
        :param int since: Timestamp in ms of the earliest candle to fetch.
        :param int limit: The maximum amount of candles to fetch.
        
        :return: List of OHLCV data.
        """
        ohlc_data = self.fetch_ohlcv(symbol, timeframe, since, limit)
        
        # Parsing the data to a more friendly format (optional)
        parsed_data = []
        for entry in ohlc_data:
            timestamp, open_price, high, low, close_price, volume = entry
            parsed_data.append({
                'timestamp': timestamp,
                'open': open_price,
                'high': high,
                'low': low,
                'close': close_price,
                'volume': volume
            })

        return parsed_data

    def calculate_max_trade_quantity(self, symbol, leverage, wallet_exposure, best_ask_price):
        # Fetch necessary data from the exchange
        market_data = self.get_market_data_bybit(symbol)
        dex_equity = self.get_balance_bybit('USDT')

        # Calculate the max trade quantity based on leverage and equity
        max_trade_qty = round(
            (float(dex_equity) * wallet_exposure / float(best_ask_price))
            / (100 / leverage),
            int(float(market_data['min_qty'])),
        )

        return max_trade_qty
    
    def debug_derivatives_positions(self, symbol):
        try:
            positions = self.exchange.fetch_derivatives_positions([symbol])
            logging.info(f"Debug positions: {positions}")
        except Exception as e:
            logging.info(f"Exception in debug derivs func: {e}")

    def debug_derivatives_markets_bybit(self):
        try:
            markets = self.exchange.fetch_derivatives_markets({'category': 'linear'})
            logging.info(f"Debug markets: {markets}")
        except Exception as e:
            logging.info(f"Exception in debug_derivatives_markets_bybit: {e}")

    def parse_trading_fee(self, fee_data):
        maker_fee = float(fee_data.get('makerFeeRate', '0'))
        taker_fee = float(fee_data.get('takerFeeRate', '0'))
        return {
            'maker_fee': maker_fee,
            'taker_fee': taker_fee
        }


    def debug_binance_market_data(self, symbol: str) -> dict:
        try:
            self.exchange.load_markets()
            symbol_data = self.exchange.market(symbol)
            print(symbol_data)
        except Exception as e:
            logging.info(f"Error occurred in debug_binance_market_data: {e}")
        
    def fetch_trades(self, symbol: str, since: int = None, limit: int = None, params={}):
        """
        Get the list of most recent trades for a particular symbol.
        :param str symbol: Unified symbol of the market to fetch trades for.
        :param int since: Timestamp in ms of the earliest trade to fetch.
        :param int limit: The maximum amount of trades to fetch.
        :param dict params: Extra parameters specific to the Bybit API endpoint.
        :returns: A list of trade structures.
        """
        try:
            return self.exchange.fetch_trades(symbol, since=since, limit=limit, params=params)
        except Exception as e:
            logging.error(f"Error fetching trades for {symbol}: {e}")
            return []

    def retry_api_call(self, function, *args, max_retries=100, delay=10, **kwargs):
        for i in range(max_retries):
            try:
                return function(*args, **kwargs)
            except Exception as e:
                logging.info(f"Error occurred during API call: {e}. Retrying in {delay} seconds...")
                time.sleep(delay)
        raise Exception(f"Failed to execute the API function after {max_retries} retries.")
    
    def get_available_balance_huobi(self, symbol):
        try:
            balance_data = self.exchange.fetch_balance()
            contract_details = balance_data.get('info', {}).get('data', [{}])[0].get('futures_contract_detail', [])
            for contract in contract_details:
                if contract['contract_code'] == symbol:
                    return float(contract['margin_available'])
            return "No contract found for symbol " + symbol
        except Exception as e:
            return f"An error occurred while fetching balance: {str(e)}"


    def debug_print_balance_huobi(self):
        try:
            balance = self.exchange.fetch_balance()
            print("Full balance data:")
            print(balance)
        except Exception as e:
            print(f"An error occurred while fetching balance: {str(e)}")


    def get_balance_huobi(self, quote, type='spot', subType='linear', marginMode='cross'):
        if self.exchange.has['fetchBalance']:
            params = {
                'type': type,
                'subType': subType,
                'marginMode': marginMode,
                'unified': False
            }
            balance = self.exchange.fetch_balance(params)
            if quote in balance:
                return balance[quote]['free']
        return None

    def get_balance_huobi_unified(self, quote, type='spot', subType='linear', marginMode='cross'):
        if self.exchange.has['fetchBalance']:
            params = {
                'type': type,
                'subType': subType,
                'marginMode': marginMode,
                'unified': True
            }
            balance = self.exchange.fetch_balance(params)
            if quote in balance:
                return balance[quote]['free']
        return None

    def cancel_order_huobi(self, id: str, symbol: Optional[str] = None, params={}):
        try:
            result = self.exchange.cancel_order(id, symbol, params)
            return result
        except Exception as e:
            # Log exception details, if any
            print(f"Failed to cancel order: {str(e)}")
            return None

    def get_contract_orders_huobi(self, symbol, status='open', type='limit', limit=20):
        if self.exchange.has['fetchOrders']:
            params = {
                'symbol': symbol,
                'status': status,  # 'open', 'closed', or 'all'
                'type': type,  # 'limit' or 'market'
                'limit': limit,  # maximum number of orders
            }
            return self.exchange.fetch_orders(params)
        return None
    
    def fetch_balance_huobi(self, params={}):
        # Call base class's fetch_balance for spot balances
        spot_balance = self.exchange.fetch_balance(params)
        print("Spot Balance:", spot_balance)

        # Fetch margin balances
        margin_balance = self.fetch_margin_balance_huobi(params)
        print("Margin Balance:", margin_balance)

        # Fetch futures balances
        futures_balance = self.fetch_futures_balance_huobi(params)
        print("Futures Balance:", futures_balance)

        # Fetch swap balances
        swaps_balance = self.fetch_swaps_balance_huobi(params)
        print("Swaps Balance:", swaps_balance)

        # Combine balances
        total_balance = self.exchange.deep_extend(spot_balance, margin_balance, futures_balance, swaps_balance)

        # Remove the unnecessary information
        parsed_balance = {}
        for currency in total_balance:
            parsed_balance[currency] = {
                'free': total_balance[currency]['free'],
                'used': total_balance[currency]['used'],
                'total': total_balance[currency]['total']
            }

        return parsed_balance

    def fetch_margin_balance_huobi(self, params={}):
        response = self.exchange.private_get_margin_accounts_balance(params)
        return self._parse_huobi_balance(response)

    def fetch_futures_balance_huobi(self, params={}):
        response = self.exchange.linearGetV2AccountInfo(params)
        return self._parse_huobi_balance(response)

    def fetch_swaps_balance_huobi(self, params={}):
        response = self.exchange.swapGetSwapBalance(params)
        return self._parse_huobi_balance(response)

    def _parse_huobi_balance(self, response):
        if 'data' in response:
            balance_data = response['data']
            parsed_balance = {}
            for currency_data in balance_data:
                currency = currency_data['currency']
                parsed_balance[currency] = {
                    'free': float(currency_data.get('available', 0)),
                    'used': float(currency_data.get('frozen', 0)),
                    'total': float(currency_data.get('balance', 0))
                }
            return parsed_balance
        else:
            return {}

    def get_price_precision(self, symbol):
        market = self.exchange.market(symbol)
        smallest_increment = market['precision']['price']
        price_precision = len(str(smallest_increment).split('.')[-1])
        return price_precision

    def get_precision_bybit(self, symbol):
        markets = self.exchange.fetch_derivatives_markets()
        for market in markets:
            if market['symbol'] == symbol:
                return market['precision']
        return None

    def get_balance(self, quote: str) -> dict:
        values = {
            "available_balance": 0.0,
            "pnl": 0.0,
            "upnl": 0.0,
            "wallet_balance": 0.0,
            "equity": 0.0,
        }
        try:
            data = self.exchange.fetch_balance()
            if "info" in data:
                if "result" in data["info"]:
                    if quote in data["info"]["result"]:
                        values["available_balance"] = float(
                            data["info"]["result"][quote]["available_balance"]
                        )
                        values["pnl"] = float(
                            data["info"]["result"][quote]["realised_pnl"]
                        )
                        values["upnl"] = float(
                            data["info"]["result"][quote]["unrealised_pnl"]
                        )
                        values["wallet_balance"] = round(
                            float(data["info"]["result"][quote]["wallet_balance"]), 2
                        )
                        values["equity"] = round(
                            float(data["info"]["result"][quote]["equity"]), 2
                        )
        except Exception as e:
            logging.info(f"An unknown error occurred in get_balance(): {e}")
        return values
    
    # Universal
    def fetch_ohlcv(self, symbol, timeframe='1d', limit=None):
        """
        Fetch OHLCV data for the given symbol and timeframe.

        :param symbol: Trading symbol.
        :param timeframe: Timeframe string.
        :param limit: Limit the number of returned data points.
        :return: DataFrame with OHLCV data.
        """
        try:
            # Fetch the OHLCV data from the exchange
            ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)  # Pass the limit parameter

            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

            df.set_index('timestamp', inplace=True)

            return df

        except ccxt.BaseError as e:
            print(f'Failed to fetch OHLCV data: {e}')
            return pd.DataFrame()

    def get_orderbook(self, symbol, max_retries=3, retry_delay=5) -> dict:
        values = {"bids": [], "asks": []}

        for attempt in range(max_retries):
            try:
                data = self.exchange.fetch_order_book(symbol)
                if "bids" in data and "asks" in data:
                    if len(data["bids"]) > 0 and len(data["asks"]) > 0:
                        if len(data["bids"][0]) > 0 and len(data["asks"][0]) > 0:
                            values["bids"] = data["bids"]
                            values["asks"] = data["asks"]
                break  # if the fetch was successful, break out of the loop

            except HTTPError as http_err:
                print(f"HTTP error occurred: {http_err} - {http_err.response.text}")

                if "Too many visits" in str(http_err) or (http_err.response.status_code == 429):
                    if attempt < max_retries - 1:
                        delay = retry_delay * (attempt + 1)  # Variable delay
                        logging.info(f"Rate limit error in get_orderbook(). Retrying in {delay} seconds...")
                        time.sleep(delay)
                        continue
                else:
                    logging.error(f"HTTP error in get_orderbook(): {http_err.response.text}")
                    raise http_err

            except Exception as e:
                if attempt < max_retries - 1:  # if not the last attempt
                    logging.info(f"An unknown error occurred in get_orderbook(): {e}. Retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                else:
                    logging.error(f"Failed to fetch order book after {max_retries} attempts: {e}")
                    raise e  # If it's still failing after max_retries, re-raise the exception.

        return values
    
    # Bitget
    def get_positions_bitget(self, symbol) -> dict:
        values = {
            "long": {
                "qty": 0.0,
                "price": 0.0,
                "realised": 0,
                "cum_realised": 0,
                "upnl": 0,
                "upnl_pct": 0,
                "liq_price": 0,
                "entry_price": 0,
            },
            "short": {
                "qty": 0.0,
                "price": 0.0,
                "realised": 0,
                "cum_realised": 0,
                "upnl": 0,
                "upnl_pct": 0,
                "liq_price": 0,
                "entry_price": 0,
            },
        }
        try:
            data = self.exchange.fetch_positions([symbol])
            for position in data:
                side = position["side"]
                values[side]["qty"] = float(position["contracts"])  # Use "contracts" instead of "contractSize"
                values[side]["price"] = float(position["entryPrice"])
                values[side]["realised"] = round(float(position["info"]["achievedProfits"]), 4)
                values[side]["upnl"] = round(float(position["unrealizedPnl"]), 4)
                if position["liquidationPrice"] is not None:
                    values[side]["liq_price"] = float(position["liquidationPrice"])
                else:
                    print(f"Warning: liquidationPrice is None for {side} position")
                    values[side]["liq_price"] = None
                values[side]["entry_price"] = float(position["entryPrice"])
        except Exception as e:
            logging.info(f"An unknown error occurred in get_positions_bitget(): {e}")
        return values

    # Bybit
    def get_best_bid_ask_bybit(self, symbol):
        orderbook = self.exchange.get_orderbook(symbol)
        try:
            best_ask_price = orderbook['asks'][0][0]
        except IndexError:
            best_ask_price = None
        try:
            best_bid_price = orderbook['bids'][0][0]
        except IndexError:
            best_bid_price = None

        return best_bid_price, best_ask_price
    
    def get_all_open_orders_bybit(self):
        """
        Fetch all open orders for all symbols from the Bybit API.
        
        :return: A list of open orders for all symbols.
        """
        try:
            all_open_orders = self.exchange.fetch_open_orders()
            return all_open_orders
        except Exception as e:
            print(f"An error occurred while fetching all open orders: {e}")
            return []
        
    
    # # Huobi
    # def safe_order_operation(self, operation, *args, **kwargs):
    #     while True:
    #         try:
    #             return operation(*args, **kwargs)
    #         except ccxt.BaseError as e:
    #             if 'In settlement' in str(e) or 'In delivery' in str(e):
    #                 print(f"Contract is in settlement or delivery. Cannot perform operation currently. Retrying in 10 seconds...")
    #                 time.sleep(10)
    #             else:
    #                 raise

    # # Huobi
    # def safe_order_operation(self, operation, *args, **kwargs):
    #     while True:
    #         try:
    #             return operation(*args, **kwargs)
    #         except ccxt.BaseError as e:
    #             e_str = str(e)
    #             if 'In settlement' in e_str or 'In delivery' in e_str:
    #                 print(f"Contract is in settlement or delivery. Cannot perform operation currently. Retrying in 10 seconds...")
    #                 time.sleep(10)
    #             elif 'Insufficient close amount available' in e_str:
    #                 print(f"Insufficient close amount available. Retrying in 5 seconds...")
    #                 time.sleep(5)
    #             else:
    #                 raise

    #Huobi 
    def safe_order_operation(self, operation, *args, **kwargs):
        while True:
            try:
                return operation(*args, **kwargs)
            except ccxt.BaseError as e:
                e_str = str(e)
                if 'In settlement' in e_str or 'In delivery' in e_str or 'Settling. Unable to place/cancel orders currently.' in e_str:
                    print(f"Contract is in settlement or delivery. Cannot perform operation currently. Retrying in 10 seconds...")
                    time.sleep(10)
                elif 'Insufficient close amount available' in e_str:
                    print(f"Insufficient close amount available. Retrying in 5 seconds...")
                    time.sleep(5)
                else:
                    raise

    # Huobi     
    def get_contract_size_huobi(self, symbol, max_retries=3, retry_delay=5):
        for i in range(max_retries):
            try:
                markets = self.exchange.fetch_markets_by_type_and_sub_type('swap', 'linear')
                for market in markets:
                    if market['symbol'] == symbol:
                        return market['contractSize']
                # If the contract size is not found in the markets, return None
                return None

            except Exception as e:
                if i < max_retries - 1:  # If not the last attempt
                    logging.info(f"An unknown error occurred in get_contract_size_huobi(): {e}. Retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                else:
                    logging.info(f"Failed to fetch contract size after {max_retries} attempts: {e}")
                    raise e  # If it's still failing after max_retries, re-raise the exception.
 
    # Huobi
    def get_positions_huobi(self, symbol) -> dict:
        print(f"Symbol received in get_positions_huobi: {symbol}")
        self.exchange.load_markets()
        if symbol not in self.exchange.markets:
            print(f"Market symbol {symbol} not found in Huobi markets.")
            return None
        values = {
            "long": {
                "qty": 0.0,
                "price": 0.0,
                "realised": 0,
                "cum_realised": 0,
                "upnl": 0,
                "upnl_pct": 0,
                "liq_price": 0,
                "entry_price": 0,
            },
            "short": {
                "qty": 0.0,
                "price": 0.0,
                "realised": 0,
                "cum_realised": 0,
                "upnl": 0,
                "upnl_pct": 0,
                "liq_price": 0,
                "entry_price": 0,
            },
        }
        try:
            data = self.exchange.fetch_positions([symbol])
            for position in data:
                if "info" not in position or "direction" not in position["info"]:
                    continue
                side = "long" if position["info"]["direction"] == "buy" else "short"
                values[side]["qty"] = float(position["info"]["volume"])  # Updated to use 'volume'
                values[side]["price"] = float(position["info"]["cost_open"])
                values[side]["realised"] = float(position["info"]["profit"])
                values[side]["cum_realised"] = float(position["info"]["profit"])  # Huobi API doesn't seem to provide cumulative realised profit
                values[side]["upnl"] = float(position["info"]["profit_unreal"])
                values[side]["upnl_pct"] = float(position["info"]["profit_rate"])
                values[side]["liq_price"] = 0.0  # Huobi API doesn't seem to provide liquidation price
                values[side]["entry_price"] = float(position["info"]["cost_open"])
        except Exception as e:
            logging.info(f"An unknown error occurred in get_positions_huobi(): {e}")
        return values

    # Huobi debug
    def get_positions_debug(self):
        try:
            positions = self.exchange.fetch_positions()
            print(f"{positions}")
        except Exception as e:
            print(f"Error is {e}")

    def get_positions(self, symbol) -> dict:
        values = {
            "long": {
                "qty": 0.0,
                "price": 0.0,
                "realised": 0,
                "cum_realised": 0,
                "upnl": 0,
                "upnl_pct": 0,
                "liq_price": 0,
                "entry_price": 0,
            },
            "short": {
                "qty": 0.0,
                "price": 0.0,
                "realised": 0,
                "cum_realised": 0,
                "upnl": 0,
                "upnl_pct": 0,
                "liq_price": 0,
                "entry_price": 0,
            },
        }
        try:
            data = self.exchange.fetch_positions([symbol])
            if len(data) == 2:
                sides = ["long", "short"]
                for side in [0, 1]:
                    values[sides[side]]["qty"] = float(data[side]["contracts"])
                    values[sides[side]]["price"] = float(data[side]["entryPrice"])
                    values[sides[side]]["realised"] = round(
                        float(data[side]["info"]["realised_pnl"]), 4
                    )
                    values[sides[side]]["cum_realised"] = round(
                        float(data[side]["info"]["cum_realised_pnl"]), 4
                    )
                    if data[side]["info"]["unrealised_pnl"] is not None:
                        values[sides[side]]["upnl"] = round(
                            float(data[side]["info"]["unrealised_pnl"]), 4
                        )
                    if data[side]["precentage"] is not None:
                        values[sides[side]]["upnl_pct"] = round(
                            float(data[side]["precentage"]), 4
                        )
                    if data[side]["liquidationPrice"] is not None:
                        values[sides[side]]["liq_price"] = float(
                            data[side]["liquidationPrice"]
                        )
                    if data[side]["entryPrice"] is not None:
                        values[sides[side]]["entry_price"] = float(
                            data[side]["entryPrice"]
                        )
        except Exception as e:
            logging.info(f"An unknown error occurred in get_positions(): {e}")
        return values

    # Universal
    def get_current_price(self, symbol: str) -> float:
        try:
            ticker = self.exchange.fetch_ticker(symbol)
            if "bid" in ticker and "ask" in ticker:
                return (ticker["bid"] + ticker["ask"]) / 2
        except Exception as e:
            logging.error(f"An error occurred in get_current_price() for {symbol}: {e}")
            return None
        
    # Binance
    def get_current_price_binance(self, symbol: str) -> float:
        current_price = 0.0
        try:
            orderbook = self.exchange.fetch_order_book(symbol)
            highest_bid = orderbook['bids'][0][0] if len(orderbook['bids']) > 0 else None
            lowest_ask = orderbook['asks'][0][0] if len(orderbook['asks']) > 0 else None
            if highest_bid and lowest_ask:
                current_price = (highest_bid + lowest_ask) / 2
        except Exception as e:
            logging.info(f"An unknown error occurred in get_current_price_binance(): {e}")
        return current_price

    # def get_leverage_tiers_binance(self, symbols: Optional[List[str]] = None):
    #     try:
    #         tiers = self.exchange.fetch_leverage_tiers(symbols)
    #         for symbol, brackets in tiers.items():
    #             print(f"\nSymbol: {symbol}")
    #             for bracket in brackets:
    #                 print(f"Bracket ID: {bracket['bracket']}")
    #                 print(f"Initial Leverage: {bracket['initialLeverage']}")
    #                 print(f"Notional Cap: {bracket['notionalCap']}")
    #                 print(f"Notional Floor: {bracket['notionalFloor']}")
    #                 print(f"Maintenance Margin Ratio: {bracket['maintMarginRatio']}")
    #                 print(f"Cumulative: {bracket['cum']}")
    #     except Exception as e:
    #         logging.error(f"An error occurred while fetching leverage tiers: {e}")

    def get_symbol_info_binance(self, symbol):
        try:
            markets = self.exchange.fetch_markets()
            print(markets)
            for market in markets:
                if market['symbol'] == symbol:
                    filters = market['info']['filters']
                    min_notional = [f['minNotional'] for f in filters if f['filterType'] == 'MIN_NOTIONAL'][0]
                    min_qty = [f['minQty'] for f in filters if f['filterType'] == 'LOT_SIZE'][0]
                    return min_notional, min_qty
        except Exception as e:
            logging.error(f"An error occurred while fetching symbol info: {e}")

    # def get_market_data_binance(self, symbol):
    #     market_data = self.exchange.load_markets(reload=True)  # Force a reload to get fresh data
    #     return market_data[symbol]

    # def get_market_data_binance(self, symbol):
    #     market_data = self.exchange.load_markets(reload=True)  # Force a reload to get fresh data
    #     print("Symbols:", market_data.keys())  # Print out all available symbols
    #     return market_data[symbol]

    def get_min_lot_size_binance(self, symbol):
        market_data = self.get_market_data_binance(symbol)

        # Extract the filters from the market data
        filters = market_data['info']['filters']

        # Find the 'LOT_SIZE' filter and get its 'minQty' value
        for f in filters:
            if f['filterType'] == 'LOT_SIZE':
                return float(f['minQty'])

        # If no 'LOT_SIZE' filter was found, return None
        return None

    def get_moving_averages(self, symbol: str, timeframe: str = "1m", num_bars: int = 20, max_retries=3, retry_delay=5) -> dict:
        values = {"MA_3_H": 0.0, "MA_3_L": 0.0, "MA_6_H": 0.0, "MA_6_L": 0.0}

        for i in range(max_retries):
            try:
                bars = self.exchange.fetch_ohlcv(
                    symbol=symbol, timeframe=timeframe, limit=num_bars
                )
                df = pd.DataFrame(
                    bars, columns=["Time", "Open", "High", "Low", "Close", "Volume"]
                )
                df["Time"] = pd.to_datetime(df["Time"], unit="ms")
                df["MA_3_High"] = df.High.rolling(3).mean()
                df["MA_3_Low"] = df.Low.rolling(3).mean()
                df["MA_6_High"] = df.High.rolling(6).mean()
                df["MA_6_Low"] = df.Low.rolling(6).mean()
                values["MA_3_H"] = df["MA_3_High"].iat[-1]
                values["MA_3_L"] = df["MA_3_Low"].iat[-1]
                values["MA_6_H"] = df["MA_6_High"].iat[-1]
                values["MA_6_L"] = df["MA_6_Low"].iat[-1]
                break  # If the fetch was successful, break out of the loop
            except Exception as e:
                if i < max_retries - 1:  # If not the last attempt
                    logging.info(f"An unknown error occurred in get_moving_averages(): {e}. Retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                else:
                    logging.info(f"Failed to fetch moving averages after {max_retries} attempts: {e}")
                    raise e  # If it's still failing after max_retries, re-raise the exception.
        
        return values

    def get_order_status(self, order_id, symbol):
        try:
            # Fetch the order details from the exchange using the order ID
            order_details = self.fetch_order(order_id, symbol)

            logging.info(f"Order details for {symbol}: {order_details}")

            # Extract and return the order status
            return order_details['status']
        except Exception as e:
            logging.error(f"An error occurred fetching order status for {order_id} on {symbol}: {e}")
            return None


    def get_open_orders(self, symbol: str) -> list:
        open_orders_list = []
        try:
            orders = self.exchange.fetch_open_orders(symbol)
            #print(orders)
            if len(orders) > 0:
                for order in orders:
                    if "info" in order:
                        #print(f"Order info: {order['info']}")  # Debug print
                        order_info = {
                            "id": order["info"]["orderId"],
                            "price": float(order["info"]["price"]),
                            "qty": float(order["info"]["qty"]),
                            "order_status": order["info"]["orderStatus"],
                            "side": order["info"]["side"],
                            "reduce_only": order["info"]["reduceOnly"],  # Update this line
                            "position_idx": int(order["info"]["positionIdx"])  # Add this line
                        }
                        open_orders_list.append(order_info)
        except Exception as e:
            logging.info(f"Bybit An unknown error occurred in get_open_orders(): {e}")
        return open_orders_list

    # Binance
    # def get_open_orders_binance(self, symbol: str) -> list:
    #     open_orders_list = []
    #     try:
    #         orders = self.exchange.fetch_open_orders(symbol)
    #         if len(orders) > 0:
    #             for order in orders:
    #                 if "info" in order:
    #                     order_info = {
    #                         "id": order["id"],
    #                         "price": float(order["price"]),
    #                         "qty": float(order["amount"]),
    #                         "order_status": order["status"],
    #                         "side": order["side"],
    #                         "reduce_only": False,  # Binance does not have a "reduceOnly" field
    #                         "position_idx": None  # Binance does not have a "positionIdx" field
    #                     }
    #                     open_orders_list.append(order_info)
    #     except Exception as e:
    #         logging.info(f"An unknown error occurred in get_open_orders(): {e}")
    #     return open_orders_list

    # def get_open_orders_binance(self, symbol: str):
    #     """
    #     Fetch open orders for a futures market in hedged mode.
        
    #     :param str symbol: Unified market symbol
    #     :return: List of order structures
    #     """
    #     params = {
    #         'type': 'future'   # specify that this is a futures market
    #     }

    #     try:
    #         # leverage ccxt's fetch_open_orders method
    #         orders = self.exchange.fetch_open_orders(symbol, params=params)
    #         return orders
    #     except Exception as e:
    #         print(f"An error occurred while fetching open orders: {e}")
    #         return None

    def get_open_orders_binance(self, symbol: str) -> list:
        open_orders_list = []
        try:
            orders = self.exchange.fetch_open_orders(symbol)
            #print(orders)
            if len(orders) > 0:
                for order in orders:
                    if "info" in order:
                        order_info = {
                            "id": order["info"]["orderId"],
                            "price": order["info"]["price"],
                            "amount": float(order["info"]["origQty"]),
                            "status": order["info"]["status"],
                            "side": order["info"]["side"],
                            "reduce_only": order["info"]["reduceOnly"],
                            "type": order["info"]["type"]
                        }
                        open_orders_list.append(order_info)
        except Exception as e:
            logging.info(f"An unknown error occurred in get_open_orders_binance(): {e}")
        return open_orders_list

    def cancel_all_entries_binance(self, symbol: str):
        try:
            # Fetch all open orders
            open_orders = self.get_open_orders_binance(symbol)

            for order in open_orders:
                # If the order is a 'LIMIT' order (i.e., an 'entry' order), cancel it
                if order['type'].upper() == 'LIMIT':
                    self.exchange.cancel_order(order['id'], symbol)
        except Exception as e:
            print(f"An error occurred while canceling entry orders: {e}")

    def get_open_orders_bybit_unified(self, symbol: str) -> list:
        open_orders_list = []
        try:
            orders = self.exchange.fetch_open_orders(symbol)
            #print(orders)
            if len(orders) > 0:
                for order in orders:
                    if "info" in order:
                        order_info = {
                            "id": order["id"],
                            "price": float(order["price"]),
                            "qty": float(order["amount"]),
                            "order_status": order["status"],
                            "side": order["side"],
                            "reduce_only": order["reduceOnly"],
                            "position_idx": int(order["info"]["positionIdx"])
                        }
                        open_orders_list.append(order_info)
        except Exception as e:
            logging.info(f"An unknown error occurred in get_open_orders(): {e}")
        return open_orders_list

    def get_open_orders_huobi(self, symbol: str) -> list:
        open_orders_list = []
        try:
            orders = self.exchange.fetch_open_orders(symbol)
            #print(f"Debug: {orders}")
            if len(orders) > 0:
                for order in orders:
                    if "info" in order:
                        try:
                            # Extracting the necessary fields for Huobi orders
                            order_info = {
                                "id": order["info"]["order_id"],
                                "price": float(order["info"]["price"]),
                                "qty": float(order["info"]["volume"]),
                                "order_status": order["info"]["status"],
                                "side": order["info"]["direction"],  # assuming 'direction' indicates 'buy' or 'sell'
                            }
                            open_orders_list.append(order_info)
                        except KeyError as e:
                            logging.info(f"Key {e} not found in order info.")
        except Exception as e:
            logging.info(f"An unknown error occurred in get_open_orders_huobi(): {e}")
        return open_orders_list

    def debug_open_orders(self, symbol: str) -> None:
        try:
            open_orders = self.exchange.fetch_open_orders(symbol)
            logging.info(open_orders)
        except:
            logging.info(f"Fuck")

    def cancel_long_entry(self, symbol: str) -> None:
        self._cancel_entry(symbol, order_side="Buy")

    def cancel_short_entry(self, symbol: str) -> None:
        self._cancel_entry(symbol, order_side="Sell")

    def _cancel_entry(self, symbol: str, order_side: str) -> None:
        try:
            orders = self.exchange.fetch_open_orders(symbol)
            for order in orders:
                if "info" in order:
                    order_id = order["info"]["order_id"]
                    order_status = order["info"]["order_status"]
                    side = order["info"]["side"]
                    reduce_only = order["info"]["reduce_only"]
                    if (
                        order_status != "Filled"
                        and side == order_side
                        and order_status != "Cancelled"
                        and not reduce_only
                    ):
                        self.exchange.cancel_order(symbol=symbol, id=order_id)
                        logging.info(f"Cancelling {order_side} order: {order_id}")
        except Exception as e:
            logging.info(f"An unknown error occurred in _cancel_entry(): {e}")

    def cancel_all_open_orders_bybit(self, derivatives: bool = False, params={}):
        """
        Cancel all open orders for all symbols.

        :param bool derivatives: Whether to cancel derivative orders.
        :param dict params: Additional parameters for the API call.
        :return: A list of canceled orders.
        """
        max_retries = 10  # Maximum number of retries
        retry_delay = 5  # Delay (in seconds) between retries

        for retry in range(max_retries):
            try:
                if derivatives:
                    return self.exchange.cancel_all_derivatives_orders(None, params)
                else:
                    return self.exchange.cancel_all_orders(None, params)
            except ccxt.RateLimitExceeded as e:
                # If rate limit error and not the last retry, then wait and try again
                if retry < max_retries - 1:
                    logging.info(f"Rate limit exceeded. Retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                else:  # If it's the last retry, raise the error
                    logging.error(f"Rate limit exceeded after {max_retries} retries.")
                    raise e
            except Exception as ex:
                # If any other exception, log it and re-raise
                logging.error(f"Error occurred while canceling orders: {ex}")
                raise ex

    def health_check(self, interval_seconds=300):
        """
        Periodically checks the health of the exchange and cancels all open orders.

        :param interval_seconds: The time interval in seconds between each health check.
        """
        while True:
            try:
                logging.info("Performing health check...")  # Log start of health check
                # You can add more health check logic here
                
                # Cancel all open orders
                self.cancel_all_open_orders_bybit()
                
                logging.info("Health check complete.")  # Log end of health check
            except Exception as e:
                logging.error(f"An error occurred during the health check: {e}")  # Log any errors
                
            time.sleep(interval_seconds)

    # def cancel_all_auto_reduce_orders_bybit(self, symbol: str, auto_reduce_order_ids: List[str]):
    #     try:
    #         orders = self.fetch_open_orders(symbol)
    #         logging.info(f"[Thread ID: {threading.get_ident()}] cancel_all_auto_reduce_orders function in exchange class accessed")
    #         logging.info(f"Fetched orders: {orders}")

    #         for order in orders:
    #             if order['status'] in ['open', 'partially_filled']:
    #                 order_id = order['id']
    #                 # Check if the order ID is in the list of auto-reduce orders
    #                 if order_id in auto_reduce_order_ids:
    #                     self.cancel_order(order_id, symbol)
    #                     logging.info(f"Cancelling auto-reduce order: {order_id}")

    #     except Exception as e:
    #         logging.warning(f"An unknown error occurred in cancel_all_auto_reduce_orders_bybit(): {e}")

    #v5 
    def cancel_all_reduce_only_orders_bybit(self, symbol: str) -> None:
        try:
            orders = self.exchange.fetch_open_orders(symbol)
            logging.info(f"[Thread ID: {threading.get_ident()}] cancel_all_reduce_only_orders_bybit function accessed")
            logging.info(f"Fetched orders: {orders}")

            for order in orders:
                if order['status'] in ['open', 'partially_filled']:
                    # Check if the order is a reduce-only order
                    if order['reduceOnly']:
                        order_id = order['id']
                        self.exchange.cancel_order(order_id, symbol)
                        logging.info(f"Cancelling reduce-only order: {order_id}")

        except Exception as e:
            logging.info(f"An error occurred in cancel_all_reduce_only_orders_bybit(): {e}")

    # v5
    def cancel_all_entries_bybit(self, symbol: str) -> None:
        try:
            orders = self.exchange.fetch_open_orders(symbol)
            logging.info(f"[Thread ID: {threading.get_ident()}] cancel_all_entries function in exchange class accessed")
            logging.info(f"Fetched orders: {orders}")

            for order in orders:
                if order['status'] in ['open', 'partially_filled']:
                    # Check if the order is not a reduce-only order
                    if not order['reduceOnly']:
                        order_id = order['id']
                        self.exchange.cancel_order(order_id, symbol)
                        logging.info(f"Cancelling order: {order_id}")

        except Exception as e:
            logging.info(f"An unknown error occurred in cancel_all_entries_bybit(): {e}")

    def cancel_entry(self, symbol: str) -> None:
        try:
            order = self.exchange.fetch_open_orders(symbol)
            #print(f"Open orders for {symbol}: {order}")
            if len(order) > 0:
                if "info" in order[0]:
                    order_id = order[0]["info"]["order_id"]
                    order_status = order[0]["info"]["order_status"]
                    order_side = order[0]["info"]["side"]
                    reduce_only = order[0]["info"]["reduce_only"]
                    if (
                        order_status != "Filled"
                        and order_side == "Buy"
                        and order_status != "Cancelled"
                        and not reduce_only
                    ):
                        self.exchange.cancel_order(symbol=symbol, id=order_id)
                        logging.info(f"Cancelling order: {order_id}")
                    elif (
                        order_status != "Filled"
                        and order_side == "Sell"
                        and order_status != "Cancelled"
                        and not reduce_only
                    ):
                        self.exchange.cancel_order(symbol=symbol, id=order_id)
                        logging.info(f"Cancelling order: {order_id}")
        except Exception as e:
            logging.info(f"An unknown error occurred in cancel_entry(): {e}")
    
    def cancel_close(self, symbol: str, side: str) -> None:
        try:
            order = self.exchange.fetch_open_orders(symbol)
            if len(order) > 0:
                if "info" in order[0]:
                    order_id = order[0]["info"]["order_id"]
                    order_status = order[0]["info"]["order_status"]
                    order_side = order[0]["info"]["side"]
                    reduce_only = order[0]["info"]["reduce_only"]
                    if (
                        order_status != "Filled"
                        and order_side == "Buy"
                        and side == "long"
                        and order_status != "Cancelled"
                        and reduce_only
                    ):
                        self.exchange.cancel_order(symbol=symbol, id=order_id)
                        logging.info(f"Cancelling order: {order_id}")
                    elif (
                        order_status != "Filled"
                        and order_side == "Sell"
                        and side == "short"
                        and order_status != "Cancelled"
                        and reduce_only
                    ):
                        self.exchange.cancel_order(symbol=symbol, id=order_id)
                        logging.info(f"Cancelling order: {order_id}")
        except Exception as e:
            logging.info(f"{e}")

    def create_take_profit_order(self, symbol, order_type, side, amount, price=None, reduce_only=False):
        if order_type == 'limit':
            if price is None:
                raise ValueError("A price must be specified for a limit order")

            if side not in ["buy", "sell"]:
                raise ValueError(f"Invalid side: {side}")

            params = {"reduceOnly": reduce_only}
            return self.exchange.create_order(symbol, order_type, side, amount, price, params)
        else:
            raise ValueError(f"Unsupported order type: {order_type}")

    def create_market_order(self, symbol: str, side: str, amount: float, params={}, close_position: bool = False) -> None:
        try:
            if side not in ["buy", "sell"]:
                logging.info(f"side {side} does not exist")
                return

            order_type = "market"

            # Determine the correct order side for closing positions
            if close_position:
                market = self.exchange.market(symbol)
                if market['type'] in ['swap', 'future']:
                    if side == "buy":
                        side = "close_short"
                    elif side == "sell":
                        side = "close_long"

            response = self.exchange.create_order(symbol, order_type, side, amount, params=params)
            return response
        except Exception as e:
            logging.info(f"An unknown error occurred in create_market_order(): {e}")

    def test_func(self):
        try:
            market = self.exchange.market('DOGEUSDT')
            print(market['info'])
        except Exception as e:
            print(f"Exception caught in test func {e}")

    def create_limit_order(self, symbol, side, amount, price, reduce_only=False, **params):
        if side == "buy":
            return self.create_limit_buy_order(symbol, amount, price, reduce_only=reduce_only, **params)
        elif side == "sell":
            return self.create_limit_sell_order(symbol, amount, price, reduce_only=reduce_only, **params)
        else:
            raise ValueError(f"Invalid side: {side}")

    def create_limit_buy_order(self, symbol: str, qty: float, price: float, **params) -> None:
        self.exchange.create_order(
            symbol=symbol,
            type='limit',
            side='buy',
            amount=qty,
            price=price,
            **params
        )

    def create_limit_sell_order(self, symbol: str, qty: float, price: float, **params) -> None:
        self.exchange.create_order(
            symbol=symbol,
            type='limit',
            side='sell',
            amount=qty,
            price=price,
            **params
        )

    def create_order(self, symbol, order_type, side, amount, price=None, reduce_only=False, **params):
        if reduce_only:
            params.update({'reduceOnly': 'true'})

        if self.exchange_id == 'bybit':
            order = self.exchange.create_order(symbol, order_type, side, amount, price, params=params)
        else:
            if order_type == 'limit':
                if side == "buy":
                    order = self.create_limit_buy_order(symbol, amount, price, **params)
                elif side == "sell":
                    order = self.create_limit_sell_order(symbol, amount, price, **params)
                else:
                    raise ValueError(f"Invalid side: {side}")
            elif order_type == 'market':
                # Special handling for market orders
                order = self.exchange.create_order(symbol, 'market', side, amount, None, params=params)
            else:
                raise ValueError("Invalid order type. Use 'limit' or 'market'.")

        return order

    # def create_order(self, symbol, order_type, side, amount, price=None, reduce_only=False, **params):
    #     if reduce_only:
    #         params.update({'reduceOnly': 'true'})

    #     if self.exchange_id == 'bybit':
    #         order = self.exchange.create_order(symbol, order_type, side, amount, price, params=params)
    #     else:
    #         if order_type == 'limit':
    #             if side == "buy":
    #                 order = self.create_limit_buy_order(symbol, amount, price, **params)
    #             elif side == "sell":
    #                 order = self.create_limit_sell_order(symbol, amount, price, **params)
    #             else:
    #                 raise ValueError(f"Invalid side: {side}")
    #         elif order_type == 'market':
    #             #... handle market orders if necessary
    #             order = self.create_market_order(symbol, side, amount, params)
    #         else:
    #             raise ValueError("Invalid order type. Use 'limit' or 'market'.")

    #     return order


    # def get_symbol_precision_bybit(self, symbol: str) -> Tuple[int, int]:
    #     try:
    #         market = self.exchange.market(symbol)
    #         price_precision = int(market['precision']['price'])
    #         quantity_precision = int(market['precision']['amount'])
    #         return price_precision, quantity_precision
    #     except Exception as e:
    #         print(f"An error occurred: {e}")
    #         return None, None

    # def get_symbol_precision_bybit(self, symbol: str) -> Tuple[int, int]:
    #     market = self.exchange.market(symbol)
    #     price_precision = int(market['precision']['price'])
    #     quantity_precision = int(market['precision']['amount'])
    #     return price_precision, quantity_precision

    # def _get_symbols(self):
    #     markets = self.exchange.load_markets()
    #     symbols = [market['symbol'] for market in markets.values()]
    #     return symbols