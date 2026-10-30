"""Verified research-to-eToro aliases, used only at the broker boundary.

Research tickers are never rewritten. An alias is an explicit identity assertion,
not a suffix heuristic or permission to trade a different asset. Add an alias
only after verifying the broker instrument ID, asset class and primary exchange.
"""
import json
from .parsing import _etoro_instrument_id

# Verified against the authenticated broker search on 29 October 2026.
# eToro CVX is a crypto asset; the NYSE Chevron equity is CVX.US / 1014.
ALIASES = {
    'CVX': {
        'company_name': 'Chevron',
        'broker_symbol': 'CVX.US',
        'instrument_id': 1014,
        'asset_classes': ('stocks', 'stock', 'equity', 'equities'),
        'exchanges': ('nyse', 'new york stock exchange'),
    },
}


def search_symbol(research_symbol):
    symbol=str(research_symbol).strip().upper()
    return ALIASES.get(symbol,{}).get('broker_symbol',symbol)


def select_instrument(research_symbol, items):
    symbol=str(research_symbol).strip().upper()
    expected=search_symbol(symbol)
    # Identical repeated rows are harmless; conflicting identities/statuses are not.
    matches={json.dumps(item,sort_keys=True):item for item in items if isinstance(item,dict)
             and str(item.get('internalSymbolFull') or item.get('symbol') or '').upper()==expected}
    if not matches:
        raise ValueError(f'eToro could not resolve instrument ID for {symbol}; expected broker symbol {expected}. No unverified alias was substituted.')
    if len(matches)!=1:
        raise ValueError(f'Ambiguous eToro identity for {symbol}: multiple matches for {expected}. Execution requires an explicit verified mapping.')
    item=next(iter(matches.values()))
    mapping=ALIASES.get(symbol)
    instrument_id=_etoro_instrument_id(item)
    if mapping:
        asset=str(item.get('internalAssetClassName') or item.get('assetClass') or item.get('assetType') or '').strip().casefold()
        exchange=str(item.get('internalExchangeName') or item.get('exchangeName') or '').strip().casefold()
        if (instrument_id!=mapping['instrument_id'] or asset not in mapping['asset_classes']
                or exchange not in mapping['exchanges']):
            raise ValueError(f'eToro identity mismatch for {symbol}: {expected} must be the verified {mapping["company_name"]} equity on NYSE (instrument {mapping["instrument_id"]}).')
    return {**item,'_finbot_resolution':{
        'research_symbol':symbol,'broker_symbol':expected,'instrument_id':instrument_id,
        'method':'verified_alias' if mapping else 'unique_exact_symbol',
        **({'company_name':mapping['company_name'],'expected_asset_class':'equity','expected_exchange':'NYSE'} if mapping else {}),
    }}
