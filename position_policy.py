"""Position Manager policies: conservative profit exits and deduplicated research work."""
import json
import math
from datetime import datetime, timezone

def stamp(value):
    d=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)

def profit_exit_evidence(trade, position, rate, rules, now=None):
    now=now or datetime.now(timezone.utc)
    result={'allowed':False,'basis':'Executable bid/ask; actual units; conservative costs; no second leverage multiplier'}
    try:
        if trade['side'] not in ('LONG','SHORT') or position.get('side')!=trade['side']:return result
        if str(position.get('position_id')).removesuffix('.0')!=str(trade.get('broker_position_id')).removesuffix('.0'):return result
        entry=float(position['open_rate']);units=abs(float(position['units']));bid=float(rate['bid']);ask=float(rate['ask'])
        pnl=float(position['unrealized_pnl']);fees=max(0,float(position['fees']))
        if not all(math.isfinite(v) for v in (entry,units,bid,ask,pnl,fees)) or min(entry,units,bid)<=0 or ask<bid:return result
        age=(now-stamp(position.get('opened_at') or trade['opened_at'])).total_seconds()/3600
        if age<0:return result
        policy=json.loads(trade.get('policy_json') or '{}');costs=policy.get('cost_assumptions') or {}
        bps=max(10,float(costs.get('roundtrip_bps',10)));annual=max(8,float(costs.get('financing_annual_pct',8)))
        margin=float(position['amount']);notional=units*entry
        if not all(math.isfinite(v) and v>=0 for v in (bps,annual,margin)):return result
        gross=(bid-entry)*units if trade['side']=='LONG' else (entry-ask)*units
        estimated_costs=fees+notional*(bps/10000+annual/100*age/8760)
        reserve=max(2*(ask-bid)*units,notional*float(rules.get('profit_exit_slippage_bps',10))/10000)
        floor=max(float(rules.get('profit_exit_min_net_usd',1)),margin*float(rules.get('profit_exit_min_margin_pct',.5))/100)
        conservative_net=min(gross-estimated_costs,pnl-estimated_costs)-reserve
        if not all(math.isfinite(v) for v in (notional,gross,estimated_costs,reserve,floor,conservative_net)) or min(reserve,floor)<0:return result
        result.update(gross_profit=gross,estimated_costs=estimated_costs,slippage_reserve=reserve,
                      conservative_net_profit=conservative_net,required_net_profit=floor,quote_checked_at=now.isoformat(),
                      allowed=bool(rules.get('profit_review_exits_enabled',True)) and conservative_net>floor)
    except (ValueError,TypeError,KeyError,OverflowError):pass
    return result

