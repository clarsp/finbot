"""Provider boundaries for research and brokerage integrations.

Real provider adapters are intentionally not implemented or enabled. Finbot stays
on deterministic local research data and paper-only accounting until those adapters
are implemented and explicitly selected.
"""

import os
from dataclasses import dataclass
from typing import Any, Dict, Protocol


@dataclass(frozen=True)
class IntegrationSettings:
    research_provider: str
    news_provider: str
    news_api_base_url: str
    news_api_key: str
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
            research_provider=os.getenv("FINBOT_RESEARCH_PROVIDER", "dummy").lower(),
            news_provider=os.getenv("FINBOT_NEWS_PROVIDER", "dummy").lower(),
            news_api_base_url=os.getenv("NEWS_API_BASE_URL", ""),
            news_api_key=os.getenv("NEWS_API_KEY", ""),
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
    async def fetch_news(self, subject: Dict[str, Any]) -> Dict[str, Any]: ...


class BrokerAdapter(Protocol):
    def submit_order(self, order: Dict[str, Any]) -> Dict[str, Any]: ...

    def get_positions(self) -> Dict[str, Any]: ...


class OpenAIResearchAdapter:
    def __init__(self, settings: IntegrationSettings) -> None:
        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required when the OpenAI provider is selected.")
        self.settings = settings

    async def research(self, subject: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("Implement and validate the research adapter before enabling it.")


class InteractiveBrokersAdapter:
    def __init__(self, settings: IntegrationSettings) -> None:
        if not settings.ibkr_paper_trading:
            raise ValueError("Live Interactive Brokers trading is deliberately disabled in this scaffold.")
        self.settings = settings

    def submit_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("Implement and validate the IBKR paper adapter before enabling it.")

    def get_positions(self) -> Dict[str, Any]:
        raise NotImplementedError("Implement IBKR position reconciliation before enabling it.")
