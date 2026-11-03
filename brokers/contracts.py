"""Broker-neutral execution boundary; adapters own endpoint and payload details."""
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class BrokerAdapter(Protocol):
    def resolve_instrument(self, symbol: str) -> dict[str, Any]: ...
    def get_quote(self, symbol: str) -> dict[str, Any]: ...
    def get_rate(self, instrument_id: int) -> dict[str, Any]: ...
    def get_positions(self) -> dict[str, Any]: ...
    def get_account_snapshot(self) -> dict[str, Any]: ...
    def get_portfolio_balance(self, source=None) -> dict[str, Any]: ...
    def get_closed_positions(self, min_date=None) -> dict[str, Any]: ...
    def submit_order(self, order: dict[str, Any]) -> dict[str, Any]: ...
    def lookup_order(self, *, order_id=None, reference_id=None) -> dict[str, Any]: ...
    def lookup_close_order(self, order_id) -> dict[str, Any]: ...
    def cancel_pending_order(self, order_id, *, request_id=None) -> dict[str, Any]: ...
    def modify_position(self, position_id, stop, target, *, request_id=None, stop_type="fixed") -> dict[str, Any]: ...
    def close_position(self, position_id, instrument_id=None, *, request_id=None) -> dict[str, Any]: ...
    def estimate_order_costs(self, symbol, side, units, leverage) -> dict[str, Any]: ...


def instrument_blockers(instrument, side=None):
    """Missing state is unknown; a closed exchange is not a delisted security."""
    if not instrument.get('resolved') and 'resolved' in instrument:
        return [instrument.get('reason') or 'Broker instrument identity could not be resolved.']
    if 'resolved' not in instrument:  # Older persisted fixtures and evidence.
        return [] if instrument.get('tradeable') and instrument.get('supported_asset') else ['Broker instrument eligibility has not been verified.']
    reasons = []
    if not instrument.get('supported_asset'):
        reasons.append('Instrument asset class is unsupported by this adapter.')
    if instrument.get('delisted') is True:
        reasons.append('Broker reports the instrument is delisted.')
    if instrument.get('platform_active') is False:
        reasons.append('Broker reports the instrument is inactive on its platform; eligibility requires verification.')
    availability = instrument.get('execution_availability') or {}
    reconciled = (availability.get('source') == 'etoro_demo_eligibility_and_quote'
                  and availability.get('verified') is True)
    if instrument.get('exchange_open') is False and not reconciled:
        session = availability.get('session') or {}
        if session.get('is_regular_session') and availability.get('reason'):
            reasons.append('Broker execution availability unverified: ' + availability['reason'])
        else:
            reasons.append('Broker reports the exchange is closed.')
    if instrument.get('currently_tradeable') is not True:
        reasons.append('Broker reports the instrument is not currently tradable.' if instrument.get('currently_tradeable') is False else 'Current broker tradability is unknown.')
    if side == 'LONG' and instrument.get('buy_enabled') is False:
        reasons.append('Broker has disabled buy orders for this instrument.')
    if side == 'SHORT' and instrument.get('short_enabled') is False:
        reasons.append('Broker has disabled short orders for this instrument.')
    return reasons


def validate_runtime_binding(conn, provider):
    """Do not reinterpret persisted broker IDs after changing the provider setting."""
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='trading_profiles'").fetchone()
    if not exists:
        return  # Legacy fixtures; normal startup initializes the profile schema.
    roles_exist = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='platform_broker_roles'").fetchone()
    reference = conn.execute("SELECT broker_account_id FROM platform_broker_roles WHERE role='admin_reference'").fetchone() if roles_exist else None
    if reference:
        row = conn.execute("SELECT c.provider FROM broker_accounts a JOIN broker_connections c ON c.id=a.connection_id WHERE a.id=?",(reference[0],)).fetchone()
        if row is None or str(row[0]).lower()!=str(provider).lower():
            raise ValueError('Admin/reference broker runtime is pinned to its original provider; new-entry destination planning does not change it.')
        return
    row = conn.execute("""SELECT c.provider FROM trading_profiles p
        JOIN broker_accounts a ON a.id=p.broker_account_id
        JOIN broker_connections c ON c.id=a.connection_id
        WHERE p.id=1 AND p.status='active'""").fetchone()
    if row is None or str(row[0]).lower() != str(provider).lower():
        raise ValueError('Configured broker does not match the active trading profile. Explicit account migration is required; no broker request was sent.')
