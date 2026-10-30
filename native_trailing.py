"""Optional broker-native ATR protection, reconciled by the existing Position Manager.

Scheduled observations continue; only the explicit broker mode arms native stops.
No worker, broker factory, or independently submitted close is created here.
"""
import json
import math
import position_policy


def adopt_verified_stop(connect, trade, position):
    """Retain native ratchets instead of repairing them back to an older fixed SL."""
    if (trade.get('side') not in {'LONG','SHORT'} or position.get('stop_loss_type')!='trailing' or position.get('side')!=trade.get('side') or
        str(position.get('position_id')).removesuffix('.0')!=str(trade.get('broker_position_id')).removesuffix('.0')):
        return False
    actual=position.get('stop_loss_price');saved=trade.get('stop_loss_price')
    if actual is None or not math.isfinite(float(actual)) or float(actual)<=0:return False
    tighter=saved is None or (float(actual)>float(saved) if trade['side']=='LONG' else float(actual)<float(saved))
    if not tighter:return False
    c=connect()
    try:
        c.execute('UPDATE trades SET stop_loss_price=? WHERE id=?',(actual,trade['id']));c.commit()
    finally:c.close()
    trade['stop_loss_price']=actual
    return True


def confirmed_protection(evidence, position):
    actual=position.get('stop_loss_price')
    if not (position.get('stop_loss_type')=='trailing' and isinstance(actual,(int,float))
            and not isinstance(actual,bool) and math.isfinite(actual) and actual>0):return False
    trigger=evidence.get('trigger_price')
    if trigger is None:return True
    tolerance=max(.01,abs(trigger)*.0001)
    return actual>=trigger-tolerance if evidence['side']=='LONG' else actual<=trigger+tolerance


async def reconcile(recovery, trade, position, adapter, evidence):
    """Fast arming, plus continued status verification without resetting active TSL."""
    confirmed=confirmed_protection(evidence,position)
    trigger=evidence.get('trigger_price')
    result={'confirmed':confirmed,'status':'broker_active' if confirmed else 'unarmed',
            'observed_stop':position.get('stop_loss_price'),'expected_trigger':trigger,'tier':evidence['tier']}
    if not recovery.rules.get('profit_trailing_enabled') or recovery.rules.get('profit_trailing_mode','scheduled')!='broker':
        return {**result,'status':'not_selected'}
    if evidence['tier']=='tracking':return result
    if evidence['close_due']:
        return {**result,'status':'broker_active_reconciliation' if confirmed else 'scheduled_fallback'}
    action=recovery.get(trade['id'],'protection')
    if action:return {**result,'status':'protection_pending','action_id':action['id']}
    if recovery.get(trade['id'],'close'):return {**result,'status':'close_pending'}
    if getattr(adapter,'supports_native_trailing',False) is not True:
        return {**result,'status':'unsupported_adapter'}
    if position.get('stop_loss_type')=='trailing':
        c=recovery.connect()
        try:
            row=c.execute("SELECT payload FROM broker_actions WHERE trade_id=? AND kind='protection' AND state='APPLIED' ORDER BY id DESC LIMIT 1",(trade['id'],)).fetchone()
        finally:c.close()
        prior=json.loads(row[0]) if row else {}
        previous=(prior.get('profit_trailing') or {}).get('requested_distance_atr')
        # A wider major distance must not relax an already secured prime stop.
        # Unknown/manual native distances are retained rather than overwritten.
        if previous is None or evidence['allowed_retreat_atr']>=float(previous)-1e-9:
            return {**result,'status':'broker_active_keep_tighter' if confirmed else 'broker_distance_unconfirmed','retained_distance_atr':previous}
    intent=position_policy.native_trailing_intent(evidence,position)
    if not intent:return result
    try:
        response=await recovery.request_protection(trade,position,adapter,intent['stop_loss_price'],
            trade.get('take_profit_price'),stop_type='trailing',native_evidence=intent['profit_trailing'])
        return {**result,'status':'update_pending','action_id':response.get('recovery_action_id')}
    except Exception as error:
        # Durable recovery owns ambiguous outcomes and prevents duplicate updates.
        return {**result,'status':'update_unconfirmed','error_type':type(error).__name__}
