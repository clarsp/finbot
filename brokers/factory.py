"""Single adapter selection point; unsupported providers never fall back to another broker."""
from .contracts import BrokerAdapter

_FACTORIES = {}
_DEMO_READY = set()


def register_broker(provider, factory, *, demo_ready=False):
    name = str(provider).strip().lower()
    if not name or name in _FACTORIES or name == 'etoro':
        raise ValueError('Broker provider is empty or already registered')
    _FACTORIES[name] = factory
    if demo_ready:
        _DEMO_READY.add(name)


def create_broker_adapter(settings) -> BrokerAdapter:
    provider = str(settings.broker_provider).lower()
    if provider == 'etoro':
        from .etoro import EtoroBrokerAdapter
        return EtoroBrokerAdapter(settings)
    if provider in _FACTORIES:
        adapter = _FACTORIES[provider](settings)
        if not isinstance(adapter, BrokerAdapter):
            raise TypeError('Broker adapter does not implement the complete execution/recovery contract')
        return adapter
    raise ValueError(f'Broker provider {provider!r} has no validated execution adapter. No request was sent.')


def demo_execution_enabled(settings):
    """Provider activation is explicit; registration alone cannot enable trading."""
    if settings.broker_provider == 'etoro':
        return bool(settings.etoro_demo_trading)
    return settings.broker_provider in _DEMO_READY


def account_lease_name(settings):
    # Preserve the existing lock name for the single deployed account.
    if settings.broker_provider == 'etoro':
        return 'etoro_demo_account'
    return f'{settings.broker_provider}_demo_account'