def ensure_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS held_research_requests (
        symbol TEXT PRIMARY KEY,requested_at TEXT NOT NULL,source_research_at TEXT,
        next_attempt_at REAL NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0)''')

def request_research(conn,symbol,source_at,now=None):
    now=now or datetime.now(timezone.utc)
    return conn.execute('INSERT OR IGNORE INTO held_research_requests(symbol,requested_at,source_research_at) VALUES (?,?,?)',
        (symbol,now.isoformat(),source_at)).rowcount

def claim_research(conn,limit,now=None):
    now=now or datetime.now(timezone.utc)
    conn.execute('BEGIN IMMEDIATE')
    rows=conn.execute('SELECT symbol FROM held_research_requests WHERE next_attempt_at<=? ORDER BY requested_at LIMIT ?', (now.timestamp(),limit)).fetchall()
    for r in rows:
        conn.execute('UPDATE held_research_requests SET next_attempt_at=?,attempts=attempts+1 WHERE symbol=?', (now.timestamp()+3600,r['symbol']))
    conn.commit()
    return [r['symbol'] for r in rows]


TRAILING_BASIS = 'entry_atr_v1'
TRAILING_MODES = ('scheduled', 'broker')
ARMING_BASES = ('amount', 'atr')
LEGACY_TRAILING_KEYS = {'profit_trailing_prime_min_usd', 'profit_trailing_major_min_usd',
                        'profit_trailing_prime_drop_usd', 'profit_trailing_major_drop_usd'}
TRAILING_DEFAULTS = {
    'profit_trailing_arming_basis': 'amount',
    'profit_trailing_prime_arm_usd': 10.0,
    'profit_trailing_major_arm_usd': 20.0,
    'profit_trailing_interval_minutes': 30.0,
    'profit_trailing_prime_arm_atr': 0.5,
    'profit_trailing_major_arm_atr': 1.0,
    'profit_trailing_prime_retreat_atr': 0.15,
    'profit_trailing_major_retreat_atr': 0.25,
}


def trailing_rules(rules=None):
    values = {k: (rules or {}).get(k, v) for k, v in TRAILING_DEFAULTS.items()}
    if values['profit_trailing_arming_basis'] not in ARMING_BASES:
        raise ValueError('Arming basis must be amount or atr.')
    for key, value in values.items():
        if key=='profit_trailing_arming_basis':continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f'{key} must be finite and positive.')
    if not 1 <= values['profit_trailing_interval_minutes'] <= 1440:
        raise ValueError('Profit trailing interval must be between 1 and 1440 minutes.')
    if values['profit_trailing_major_arm_atr'] <= values['profit_trailing_prime_arm_atr']:
        raise ValueError('Major arming threshold must exceed the prime threshold.')
    if values['profit_trailing_major_arm_usd'] <= values['profit_trailing_prime_arm_usd']:
        raise ValueError('Major amount threshold must exceed the prime threshold.')
    for tier in ('prime', 'major'):
        if values['profit_trailing_arming_basis']=='atr' and values[f'profit_trailing_{tier}_retreat_atr'] >= values[f'profit_trailing_{tier}_arm_atr']:
            raise ValueError(f'{tier.title()} retreat must be smaller than its arming threshold.')
    return values


def ensure_price_trailing_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS price_trailing (
        trade_id INTEGER PRIMARY KEY REFERENCES trades(id), position_id TEXT NOT NULL,
        side TEXT NOT NULL, entry_price REAL NOT NULL, entry_atr REAL NOT NULL,
        best_price REAL NOT NULL, started_at REAL NOT NULL, checked_at REAL NOT NULL,
        evidence TEXT NOT NULL)""")


def trailing_evidence(trade, position, rate, best_price=None, rules=None, *, entry_atr=None, entry_price=None, now=None, previous_evidence=None):
    """Selectable cash/ATR arming with ATR retreats on executable bid/ask."""
    side = trade.get('side')
    if not trade.get('broker_position_id') or side not in ('LONG', 'SHORT') or str(position.get('position_id')).removesuffix('.0') != str(trade['broker_position_id']).removesuffix('.0') or position.get('side') != side:
        raise ValueError('Trailing position identity mismatch')
    policy = json.loads(trade.get('policy_json') or '{}')
    entry = float(entry_price if entry_price is not None else position['open_rate'])
    atr = float(entry_atr if entry_atr is not None else policy['atr14'])
    actual_entry = float(position['open_rate'])
    bid, ask = float(rate['bid']), float(rate['ask'])
    now = now or datetime.now(timezone.utc)
    quote_time = rate.get('timestamp') or rate.get('lastUpdated') or rate.get('fetched_at')
    if quote_time and not -5 <= (now-stamp(quote_time)).total_seconds() <= 60:
        raise ValueError('Broker quote timestamp is stale or invalid')
    if 'delay' in str(rate.get('quoteType','')).lower():
        raise ValueError('Broker quote is delayed')
    if rate.get('available') is False or rate.get('executable') is False:
        raise ValueError('Executable broker quote unavailable')
    if not all(math.isfinite(v) and v > 0 for v in (entry, atr, actual_entry, bid, ask)) or ask < bid:
        raise ValueError('Positive finite entry, entry ATR and bid/ask are required')
    if abs(actual_entry-entry) > max(1e-6, abs(entry)*1e-8):
        raise ValueError('The position entry changed; trailing basis needs review')
    rules=trailing_rules(rules)
    arming_basis=rules['profit_trailing_arming_basis']
    prior=previous_evidence or {}
    same_basis=prior.get('arming_basis','atr')==arming_basis
    # New arming basis starts fresh observations; frozen entry/ATR are retained.
    if previous_evidence is not None and not same_basis:best_price=None
    price = bid if side == 'LONG' else ask
    previous = entry if best_price is None else float(best_price)
    if not math.isfinite(previous) or previous <= 0:
        raise ValueError('Invalid saved best price')
    best = max(entry, price, previous) if side == 'LONG' else min(entry, price, previous)
    sign = 1 if side == 'LONG' else -1
    peak = sign*(best-entry)/atr
    progress = sign*(price-entry)/atr
    retreat = sign*(best-price)/atr
    pnl = position.get('unrealized_pnl')
    pnl = float(pnl) if isinstance(pnl, (int,float)) and not isinstance(pnl,bool) and math.isfinite(pnl) else None
    cash_peak=None
    if arming_basis=='amount':
        saved=prior.get('peak_profit_usd') if same_basis else None
        saved=float(saved) if isinstance(saved,(int,float)) and not isinstance(saved,bool) and math.isfinite(saved) else 0.0
        cash_peak=max(0.0,saved,pnl if pnl is not None else 0.0)
        tier=('major' if cash_peak>rules['profit_trailing_major_arm_usd']+1e-9 else
              'prime' if cash_peak>rules['profit_trailing_prime_arm_usd']+1e-9 else 'tracking')
    else:
        tier = ('major' if peak > rules['profit_trailing_major_arm_atr']+1e-9 else
                'prime' if peak > rules['profit_trailing_prime_arm_atr']+1e-9 else 'tracking')
    allowed_retreat = rules[f'profit_trailing_{tier}_retreat_atr'] if tier != 'tracking' else None
    trigger = best-sign*allowed_retreat*atr if allowed_retreat is not None else None
    return dict(basis=TRAILING_BASIS, arming_basis=arming_basis, peak_profit_usd=cash_peak,
        arming_available=arming_basis=='atr' or pnl is not None or (cash_peak or 0)>0,
        side=side, entry_price=entry, entry_atr=atr,
        current_price=price, best_price=best, peak_movement_atr=peak,
        current_movement_atr=progress, retreat_atr=retreat, allowed_retreat_atr=allowed_retreat,
        trigger_price=trigger, tier=tier, rules=rules, profit_usd=pnl,
        quote_basis='bid' if side == 'LONG' else 'ask',quote_checked_at=now.isoformat(),
        close_due=allowed_retreat is not None and retreat+1e-9 >= allowed_retreat)


def observe_trailing_profit(conn, trade, position, rate, now, rules=None, *, peak_only=False):
    """Persist scheduled price observations; legacy dollar peaks remain historical."""
    ensure_price_trailing_schema(conn)
    row = conn.execute('SELECT position_id,side,entry_price,entry_atr,best_price,started_at,checked_at,evidence FROM price_trailing WHERE trade_id=?', (trade['id'],)).fetchone()
    pid = str(trade.get('broker_position_id')).removesuffix('.0')
    if row and (row[0] != pid or row[1] != trade.get('side')):
        raise ValueError('Trailing position owner changed')
    rules = trailing_rules(rules)
    if not peak_only and row and now-float(row[6]) < rules['profit_trailing_interval_minutes']*60:
        return None
    evidence = trailing_evidence(trade, position, rate, row[4] if row else None, rules,
        entry_price=row[2] if row else None, entry_atr=row[3] if row else None, now=datetime.fromtimestamp(now,timezone.utc),
        previous_evidence=json.loads(row[7]) if row else None)
    evidence['started_at'] = row[5] if row else now
    evidence['quote_checked_at'] = datetime.fromtimestamp(now,timezone.utc).isoformat()
    conn.execute("""INSERT INTO price_trailing VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(trade_id) DO UPDATE SET best_price=excluded.best_price,
        checked_at=excluded.checked_at,evidence=excluded.evidence""",
        (trade['id'],pid,trade['side'],evidence['entry_price'],evidence['entry_atr'],
         evidence['best_price'],evidence['started_at'],row[6] if peak_only and row else now,json.dumps(evidence)))
    conn.commit()
    return evidence


def native_trailing_intent(evidence, position):
    """Protective native stop intent; never widen a verified existing broker stop."""
    if evidence['tier']=='tracking' or evidence['close_due']:
        return None
    side=evidence['side'];price=evidence['current_price']
    stop=float(evidence['trigger_price'])
    actual=position.get('stop_loss_price')
    if actual is not None:
        actual=float(actual)
        if not math.isfinite(actual) or actual<=0:raise ValueError('Invalid broker stop')
        stop=max(stop,actual) if side=='LONG' else min(stop,actual)
    if not math.isfinite(stop) or stop<=0 or (stop>=price if side=='LONG' else stop<=price):
        raise ValueError('Native trailing stop lacks price clearance')
    return dict(stop_loss_price=stop,stop_loss_type='trailing',native_atr=True,
        profit_trailing={**evidence,'requested_distance_atr':evidence['allowed_retreat_atr'],
            'initial_effective_distance_atr':abs(price-stop)/evidence['entry_atr']})
