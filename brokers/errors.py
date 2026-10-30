"""Normalized transport failure, retaining provider request evidence."""
from typing import Any, Dict, Optional

class BrokerAPIError(ValueError):
    def __init__(self, message: str, api_logs: Optional[list[Dict[str, Any]]] = None, *, status_code=None, retry_after_seconds=None) -> None:
        super().__init__(message)
        self.api_logs = api_logs or []
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds

EtoroAPIError = BrokerAPIError  # Compatibility for historical imports.


class BrokerPreflightError(BrokerAPIError):
    """An eligibility/read check failed before any order request was sent."""
    request_sent = False
