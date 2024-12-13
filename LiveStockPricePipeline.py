import websocket
import json
import time
from collections import defaultdict
from datetime import datetime
import numpy as np
import pytz
import logging

class FinnhubWebSocket:
    def __init__(self, api_key, tickers, initial_reconnect_delay=3, max_reconnect_delay=120, max_retries=10):
        self.api_key = api_key
        self.tickers = tickers
        self.ws = None
        self.cache = defaultdict(list)
        self.latest_vwap = {}
        self.last_minute = int(time.time() / 60)

        # Backoff and retry configuration
        self.initial_reconnect_delay = initial_reconnect_delay
        self.max_reconnect_delay = max_reconnect_delay
        self.current_reconnect_delay = initial_reconnect_delay
        self.max_retries = max_retries
        self.retries = 0

        # Active flag to stop reconnect attempts when pipelines are stopped
        self.active = True

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(levelname)s - %(message)s",
            handlers=[
                logging.StreamHandler(),
                logging.FileHandler("finnhub_websocket.log"),
            ],
        )

    def stop(self):
        # Call this method when pipelines are stopped
        self.active = False

    def convert_to_est(self, timestamp_ms):
        timestamp_s = timestamp_ms / 1000
        utc_time = datetime.utcfromtimestamp(timestamp_s)
        est_timezone = pytz.timezone("US/Eastern")
        est_time = utc_time.replace(tzinfo=pytz.utc).astimezone(est_timezone)
        return est_time.strftime("%Y-%m-%d %I:%M:%S %p %Z")

    def calculate_vwap(self, ticker_data):
        total_weighted_price = sum([entry["price"] * entry["volume"] for entry in ticker_data])
        total_volume = sum([entry["volume"] for entry in ticker_data])
        if total_volume > 0:
            return np.round(total_weighted_price / total_volume,2)
        return None

    def dump_cache_to_file(self):
        current_time = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{current_time}.txt"
        vwap_results = {}

        with open(filename, "w") as file:
            for ticker, data in self.cache.items():
                vwap = self.calculate_vwap(data)
                vwap_results[ticker] = vwap
                file.write(f"Ticker: {ticker}, VWAP: {vwap}, Data: {data}\n")

        logging.info(f"Cache dumped and saved to {filename}")
        logging.info(f"VWAP results updated")

        # Keep only the last entry for each ticker
        for ticker, data in self.cache.items():
            if data:
                self.cache[ticker] = [data[-1]]

        self.latest_vwap = vwap_results
        return vwap_results

    def on_message(self, ws, message):
        data = json.loads(message)
        if "data" in data:
            for entry in data["data"]:
                ticker = entry["s"]
                price = entry["p"]
                volume = entry["v"]
                timestamp = entry["t"]
                est_time = self.convert_to_est(timestamp)
                self.cache[ticker].append({"price": price, "volume": volume, "time": est_time})

        current_minute = int(time.time() / 60)
        if current_minute != self.last_minute:
            self.dump_cache_to_file()
            self.last_minute = current_minute

    def on_error(self, ws, error):
        logging.error(f"WebSocket Error: {error}")

    def on_close(self, ws, close_status_code, close_msg):
        logging.warning("WebSocket closed.")
        self.attempt_reconnect()

    def on_open(self, ws):
        for ticker in self.tickers:
            subscribe_message = {"type": "subscribe", "symbol": ticker}
            ws.send(json.dumps(subscribe_message))
            logging.info(f"Subscribed to: {ticker}")

        # Reset reconnect delay and retries on a successful connection
        self.current_reconnect_delay = self.initial_reconnect_delay
        self.retries = 0

    def attempt_reconnect(self):
        # Only attempt reconnect if still active
        if not self.active:
            logging.info("This WebSocket instance is no longer active. Stopping reconnect attempts.")
            return

        self.retries += 1
        if self.retries > self.max_retries:
            logging.error("Max retries reached. Stopping reconnect attempts.")
            return

        logging.warning(f"Attempting to reconnect in {self.current_reconnect_delay} seconds...")
        time.sleep(self.current_reconnect_delay)

        # Exponential backoff
        self.current_reconnect_delay = min(self.current_reconnect_delay * 2, self.max_reconnect_delay)

        if self.active:
            self.start()

    def start(self):
        if not self.active:
            logging.info("WebSocket start called but this instance is no longer active. Not starting.")
            return

        websocket.enableTrace(False)
        self.ws = websocket.WebSocketApp(
            f"wss://ws.finnhub.io?token={self.api_key}",
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close
        )
        self.ws.on_open = self.on_open
        self.ws.run_forever()
