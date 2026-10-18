"""Shared-profile permissions and future account ownership metadata.

Only profile 1 has an active trading runtime. Other profiles are drafts until
account-scoped storage, broker routing, workers and caches are implemented.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json

ROLES={'viewer':1,'operator':2,'owner':3}
_request=ContextVar('finbot_web_request',default=None)
_login_ticket=ContextVar('finbot_login_ticket',default=None)


def migrate(conn, provider='etoro'):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS broker_connections (
        id INTEGER PRIMARY KEY, owner_user_id INTEGER REFERENCES users(id),
        name TEXT NOT NULL, provider TEXT NOT NULL, environment TEXT NOT NULL,
        credentials_ref TEXT, status TEXT NOT NULL DEFAULT 'draft');
    CREATE TABLE IF NOT EXISTS broker_accounts (
        id INTEGER PRIMARY KEY, connection_id INTEGER NOT NULL REFERENCES broker_connections(id),
        account_ref TEXT NOT NULL, name TEXT NOT NULL, UNIQUE(connection_id,account_ref));
    CREATE TABLE IF NOT EXISTS trading_profiles (
        id INTEGER PRIMARY KEY, name TEXT NOT NULL, broker_account_id INTEGER REFERENCES broker_accounts(id),
        status TEXT NOT NULL CHECK(status IN ('active','draft')), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE UNIQUE INDEX IF NOT EXISTS one_active_profile_runtime ON trading_profiles(status) WHERE status='active';
    CREATE TABLE IF NOT EXISTS profile_memberships (
        profile_id INTEGER NOT NULL REFERENCES trading_profiles(id),
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        role TEXT NOT NULL CHECK(role IN ('viewer','operator','owner')), PRIMARY KEY(profile_id,user_id));
    CREATE TABLE IF NOT EXISTS access_events (
        id INTEGER PRIMARY KEY, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        actor TEXT NOT NULL, action TEXT NOT NULL, details TEXT NOT NULL);
    ''')
    conn.execute('BEGIN IMMEDIATE')
    exists=conn.execute('SELECT 1 FROM trading_profiles WHERE id=1').fetchone()
    if not exists:
        owner=conn.execute('SELECT id FROM users WHERE is_admin=1 AND is_active=1 ORDER BY id LIMIT 1').fetchone()
        owner_id=owner['id'] if owner else None
        conn.execute("INSERT INTO broker_connections VALUES (1,?,'Current runtime connection',?,'demo','runtime.env','active')",(owner_id,provider))
        conn.execute("INSERT INTO broker_accounts VALUES (1,1,'runtime-default','Current demo account')")
        conn.execute("INSERT INTO trading_profiles(id,name,broker_account_id,status) VALUES (1,'Finbot shared demo',1,'active')")
        conn.execute("INSERT INTO profile_memberships SELECT 1,id,CASE WHEN is_admin=1 THEN 'owner' ELSE 'viewer' END FROM users")
    # Idempotent tagging only. Trading policies and broker references are unchanged.
    columns={r['name'] for r in conn.execute('PRAGMA table_info(trades)')}
    if 'trading_profile_id' not in columns:
        conn.execute('ALTER TABLE trades ADD COLUMN trading_profile_id INTEGER NOT NULL DEFAULT 1')
    # Other operational tables inherit the single active runtime for now. Their
    # account/profile scoping must precede activation of any additional runtime.
    conn.commit()


class AccessControl:
    def __init__(self, connect, validate_token):self.connect=connect;self.validate_token=validate_token

    @contextmanager
    def web_request(self, token=None, profile_id=1):
        marker=_request.set({'token':token,'profile_id':profile_id})
        login_marker=_login_ticket.set(None)
        try:yield
        finally:
            _request.reset(marker)
            _login_ticket.reset(login_marker)

    def login_verified(self,username):
        _login_ticket.set(username)

    def require_login_ticket(self,username):
        if _request.get() is not None and _login_ticket.get()!=username:
            raise PermissionError('Authenticate this user before creating a session.')
        _login_ticket.set(None)

    def bind_session(self, token, profile_id=1):
        _request.set({'token':token,'profile_id':int(profile_id)})
        return self.identity()

    def identity(self):
        context=_request.get()
        if context is None:return None # Trusted in-process worker, never an HTTP request.
        username=self.validate_token(context.get('token')) if context.get('token') else None
        if not username:raise PermissionError('Your session has expired or the account is suspended. Sign in again.')
        conn=self.connect()
        try:row=conn.execute('SELECT id,username,is_admin,is_active FROM users WHERE username=?',(username,)).fetchone()
        finally:conn.close()
        if not row or not row['is_active']:raise PermissionError('User account is suspended.')
        return dict(row)

    def require_admin(self):
        user=self.identity()
        if user is not None and not user['is_admin']:raise PermissionError('Platform administrator permission is required.')
        return user

    def require_self(self,username):
        user=self.identity()
        if user is not None and user['username']!=username and not user['is_admin']:
            raise PermissionError('You may manage only your own login.')
        return user

    def require_profile(self, role='viewer', profile_id=None, runtime=True):
        user=self.identity();context=_request.get()
        pid=int(profile_id if profile_id is not None else context['profile_id'] if context else 1)
        if user is None and pid==1:return {'id':1,'status':'active','effective_role':'owner'}
        conn=self.connect()
        try:
            profile=conn.execute('SELECT * FROM trading_profiles WHERE id=?',(pid,)).fetchone()
            member=conn.execute('SELECT role FROM profile_memberships WHERE profile_id=? AND user_id=?',(pid,user['id'])).fetchone() if user else None
        finally:conn.close()
        if not profile:raise PermissionError('Trading profile was not found.')
        if user is not None and not user['is_admin'] and (not member or ROLES[member['role']]<ROLES[role]):
            raise PermissionError(role.title()+' permission on this trading profile is required.')
        if runtime and (pid!=1 or profile['status']!='active'):
            raise PermissionError('This profile is a draft. Its independent trading runtime is not enabled.')
        return {**dict(profile),'effective_role':'owner' if user is None or user['is_admin'] else member['role']}

    def audit(self,conn,action,details):
        user=self.identity()
        conn.execute('INSERT INTO access_events(actor,action,details) VALUES (?,?,?)',
            (user['username'] if user else 'system',action,json.dumps(details,sort_keys=True)))

    def list_profiles(self):
        user=self.identity();conn=self.connect()
        try:
            if user is None or user['is_admin']:
                rows=conn.execute("SELECT p.*, 'owner' AS effective_role FROM trading_profiles p ORDER BY id").fetchall()
            else:
                rows=conn.execute('SELECT p.*,m.role AS effective_role FROM trading_profiles p JOIN profile_memberships m ON m.profile_id=p.id WHERE m.user_id=? ORDER BY p.id',(user['id'],)).fetchall()
            return [dict(r) for r in rows]
        finally:conn.close()

    def create_draft(self,name,provider='undecided',environment='demo'):
        user=self.identity()
        if user is None:raise PermissionError('Choose a signed-in owner before creating a draft profile.')
        if not name.strip() or provider not in {'etoro','ibkr','binance','undecided'} or environment!='demo':
            raise ValueError('Provide a name and a supported demo provider; independent trading is not enabled.')
        conn=self.connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            connection=conn.execute("INSERT INTO broker_connections(owner_user_id,name,provider,environment,status) VALUES (?,?,?,?, 'draft')",(user['id'],name.strip()+' connection',provider,environment)).lastrowid
            account=conn.execute('INSERT INTO broker_accounts(connection_id,account_ref,name) VALUES (?,?,?)',(connection,'not-connected','Planned demo account')).lastrowid
            pid=conn.execute("INSERT INTO trading_profiles(name,broker_account_id,status) VALUES (?,?,'draft')",(name.strip(),account)).lastrowid
            conn.execute("INSERT INTO profile_memberships VALUES (?,?,'owner')",(pid,user['id']))
            self.audit(conn,'create_draft_profile',{'profile_id':pid,'provider':provider});conn.commit();return pid
        finally:conn.close()

    def members(self,profile_id):
        self.require_profile('owner',profile_id,runtime=False)
        conn=self.connect()
        try:return [dict(r) for r in conn.execute('SELECT u.username,u.is_active,m.role FROM profile_memberships m JOIN users u ON u.id=m.user_id WHERE m.profile_id=? ORDER BY u.username',(profile_id,))]
        finally:conn.close()

    def set_membership(self,profile_id,username,role):
        self.require_profile('owner',profile_id,runtime=False)
        if role is not None and role not in ROLES:raise ValueError('Choose viewer, operator or owner.')
        conn=self.connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            user=conn.execute('SELECT id,is_active FROM users WHERE username=?',(username,)).fetchone()
            if not user:raise ValueError('User account was not found.')
            existing=conn.execute('SELECT role FROM profile_memberships WHERE profile_id=? AND user_id=?',(profile_id,user['id'])).fetchone()
            if existing and existing['role']=='owner' and role!='owner' and user['is_active']:
                count=conn.execute("SELECT count(*) FROM profile_memberships m JOIN users u ON u.id=m.user_id WHERE m.profile_id=? AND m.role='owner' AND u.is_active=1",(profile_id,)).fetchone()[0]
                if count<=1:raise ValueError('Keep at least one active profile owner.')
            if role is None:conn.execute('DELETE FROM profile_memberships WHERE profile_id=? AND user_id=?',(profile_id,user['id']))
            else:conn.execute('INSERT INTO profile_memberships VALUES (?,?,?) ON CONFLICT(profile_id,user_id) DO UPDATE SET role=excluded.role',(profile_id,user['id'],role))
            self.audit(conn,'set_profile_membership',{'profile_id':profile_id,'username':username,'role':role});conn.commit()
        finally:conn.close()
