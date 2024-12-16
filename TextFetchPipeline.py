from datetime import datetime, timedelta
import praw
import cohere
from newsapi import NewsApiClient
import pandas as pd
from datetime import datetime
import logging
import pytz
import time
import warnings

warnings.filterwarnings('ignore')


class TextFetchPipeline:
    def __init__(self, news_api_key, reddit_client_id, reddit_client_secret, reddit_user_agent, cohere_key, current_ticker):
        # Initialize APIs
        self.news_api = NewsApiClient(api_key=news_api_key)
        self.reddit = praw.Reddit(
            client_id=reddit_client_id,
            client_secret=reddit_client_secret,
            user_agent=reddit_user_agent
        )

        # Cache to avoid duplicate processing
        self.news_cache = set()
        self.reddit_cache = set()

        # Ticker to company name mapping
        self.tickers = {
            'TSLA': "Tesla",
            'AMZN': "Amazon",
            'AAPL': "Apple",
            'META': "Meta",
            "NFLX": "Netflix"
        }

        self.ticker = current_ticker

        self.co = cohere.Client(cohere_key)
        self.agg_text = {}

        # Configure logging
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(levelname)s - %(message)s",
            handlers=[
                logging.StreamHandler(),
                logging.FileHandler("finnhub_websocket.log"),
            ],
        )

    def fetch_news(self, ticker):
        """
        Fetch latest news headlines for the ticker or company name.
        """
        company_name = self.tickers[self.ticker]
        now = datetime.utcnow()
        thirty_minutes_ago = now - timedelta(minutes=30)
        now_str = now.strftime('%Y-%m-%dT%H:%M:%S')
        thirty_minutes_ago_str = thirty_minutes_ago.strftime('%Y-%m-%dT%H:%M:%S')

        # Fetch articles from the News API
        articles = self.news_api.get_everything(
            q=f"{ticker} OR {company_name}",
            from_param=thirty_minutes_ago_str,
            to=now_str,
            language="en",
            sort_by="publishedAt",
            page=5
        )

        new_articles = []
        for article in articles.get("articles", []):
            title = article["title"]
            published_at = datetime.strptime(article["publishedAt"], '%Y-%m-%dT%H:%M:%SZ')

            # Check if the article is within the 30-minute window and not in cache
            if title not in self.news_cache and published_at >= thirty_minutes_ago:
                self.news_cache.add(title)
                new_articles.append({"source": "news", "text": title, "ticker": ticker})

        return new_articles

    def fetch_reddit(self, ticker):
        """
        Fetch Reddit posts containing the ticker from r/wallstreetbets.
        """
        company_name = self.tickers[self.ticker]
        reddit_posts = []
        current_time_utc = datetime.utcnow().replace(tzinfo=pytz.UTC)  # Ensure current time is in UTC

        # Ensure the cache contains tuples (post_id, timestamp)
        if self.reddit_cache and not all(isinstance(item, tuple) and len(item) == 2 for item in self.reddit_cache):
            self.reddit_cache = set()

        # Remove stale entries older than 10 minutes
        self.reddit_cache = {
            (post_id, timestamp) for post_id, timestamp in self.reddit_cache
            if timestamp > current_time_utc - timedelta(minutes=10)
        }

        for post in self.reddit.subreddit("wallstreetbets").new(limit=50):
            text = post.selftext.lower()  # Ensure case-insensitive matching
            post_time_utc = datetime.fromtimestamp(post.created_utc, pytz.UTC)  # Convert post time to UTC

            # Check cache and if the ticker/company name appears in the post text
            if (post.id, post_time_utc) not in self.reddit_cache and (
                    ticker.lower() in text or company_name.lower() in text):
                reddit_posts.append({"source": "reddit", "text": text, "ticker": ticker})
                self.reddit_cache.add((post.id, post_time_utc))  # Add to cache

        return reddit_posts

    def fetch_combined_data(self):
        """
        Fetch data from news and Reddit for all tickers.
        """
        combined_data = []
        ticker = self.ticker
        # Fetch news and Reddit data for each ticker
        news_data = self.fetch_news(ticker)
        reddit_data = self.fetch_reddit(ticker)
        combined_data.extend(news_data + reddit_data)

        return combined_data

    def summarize_text(self, text):
        """
        Summarizes the text using Cohere's API.
        """
        text_length = len(text)
        logging.info(f"Attempting to summarize text of length {text_length} characters.")

        # Log the text (truncated if very long)
        max_preview_len = 500
        preview_text = text[:max_preview_len] + ("..." if text_length > max_preview_len else "")
        logging.debug(f"Text to summarize (preview): {preview_text}")

        if text_length < 250:
            logging.warning("Text is shorter than 250 characters, skipping summarization and returning original text.")
            return text

        try:
            response = self.co.summarize(
                text=text,
                length="short",
                format="paragraph",
                model="summarize-xlarge"
            )
            logging.info("Received summary from Cohere successfully.")
            logging.debug(f"Summary result: {response.summary}")
            return response.summary
    
        except Exception as e:
            logging.error(f"Error while summarizing text: {str(e)}")
            return text

    def process_combined_data_with_summary(self):
        """
        Processes the data fetched from the pipeline to include time, aggregated texts, and summaries.
        """
        try:
            # Define EST timezone
            est = pytz.timezone("America/New_York")

            # Fetch combined data
            raw_data = self.fetch_combined_data()

            # Ensure raw_data is not empty
            if not raw_data:
                logging.warning("No data fetched from pipeline.")
                # Set default blank texts for all tickers
                for ticker in self.tickers:
                    self.agg_text[ticker] = "No data available."
                return

            # Add current timestamp in EST
            for entry in raw_data:
                entry["time"] = datetime.utcnow().replace(tzinfo=pytz.UTC).astimezone(est).strftime("%Y-%m-%d %H:%M:%S")

            # Convert raw data to a DataFrame
            df = pd.DataFrame(raw_data)

            # Ensure the DataFrame has the required columns
            if "ticker" not in df.columns or "text" not in df.columns:
                logging.error("Missing required columns in fetched data.")
                return

            # Aggregate texts by ticker
            aggregated_df = df.groupby("ticker").agg({
                "time": "first",  # Use the first timestamp
                "text": lambda texts: " ".join(texts)  # Concatenate all texts for each ticker
            }).reset_index()

            # Debug log each ticker’s aggregated text
            for idx, row in aggregated_df.iterrows():
                t = row["ticker"]
                txt = row["text"]
                logging.debug(f"Aggregated text for {t}: {txt}")

            # Summarize aggregated texts
            aggregated_df["summary"] = aggregated_df["text"].apply(self.summarize_text)

            # Update self.agg_text with summarized data
            for ticker in self.tickers:
                filtered_df = aggregated_df[aggregated_df["ticker"] == ticker]
                if not filtered_df.empty:
                    self.agg_text[ticker] = filtered_df["summary"].values[0]
                else:
                    # Default to blank text if no data is available for this ticker
                    self.agg_text[ticker] = ""

            logging.info("Aggregated and summarized text data successfully.")

        except Exception as e:
            logging.error(f"Error in processing combined data with summary: {str(e)}")

    def run_periodically(self):
        """
        Periodically process combined data and update summaries at the start of every minute.
        """
        while True:
            # Wait until the next minute starts
            current_time = datetime.now()
            sleep_seconds = 60 - current_time.second
            time.sleep(sleep_seconds)

            try:
                # Process combined data with summaries
                self.process_combined_data_with_summary()
                logging.info(f"Text aggregation and summarization completed at {datetime.now()}")
            except Exception as e:
                logging.error(f"Error during periodic text aggregation: {str(e)}")
