"""Research and broker provider boundaries for the local paper-trading MVP."""

import asyncio
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Protocol

import yfinance as yf
import yfscreen as yfs
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IntegrationSettings:
    research_provider: str
    news_provider: str
    news_api_base_url: str
    news_api_key: str
    news_interval_seconds: int
    news_symbol_limit: int
    news_fallback_tickers: tuple[str, ...]
    openai_base_url: str
    openai_api_key: str
    openai_model: str
    broker_provider: str
    ibkr_host: str
    ibkr_port: int
    ibkr_client_id: int
    ibkr_account_id: str
    ibkr_paper_trading: bool

    @classmethod
    def from_environment(cls) -> "IntegrationSettings":
        return cls(
            research_provider=os.getenv("FINBOT_RESEARCH_PROVIDER", "yfscreen").lower(),
            news_provider=os.getenv("FINBOT_NEWS_PROVIDER", "yfinance").lower(),
            news_api_base_url=os.getenv("NEWS_API_BASE_URL", ""),
            news_api_key=os.getenv("NEWS_API_KEY", ""),
            news_interval_seconds=max(60, int(os.getenv("FINBOT_NEWS_INTERVAL_SECONDS", "3600"))),
            news_symbol_limit=max(1, int(os.getenv("FINBOT_NEWS_SYMBOL_LIMIT", "8"))),
            news_fallback_tickers=tuple(
                ticker.strip().upper()
                for ticker in os.getenv("FINBOT_NEWS_TICKERS", "AAPL,MSFT,NVDA,AMD,META,XOM").split(",")
                if ticker.strip()
            ),
            openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            broker_provider=os.getenv("FINBOT_BROKER_PROVIDER", "dummy").lower(),
            ibkr_host=os.getenv("IBKR_HOST", "127.0.0.1"),
            ibkr_port=int(os.getenv("IBKR_PORT", "7497")),
            ibkr_client_id=int(os.getenv("IBKR_CLIENT_ID", "1")),
            ibkr_account_id=os.getenv("IBKR_ACCOUNT_ID", ""),
            ibkr_paper_trading=os.getenv("IBKR_PAPER_TRADING", "true").lower() in {"1", "true", "yes", "on"},
        )


class ResearchProvider(Protocol):
    async def research(self, subject: Dict[str, Any]) -> Dict[str, Any]: ...


class NewsProvider(Protocol):
    async def fetch_news(self, subject: Dict[str, Any] | None = None) -> list[Dict[str, Any]]: ...


class YahooFinanceNewsProvider:
    def __init__(self, settings: IntegrationSettings) -> None:
        self.settings = settings

    async def fetch_news(self, subject: Dict[str, Any] | None = None) -> list[Dict[str, Any]]:
        tickers = await asyncio.to_thread(self._screen_tickers)
        if not tickers:
            tickers = list(self.settings.news_fallback_tickers)
        tickers = tickers[: self.settings.news_symbol_limit]
        return await asyncio.to_thread(self._fetch_ticker_news, tickers)

    def _screen_tickers(self) -> list[str]:
        try:
            filters = [["eq", ["region", "us"]], ["gt", ["dayvolume", 1_000_000]]]
            query = yfs.create_query(filters=filters)
            payload = yfs.create_payload("equity", query, size=self.settings.news_symbol_limit)
            frame = yfs.get_data(payload)
            if frame is None or frame.empty:
                return []
            symbol_column = next((name for name in ("ticker", "symbol") if name in frame.columns), None)
            if not symbol_column:
                logger.warning("yfscreen response did not contain a ticker column")
                return []
            return list(dict.fromkeys(frame[symbol_column].dropna().astype(str).str.upper().tolist()))
        except Exception:
            logger.exception("yfscreen symbol screening failed; using configured Yahoo tickers")
            return []

    @staticmethod
    def _fetch_ticker_news(tickers: list[str]) -> list[Dict[str, Any]]:
        articles: Dict[str, Dict[str, Any]] = {}
        for ticker in tickers:
            try:
                search_results = yf.Search(
                    ticker, max_results=8, news_count=5, lists_count=0
                ).news or []
                if not search_results:
                    search_results = yf.Ticker(ticker).news or []
                for item in search_results:
                    content = item.get("content") or item
                    title = content.get("title") or item.get("title")
                    if not title:
                        continue
                    url_data = (
                        content.get("canonicalUrl")
                        or content.get("clickThroughUrl")
                        or content.get("link")
                        or item.get("link")
                        or {}
                    )
                    url = url_data.get("url", "") if isinstance(url_data, dict) else str(url_data)
                    published_at = content.get("pubDate") or content.get("displayTime")
                    if not published_at and content.get("providerPublishTime"):
                        published_at = datetime.fromtimestamp(
                            content["providerPublishTime"], timezone.utc
                        ).isoformat()
                    source_data = content.get("provider") or item.get("publisher") or "Yahoo Finance"
                    source = source_data.get("displayName", "Yahoo Finance") if isinstance(source_data, dict) else str(source_data)
                    key = url or str(content.get("id") or hashlib.sha256(f"{ticker}:{title}".encode()).hexdigest())
                    articles[key] = {
                        "article_key": key,
                        "ticker": ticker,
                        "title": str(title),
                        "summary": str(content.get("summary") or content.get("description") or ""),
                        "source": source,
                        "url": url,
                        "published_at": published_at,
                        "fetched_at": datetime.now(timezone.utc).isoformat(),
                    }
            except Exception:
                logger.exception("Yahoo Finance news fetch failed for %s", ticker)
        return list(articles.values())


