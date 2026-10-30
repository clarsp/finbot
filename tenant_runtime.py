"""Immutable broker ownership and staged destinations; additional runtimes stay disabled."""
import sqlite3


def _execute_batch(conn, script):
    statement = ''
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ''
    if statement.strip():
        raise ValueError('Incomplete migration SQL')


def migrate(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='broker_actions'").fetchone():
        import broker_recovery
        broker_recovery.ensure_schema(conn)
    conn.execute('BEGIN IMMEDIATE')
    try:
        _migrate(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _migrate(conn):
    _execute_batch(conn, '''
    CREATE TABLE IF NOT EXISTS platform_broker_roles (
        role TEXT PRIMARY KEY CHECK(role='admin_reference'),broker_account_id INTEGER NOT NULL REFERENCES broker_accounts(id));
    CREATE TABLE IF NOT EXISTS profile_execution_routes (
        profile_id INTEGER PRIMARY KEY REFERENCES trading_profiles(id),
        active_account_id INTEGER NOT NULL REFERENCES broker_accounts(id),
        planned_account_id INTEGER REFERENCES broker_accounts(id),updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS trade_broker_ownership (
        trade_id INTEGER PRIMARY KEY,profile_id INTEGER NOT NULL,broker_account_id INTEGER NOT NULL,
        broker_connection_id INTEGER NOT NULL,provider TEXT NOT NULL,environment TEXT NOT NULL,
        bound_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS broker_action_ownership (
        action_id INTEGER PRIMARY KEY,trade_id INTEGER NOT NULL,broker_account_id INTEGER NOT NULL,
        broker_connection_id INTEGER NOT NULL,provider TEXT NOT NULL,environment TEXT NOT NULL,
        bound_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    ''')
    conn.execute('''INSERT OR IGNORE INTO profile_execution_routes(profile_id,active_account_id)
        SELECT id,broker_account_id FROM trading_profiles''')
    conn.execute('''INSERT OR IGNORE INTO platform_broker_roles
        SELECT 'admin_reference',a.id FROM trading_profiles p JOIN broker_accounts a ON a.id=p.broker_account_id
        JOIN broker_connections c ON c.id=a.connection_id WHERE p.id=1 AND c.provider='etoro' AND c.environment='demo' ''')
    conn.execute('''INSERT OR IGNORE INTO trade_broker_ownership
        (trade_id,profile_id,broker_account_id,broker_connection_id,provider,environment)
        SELECT t.id,t.trading_profile_id,r.active_account_id,a.connection_id,c.provider,c.environment
        FROM trades t JOIN profile_execution_routes r ON r.profile_id=t.trading_profile_id
        JOIN broker_accounts a ON a.id=r.active_account_id JOIN broker_connections c ON c.id=a.connection_id''')
    conn.execute('''INSERT OR IGNORE INTO broker_action_ownership
        (action_id,trade_id,broker_account_id,broker_connection_id,provider,environment)
        SELECT x.id,x.trade_id,o.broker_account_id,o.broker_connection_id,o.provider,o.environment
        FROM broker_actions x JOIN trade_broker_ownership o ON o.trade_id=x.trade_id''')
    if conn.execute('SELECT count(*) FROM trades t LEFT JOIN trade_broker_ownership o ON o.trade_id=t.id WHERE o.trade_id IS NULL').fetchone()[0]:
        raise ValueError('Cannot establish broker ownership for every trade')
    if conn.execute('SELECT count(*) FROM broker_actions x LEFT JOIN broker_action_ownership o ON o.action_id=x.id WHERE o.action_id IS NULL').fetchone()[0]:
        raise ValueError('Cannot establish broker ownership for every recovery action')
    # Ownership tombstones intentionally outlive analysis archival; restoring a lifecycle keeps its original account.
    _execute_batch(conn, '''
    CREATE TRIGGER IF NOT EXISTS pin_trade_broker AFTER INSERT ON trades BEGIN
      INSERT OR IGNORE INTO trade_broker_ownership(trade_id,profile_id,broker_account_id,broker_connection_id,provider,environment)
      SELECT NEW.id,NEW.trading_profile_id,r.active_account_id,a.connection_id,c.provider,c.environment
      FROM profile_execution_routes r JOIN broker_accounts a ON a.id=r.active_account_id
      JOIN broker_connections c ON c.id=a.connection_id WHERE r.profile_id=NEW.trading_profile_id;
      SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM trade_broker_ownership WHERE trade_id=NEW.id AND profile_id=NEW.trading_profile_id)
      THEN RAISE(ABORT,'Trade broker ownership could not be established') END;
    END;
    CREATE TRIGGER IF NOT EXISTS pin_recovery_broker AFTER INSERT ON broker_actions BEGIN
      INSERT INTO broker_action_ownership(action_id,trade_id,broker_account_id,broker_connection_id,provider,environment)
      SELECT NEW.id,NEW.trade_id,o.broker_account_id,o.broker_connection_id,o.provider,o.environment
      FROM trade_broker_ownership o WHERE o.trade_id=NEW.trade_id;
      SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM broker_action_ownership WHERE action_id=NEW.id)
      THEN RAISE(ABORT,'Recovery action has no broker owner') END;
    END;
    CREATE TRIGGER IF NOT EXISTS keep_trade_profile BEFORE UPDATE OF trading_profile_id ON trades
      WHEN NEW.trading_profile_id<>OLD.trading_profile_id BEGIN SELECT RAISE(ABORT,'Trade profile ownership is immutable'); END;
    CREATE TRIGGER IF NOT EXISTS keep_recovery_trade BEFORE UPDATE OF trade_id ON broker_actions
      WHEN NEW.trade_id<>OLD.trade_id BEGIN SELECT RAISE(ABORT,'Recovery trade ownership is immutable'); END;
    CREATE TRIGGER IF NOT EXISTS keep_owned_account_identity BEFORE UPDATE OF connection_id,account_ref ON broker_accounts
      WHEN (NEW.connection_id<>OLD.connection_id OR NEW.account_ref<>OLD.account_ref)
      AND (EXISTS(SELECT 1 FROM trade_broker_ownership WHERE broker_account_id=OLD.id)
           OR EXISTS(SELECT 1 FROM platform_broker_roles WHERE broker_account_id=OLD.id))
      BEGIN SELECT RAISE(ABORT,'Owned broker account identity is immutable'); END;
    CREATE TRIGGER IF NOT EXISTS keep_owned_connection_identity BEFORE UPDATE OF provider,environment ON broker_connections
      WHEN (NEW.provider<>OLD.provider OR NEW.environment<>OLD.environment)
      AND (EXISTS(SELECT 1 FROM trade_broker_ownership WHERE broker_connection_id=OLD.id)
           OR EXISTS(SELECT 1 FROM platform_broker_roles r JOIN broker_accounts a ON a.id=r.broker_account_id WHERE a.connection_id=OLD.id))
      BEGIN SELECT RAISE(ABORT,'Owned broker provider/environment is immutable'); END;
    CREATE TRIGGER IF NOT EXISTS pin_new_profile_route AFTER INSERT ON trading_profiles BEGIN
      INSERT INTO profile_execution_routes(profile_id,active_account_id) VALUES(NEW.id,NEW.broker_account_id);
    END;
    ''')
    for table in ('platform_broker_roles','trade_broker_ownership','broker_action_ownership'):
        for operation in (('UPDATE',) if table=='broker_action_ownership' else ('UPDATE','DELETE')):
            conn.execute(f'''CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation.lower()}
                BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'Broker ownership/reference role is immutable'); END''')
    _execute_batch(conn, """
    CREATE TRIGGER IF NOT EXISTS remove_deleted_action_owner AFTER DELETE ON broker_actions BEGIN
      DELETE FROM broker_action_ownership WHERE action_id=OLD.id;
    END;
    CREATE TRIGGER IF NOT EXISTS retain_live_action_owner BEFORE DELETE ON broker_action_ownership
      WHEN EXISTS(SELECT 1 FROM broker_actions WHERE id=OLD.action_id)
      BEGIN SELECT RAISE(ABORT,'Cannot remove the owner of an existing recovery action'); END;
    """)


def account(conn, account_id):
    row=conn.execute('''SELECT a.id AS broker_account_id,a.name AS account_name,c.id AS broker_connection_id,
        c.provider,c.environment,c.status AS connection_status,c.owner_user_id,
        CASE WHEN c.credentials_ref IS NOT NULL AND c.credentials_ref<>'' THEN 1 ELSE 0 END AS credentials_configured
        FROM broker_accounts a JOIN broker_connections c ON c.id=a.connection_id WHERE a.id=?''',(account_id,)).fetchone()
    if row is None:raise ValueError('Broker account was not found')
    return dict(row)


def readiness(conn, profile_id):
    row=conn.execute('SELECT * FROM profile_execution_routes WHERE profile_id=?',(profile_id,)).fetchone()
    if row is None:raise ValueError('Trading profile route was not found')
    active=account(conn,row['active_account_id'])
    planned=account(conn,row['planned_account_id']) if row['planned_account_id'] else None
    role=conn.execute("SELECT broker_account_id FROM platform_broker_roles WHERE role='admin_reference'").fetchone()
    reference=account(conn,role[0]) if role else None
    target=planned or active
    blockers=['Account-scoped operational storage, settings, reports and caches are not complete.',
              'Independent account workers and risk aggregation are not enabled.',
              'Per-connection secret resolution and connection-use authorization are not complete.']
    if target['connection_status']!='active':blockers.append('Destination broker connection is not active.')
    if not target['credentials_configured']:blockers.append('Destination has no configured credential reference.')
    if target['provider']!='etoro':blockers.append('Destination adapter has not been validated for an independent runtime.')
    return {'profile_id':profile_id,'admin_reference':reference,'active_destination':active,
            'planned_destination':planned,'additional_runtime_ready':False,'blockers':blockers}


def trade_owner(conn, trade_id):
    row=conn.execute('SELECT * FROM trade_broker_ownership WHERE trade_id=?',(trade_id,)).fetchone()
    if row is None:raise ValueError('Trade broker ownership was not found')
    return dict(row)

