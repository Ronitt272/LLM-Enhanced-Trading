from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from typing import List
import uvicorn
import logging
from datetime import datetime
import threading
import time
from html import escape

# Importing custom modules
from LiveStockPricePipeline import FinnhubWebSocket
from TextFetchPipeline import TextFetchPipeline
from SignalGenerator import SignalGeneration

app = FastAPI()

# Global references
tickers = []
stock_pipeline = None
text_pipeline = None
signal_generator = None
pipeline_threads = []
last_selected_ticker = None
current_ticker = None

# Example keys (replace with your own)
reddit_client_secret = 'sfFdYwuZWqofiqro51zGlKcJiC2YiQ'
reddit_client_id = 'mNEnO4swPUjezEf92dxgxg'
reddit_user_agent = 'LLM class project'
news_api_key = '401ac3f457c64606a460107936a62709'
cohere_key = 'rUFyKOiTQawP7WX2bNXNQDGtqyZ63YVZWxVCAba5'
finnhub_token = 'ctahnvpr01qrt5hhnbg0ctahnvpr01qrt5hhnbgg'


class CombinedData(BaseModel):
    ticker: str
    VWAP: float
    time: str
    text: str
    sentiment: int
    probability: float
    MA_Crossover: int
    RSI: int
    Breakout: int
    Oscillator: int
    Signal: int


def stop_pipelines():
    global stock_pipeline, text_pipeline, signal_generator, pipeline_threads
    if stock_pipeline is not None:
        stock_pipeline.stop()  # Mark the old pipeline as inactive

    stock_pipeline = None
    text_pipeline = None
    signal_generator = None
    pipeline_threads = []


def start_pipelines():
    global stock_pipeline, text_pipeline, signal_generator, pipeline_threads, current_ticker

    pipeline_threads = []

    text_pipeline = TextFetchPipeline(
        news_api_key,
        reddit_client_id,
        reddit_client_secret,
        reddit_user_agent,
        cohere_key,
        current_ticker
    )

    stock_pipeline = FinnhubWebSocket(finnhub_token, tickers)
    signal_generator = SignalGeneration(buffer_size=30)

    ws_thread = threading.Thread(target=stock_pipeline.start, daemon=True)
    ws_thread.start()

    vwap_thread = threading.Thread(target=signal_generator.collect_vwap, args=(stock_pipeline,), daemon=True)
    vwap_thread.start()

    text_thread = threading.Thread(target=text_pipeline.run_periodically, daemon=True)
    text_thread.start()

    pipeline_threads = [ws_thread, vwap_thread, text_thread]


def signal_icon(value: int) -> str:
    if value == 1:
        return "✅"
    elif value == -1:
        return "❌"
    return "Hold"


@app.get("/", response_class=HTMLResponse)
async def home():
    html_content = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Stock Dashboard</title>
    </head>
    <body>
        <h1>Enter a Stock Ticker</h1>
        <form action="/select_ticker" method="post">
            <input type="text" name="ticker" placeholder="e.g. AAPL" required>
            <button type="submit">Submit</button>
        </form>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


@app.post("/select_ticker")
async def select_ticker(ticker: str = Form(...)):
    global tickers, last_selected_ticker, current_ticker

    ticker = ticker.upper().strip()

    if ticker != current_ticker:
        stop_pipelines()
        tickers = [ticker]
        last_selected_ticker = ticker
        current_ticker = ticker
        start_pipelines()

    while True:
        if stock_pipeline is None or signal_generator is None or text_pipeline is None:
            time.sleep(0.5)
            continue

        latest_vwap = stock_pipeline.latest_vwap.get(ticker, None)
        latest_text = text_pipeline.agg_text.get(ticker, None)
        latest_signals = signal_generator.get_signals().get(ticker, {})
        

        if latest_vwap is not None and latest_text is not None and len(latest_signals) > 0:
            return RedirectResponse(url="/dashboard", status_code=303)

        time.sleep(0.5)