class BrokerAdapter(Protocol):
    def submit_order(self, order: Dict[str, Any]) -> Dict[str, Any]: ...

    def get_positions(self) -> Dict[str, Any]: ...


class OpenAIResearchAdapter:
    def __init__(self, settings: IntegrationSettings) -> None:
        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required when the OpenAI provider is selected.")
        self.settings = settings

    async def research(self, subject: Dict[str, Any]) -> Dict[str, Any]:
        headlines = [
            {
                "ticker": str(item.get("ticker", "")),
                "title": str(item.get("title", ""))[:400],
                "summary": str(item.get("summary", ""))[:1000],
                "source": str(item.get("source", "")),
                "published_at": item.get("published_at"),
            }
            for item in subject.get("headlines", [])[:8]
        ]
        research_input = {
            "company_name": subject.get("company_name", ""),
            "ticker": subject.get("ticker", ""),
            "sector": subject.get("sector", ""),
            "dummy_signal": subject.get("dummy_signal", "neutral"),
            "dummy_confidence": subject.get("dummy_confidence", 0.0),
            "background": str(subject.get("background", ""))[:1000],
            "headlines": headlines,
        }
        async with AsyncOpenAI(
            api_key=self.settings.openai_api_key,
            base_url=self.settings.openai_base_url,
            timeout=45.0,
            max_retries=1,
        ) as client:
            response = await client.chat.completions.create(
                model=self.settings.openai_model,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Analyze only the supplied company context and headlines. Do not invent facts. "
                            "Return one JSON object with signal (bullish, bearish, or neutral), confidence "
                            "(number from 0 to 1), catalyst (short label), and summary (brief evidence-based text). "
                            "This is research context only, not an order or financial advice."
                        ),
                    },
                    {"role": "user", "content": json.dumps(research_input, ensure_ascii=True)},
                ],
            )

        content = response.choices[0].message.content
        if not content:
            raise ValueError("OpenAI returned an empty research response.")
        try:
            result = json.loads(content)
        except json.JSONDecodeError as error:
            raise ValueError("OpenAI research response was not valid JSON.") from error

        signal = str(result.get("signal", "")).lower()
        if signal not in {"bullish", "bearish", "neutral"}:
            raise ValueError("OpenAI returned an unsupported signal; expected bullish, bearish, or neutral.")
        confidence = float(result.get("confidence", -1))
        if not 0 <= confidence <= 1:
            raise ValueError("OpenAI confidence must be between 0 and 1.")
        return {
            "signal": signal,
            "confidence": confidence,
            "catalyst": str(result.get("catalyst", "news_analysis"))[:100],
            "summary": str(result.get("summary", ""))[:1000],
            "provider": "openai",
            "model": self.settings.openai_model,
        }


class InteractiveBrokersAdapter:
    def __init__(self, settings: IntegrationSettings) -> None:
        if not settings.ibkr_paper_trading:
            raise ValueError("Live Interactive Brokers trading is deliberately disabled in this scaffold.")
        self.settings = settings

    def submit_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("Implement and validate the IBKR paper adapter before enabling it.")

    def get_positions(self) -> Dict[str, Any]:
        raise NotImplementedError("Implement IBKR position reconciliation before enabling it.")
