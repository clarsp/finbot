"""Explicit S&P 500 instruments shared by research, price history and the broker."""
SP500={
 'SPX500':{'name':'S&P 500 Index (Non Expiry)','reference':'^GSPC','kind':'index','broker_id':27},
 'SPY':{'name':'SPDR S&P 500 ETF Trust','reference':'SPY','kind':'etf'},
 'VOO':{'name':'Vanguard S&P 500 ETF','reference':'VOO','kind':'etf'},
 'IVV':{'name':'iShares Core S&P 500 ETF','reference':'IVV','kind':'etf'},
}

def reference_symbol(symbol):
 symbol=symbol.upper()
 return SP500.get(symbol,{}).get('reference',{'USOIL':'CL=F'}.get(symbol,symbol))

def broker_symbol(symbol):return 'SPX500' if symbol.upper()=='^GSPC' else symbol.upper()

def sp500_index_supported(symbol,asset_class,instrument_id):
 return symbol.upper()=='SPX500' and asset_class.lower() in {'index','indices','equity index'} and instrument_id==27