trade_log = []


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    global last_selected_ticker, current_ticker, trade_log
    ticker = last_selected_ticker
    if ticker is None:
        return HTMLResponse(content="<h2>No ticker selected.</h2>")

    if stock_pipeline is None or text_pipeline is None or signal_generator is None:
        return HTMLResponse(content="<h2>Pipeline not running. Please go back and select a ticker.</h2>")

    latest_vwap = stock_pipeline.latest_vwap.get(ticker, 0.0)
    latest_signals = signal_generator.get_signals().get(ticker, {})
    latest_news = text_pipeline.agg_text.get(ticker, "No data available!")
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    sentiment = 1
    probability = 0.9
    ma_cross = latest_signals.get("SMA", 0)
    rsi = latest_signals.get("RSI", 0)
    osc = latest_signals.get("Stochastic", 0)
    signal = 1
    breakout = 0

    if not trade_log:
        trade_log_html = "<p>No trades have been made yet.</p>"
    else:
        trade_log_html = "<ul>"
        for entry in trade_log:
            trade_log_html += f"<li>{escape(entry)}</li>"
        trade_log_html += "</ul>"

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Stock Dashboard for {escape(ticker)}</title>
        <style>
            body {{
                font-family: Arial, sans-serif;
                margin: 20px;
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
            }}
            th, td {{
                padding: 12px;
                text-align: center;
                border: 1px solid black;
                vertical-align: top;
            }}
            th {{
                background-color: #f2f2f2;
                font-size: 16px;
            }}
            td.text-column {{
                max-width: 300px;
                text-align: left;
                word-wrap: break-word;
                overflow-wrap: break-word;
            }}
            .signal-icon {{
                font-size: 20px;
            }}
            h1 {{
                text-align: center;
            }}
        </style>
    </head>
    <body>
        <h1>Stock Dashboard</h1>
        <table>
            <thead>
                <tr>
                    <th>Ticker</th>
                    <th>VWAP</th>
                    <th>Time</th>
                    <th>Text</th>
                    <th>Sentiment</th>
                    <th>Probability</th>
                    <th>MA Crossover</th>
                    <th>RSI</th>
                    <th>Breakout</th>
                    <th>Oscillator</th>
                    <th>Signal</th>
                    <th>Buy</th>
                    <th>Sell</th>
                </tr>
            </thead>
            <tbody>
                <tr>
                    <td>{escape(ticker)}</td>
                    <td>{latest_vwap:.2f}</td>
                    <td>{escape(current_time)}</td>
                    <td class="text-column">{escape(latest_news)}</td>
                    <td>{"Positive" if sentiment == 1 else "Negative"}</td>
                    <td>{probability}</td>
                    <td class="signal-icon">{signal_icon(ma_cross)}</td>
                    <td class="signal-icon">{signal_icon(rsi)}</td>
                    <td class="signal-icon">{signal_icon(breakout)}</td>
                    <td class="signal-icon">{signal_icon(osc)}</td>
                    <td class="signal-icon">{signal_icon(signal)}</td>
                    <td>
                        <form action="/buy" method="post">
                            <input type="hidden" name="ticker" value="{escape(ticker)}">
                            <input type="hidden" name="price" value="{latest_vwap}">
                            <input type="number" name="amount" min="1" required>
                            <button type="submit">Buy</button>
                        </form>
                    </td>
                    <td>
                        <form action="/sell" method="post">
                            <input type="hidden" name="ticker" value="{escape(ticker)}">
                            <input type="hidden" name="price" value="{latest_vwap}">
                            <input type="number" name="amount" min="1" required>
                            <button type="submit">Sell</button>
                        </form>
                    </td>
                </tr>
            </tbody>
        </table>

        <h2>Trade Log</h2>
        <div id="trade-log">
            {trade_log_html}
        </div>

        <br><br>
        <a href="/">Go Back</a>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)



@app.post("/buy")
async def buy(ticker: str = Form(...), amount: int = Form(...), price: float = Form(...)):
    trade_entry = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Bought {amount} shares of {ticker} @ {price}"
    trade_log.append(trade_entry)
    return RedirectResponse(url="/dashboard", status_code=303)


@app.post("/sell")
async def sell(ticker: str = Form(...), amount: int = Form(...), price: float = Form(...)):
    trade_entry = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Sold {amount} shares of {ticker} @ {price}"
    trade_log.append(trade_entry)
    return RedirectResponse(url="/dashboard", status_code=303)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
