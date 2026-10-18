"""Validated SQLite backups for AWS S3 and Tigris; never mount object storage.

Existing local databases always win at normal startup, including WAL commits.
Only an explicit cold-restore command may replace an existing database.
"""
from __future__ import annotations
import argparse
from dataclasses import dataclass,field
from datetime import datetime,timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import tempfile
import threading
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4
from database_runtime import database_lock

logging.basicConfig(level=os.getenv('FINBOT_LOG_LEVEL','INFO').upper(),format='%(asctime)s %(levelname)s %(name)s %(message)s')
logger=logging.getLogger('finbot.s3_backup')


def configuration_environment():
    """Host/Fly environment wins; persisted file fills missing storage settings."""
    values=dict(os.environ)
    path=Path(values.get('FINBOT_ENV_CONFIG_PATH',Path(__file__).resolve().parent/'.env.config'))
    if path.is_file():
        for line in path.read_text(encoding='utf-8').splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                key,value=line.strip().split('=',1)
                if key.startswith(('FINBOT_S3_','AWS_')) or key in {'FINBOT_DB_PATH','BUCKET_NAME'}:
                    values.setdefault(key,value.strip().strip('"').strip("'"))
    return values


def safe_error(error):
    # Transfer helpers wrap ClientError; preserve its code without its message.
    root=error
    for _ in range(8):
        code=str(getattr(root,'response',{}).get('Error',{}).get('Code',''))
        if code:
            return type(error).__name__+': '+re.sub('[^A-Za-z0-9_-]','',code)[:80]
        following=root.__cause__ or root.__context__
        if following is None or following is root:break
        root=following
    return type(error).__name__


def checksum(path):
    result=hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda:handle.read(1024*1024),b''):result.update(block)
    return result.hexdigest()


@dataclass(frozen=True)
class BackupSettings:
    enabled:bool
    database_path:Path
    bucket:str
    prefix:str
    region:str|None
    endpoint_url:str|None
    interval_seconds:int
    restore_mode:str
    restore_required:bool
    server_side_encryption:str
    kms_key_id:str|None
    provider:str='s3'
    access_key_id:str|None=field(default=None,repr=False)
    secret_access_key:str|None=field(default=None,repr=False)
    session_token:str|None=field(default=None,repr=False)

    @classmethod
    def from_environment(cls):
        env=configuration_environment()
        boolean=lambda key,default=False:str(env.get(key,str(default))).lower() in {'1','true','yes','on'}
        provider=env.get('FINBOT_S3_PROVIDER','s3').strip().lower()
        if provider not in {'s3','tigris'}:raise ValueError('FINBOT_S3_PROVIDER must be s3 or tigris.')
        endpoint=env.get('FINBOT_S3_ENDPOINT_URL') or env.get('AWS_ENDPOINT_URL_S3') or env.get('AWS_ENDPOINT_URL') or ('https://t3.storage.dev' if provider=='tigris' else None)
        if endpoint:
            parsed=urlsplit(endpoint)
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError('S3 endpoint must not contain credentials, query parameters or fragments.')
            if parsed.scheme not in {'https','http'} or not parsed.hostname:raise ValueError('Provide a valid S3 endpoint URL.')
            if parsed.hostname=='iam.storage.dev':raise ValueError('Use https://t3.storage.dev for backups; iam.storage.dev manages credentials, not objects.')
        is_tigris=provider=='tigris' or bool(endpoint and urlsplit(endpoint).hostname in {'t3.storage.dev','fly.storage.tigris.dev'})
        mode=env.get('FINBOT_S3_RESTORE_MODE','if_missing').strip().lower()
        if mode not in {'never','if_missing','if_newer','always'}:raise ValueError('Invalid FINBOT_S3_RESTORE_MODE.')
        interval=int(env.get('FINBOT_S3_BACKUP_INTERVAL_SECONDS','900'))
        if interval<60:raise ValueError('Backup interval must be at least 60 seconds.')
        settings=cls(enabled=boolean('FINBOT_S3_BACKUP_ENABLED'),
            database_path=Path(env.get('FINBOT_DB_PATH','/data/finbot.db')).expanduser().resolve(),
            bucket=(env.get('FINBOT_S3_BUCKET') or env.get('BUCKET_NAME') or '').strip(),
            prefix=env.get('FINBOT_S3_PREFIX','finbot').strip().strip('/'),
            region=env.get('AWS_REGION') or env.get('AWS_DEFAULT_REGION') or ('auto' if is_tigris else None),
            endpoint_url=endpoint,interval_seconds=interval,restore_mode=mode,
            restore_required=boolean('FINBOT_S3_RESTORE_REQUIRED'),
            server_side_encryption=env.get('FINBOT_S3_SERVER_SIDE_ENCRYPTION','AES256').strip(),
            kms_key_id=env.get('FINBOT_S3_KMS_KEY_ID') or None,provider='tigris' if is_tigris else provider,
            access_key_id=env.get('AWS_ACCESS_KEY_ID') or None,secret_access_key=env.get('AWS_SECRET_ACCESS_KEY') or None,
            session_token=env.get('AWS_SESSION_TOKEN') or None)
        if settings.enabled and not settings.bucket:raise ValueError('FINBOT_S3_BUCKET is required when backups are enabled.')
        return settings

    @property
    def latest_key(self):return f"{self.prefix+'/' if self.prefix else ''}latest/finbot.db"

    def versioned_key(self,timestamp):
        stamp=timestamp.astimezone(timezone.utc).strftime('%Y/%m/%d/finbot-%Y%m%dT%H%M%S.%fZ')
        return f"{self.prefix+'/' if self.prefix else ''}snapshots/{stamp}-{uuid4().hex[:8]}.db"

    def public(self):
        return dict(enabled=self.enabled,provider=self.provider,bucket=self.bucket,prefix=self.prefix,
            endpoint=self.endpoint_url,region=self.region,interval_seconds=self.interval_seconds,restore_mode=self.restore_mode,
            credentials_configured=bool(self.access_key_id and self.secret_access_key) if self.provider=='tigris' else 'SDK credential chain',
            latest_key=self.latest_key)


def status_path(settings):return settings.database_path.parent/'backup_status.json'


def read_status(settings):
    try:
        data=json.loads(status_path(settings).read_text())
        return data if data.get('destination')==[settings.bucket,settings.prefix,settings.endpoint_url] else {}
    except (OSError,ValueError):return {}


def write_status(settings,**updates):
    path=status_path(settings);path.parent.mkdir(parents=True,exist_ok=True)
    data={**read_status(settings),**updates,'destination':[settings.bucket,settings.prefix,settings.endpoint_url],'updated_at':datetime.now(timezone.utc).isoformat()}
    temporary=path.with_name('.'+path.name+'.'+uuid4().hex+'.tmp')
    try:
        temporary.write_text(json.dumps(data,sort_keys=True));temporary.chmod(0o600);os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)


class S3SQLiteBackup:
    def __init__(self,settings):self.settings=settings;self._client=None

    @property
    def s3(self):
        if self._client is None:
            if self.settings.provider=='tigris' and not (self.settings.access_key_id and self.settings.secret_access_key):
                raise RuntimeError('Tigris access key and secret are not configured.')
            import boto3
            from botocore.config import Config
            options:dict[str,Any]={'config':Config(signature_version='s3v4',connect_timeout=5,read_timeout=30,
                retries={'max_attempts':3,'mode':'standard'},
                s3={'addressing_style':'virtual' if self.settings.provider=='tigris' else 'auto'},
                request_checksum_calculation='when_required',response_checksum_validation='when_required')}
            for key,value in [('region_name',self.settings.region),('endpoint_url',self.settings.endpoint_url),
                ('aws_access_key_id',self.settings.access_key_id),('aws_secret_access_key',self.settings.secret_access_key),
                ('aws_session_token',self.settings.session_token)]:
                if value:options[key]=value
            self._client=boto3.client('s3',**options)
        return self._client

    def _upload_args(self,digest,created_at):
        args={'ContentType':'application/vnd.sqlite3','Metadata':{'sha256':digest,'created-at':created_at.astimezone(timezone.utc).isoformat()}}
        if self.settings.server_side_encryption:args['ServerSideEncryption']=self.settings.server_side_encryption
        if self.settings.server_side_encryption=='aws:kms' and self.settings.kms_key_id:args['SSEKMSKeyId']=self.settings.kms_key_id
        return args

    @staticmethod
    def _validate_database(path):
        c=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)
        try:
            rows=c.execute('PRAGMA quick_check').fetchall()
            if rows!=[('ok',)]:raise RuntimeError('SQLite snapshot integrity validation failed.')
        finally:c.close()

    def create_snapshot(self):
        now=datetime.now(timezone.utc)
        write_status(self.settings,last_attempt_at=now.isoformat())
        try:
            with database_lock(self.settings.database_path,'backup'):
                result=self._create_snapshot(now)
            write_status(self.settings,last_success_at=datetime.now(timezone.utc).isoformat(),last_key=result['key'],
                sha256=result['sha256'],bytes=result['bytes'],last_error=None)
            return result
        except Exception as error:
            write_status(self.settings,last_error=safe_error(error));raise

    def _create_snapshot(self,created_at):
        source_path=self.settings.database_path
        if not source_path.is_file():raise FileNotFoundError('SQLite database does not exist yet.')
        with tempfile.TemporaryDirectory(prefix='finbot-s3-backup-') as directory:
            snapshot=Path(directory)/'finbot.db'
            source=sqlite3.connect(source_path.as_uri()+'?mode=ro',uri=True,timeout=30)
            dest=sqlite3.connect(snapshot)
            try:source.backup(dest)
            finally:dest.close();source.close()
            self._validate_database(snapshot)
            digest=checksum(snapshot);size=snapshot.stat().st_size
            args=self._upload_args(digest,created_at);key=self.settings.versioned_key(created_at)
            self.s3.upload_file(str(snapshot),self.settings.bucket,key,ExtraArgs=args)
            self.s3.upload_file(str(snapshot),self.settings.bucket,self.settings.latest_key,ExtraArgs=args)
        logger.info('Uploaded validated SQLite snapshot to s3://%s/%s',self.settings.bucket,key)
        return dict(bucket=self.settings.bucket,key=key,sha256=digest,bytes=size)

    def _download_validated(self,path):
        # Body and metadata belong to the SAME GET, avoiding head/latest races.
        response=self.s3.get_object(Bucket=self.settings.bucket,Key=self.settings.latest_key)
        body=response['Body']
        try:
            with Path(path).open('wb') as handle:shutil.copyfileobj(body,handle)
        finally:body.close()
        expected=(response.get('Metadata') or {}).get('sha256')
        digest=checksum(path)
        if not expected or digest!=expected:raise RuntimeError('Backup SHA256 metadata is missing or does not match the downloaded snapshot.')
        self._validate_database(path)
        return dict(sha256=digest,bytes=Path(path).stat().st_size)

    def verify_latest(self):
        try:
            with tempfile.TemporaryDirectory(prefix='finbot-s3-verify-') as directory:
                result=self._download_validated(Path(directory)/'finbot.db')
        except Exception as error:
            write_status(self.settings,last_verification_error=safe_error(error))
            raise
        now=datetime.now(timezone.utc).isoformat()
        write_status(self.settings,last_verified_at=now,last_verified_sha256=result['sha256'],last_verification_error=None)
        return {**result,'bucket':self.settings.bucket,'key':self.settings.latest_key,'verified_at':now}

    def restore_latest(self,allow_overwrite=False):
        mode=self.settings.restore_mode;path=self.settings.database_path
        if mode=='never':return False
        if path.exists() and mode in {'if_missing','if_newer'}:
            if mode=='if_newer':logger.warning('Deprecated if_newer now preserves existing local databases; use if_missing. WAL state is never compared using mtimes.')
            logger.info('Persistent database exists; startup restore skipped.');return False
        if path.exists() and not allow_overwrite:
            raise RuntimeError('Replacing an existing database requires an explicit cold restore with --allow-overwrite; normal startup will not overwrite it.')
        with database_lock(path,'runtime'):
            if path.exists() and not allow_overwrite:
                raise RuntimeError('A local database appeared before restore; preserving it.')
            temporary=path.with_name('.'+path.name+'.restore-'+uuid4().hex+'.tmp')
            try:
                try:self._download_validated(temporary)
                except Exception as error:
                    code=str(getattr(error,'response',{}).get('Error',{}).get('Code',''))
                    if code in {'404','NoSuchKey','NotFound'} and not self.settings.restore_required:
                        logger.warning('No remote snapshot exists yet; initializing a new database is permitted.');return False
                    raise
                # A restored account must reconcile before an operator enables new entries.
                c=sqlite3.connect(temporary)
                try:
                    if c.execute("SELECT 1 FROM sqlite_master WHERE name='app_settings' AND type='table'").fetchone():
                        now=datetime.now(timezone.utc).isoformat()
                        c.executemany('INSERT INTO app_settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                            [('demo_automated_trading_enabled','false'),('demo_automation_default_migration_v1','complete'),('backup_restored_at',now),
                             ('backup_restored_from',f's3://{self.settings.bucket}/{self.settings.latest_key}')]);c.commit()
                finally:c.close()
                if path.exists():
                    recovery=path.parent/'recovery'/('pre-s3-restore-'+uuid4().hex);recovery.mkdir(parents=True)
                    for suffix in ('','-wal','-shm','-journal'):
                        original=Path(str(path)+suffix)
                        if original.exists():shutil.copy2(original,recovery/original.name)
                for suffix in ('-wal','-shm','-journal'):Path(str(path)+suffix).unlink(missing_ok=True)
                os.replace(temporary,path)
            finally:temporary.unlink(missing_ok=True)
        write_status(self.settings,last_restore_at=datetime.now(timezone.utc).isoformat(),restored_from=self.settings.latest_key)
        logger.info('Restored validated database; new demo entries are paused pending account reconciliation.')
        return True


def run_daemon(backup):
    stopping=threading.Event()
    def stop(_signum,_frame):stopping.set()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    logger.info('S3 backup daemon started; interval=%s seconds',backup.settings.interval_seconds)
    delay=min(10,backup.settings.interval_seconds)
    while not stopping.wait(delay):
        try:backup.create_snapshot()
        except Exception as error:logger.error('Scheduled backup failed (%s); retrying next interval.',safe_error(error))
        delay=backup.settings.interval_seconds
    try:backup.create_snapshot()
    except FileNotFoundError:logger.warning('No database exists for final backup.')
    except Exception as error:logger.error('Final backup failed (%s).',safe_error(error))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('restore','backup','verify','daemon'))
    parser.add_argument('--allow-overwrite',action='store_true',help='Explicit cold restore only; existing DB/WAL evidence is preserved.')
    args=parser.parse_args();settings=BackupSettings.from_environment()
    if not settings.enabled:logger.info('S3 backup is disabled.');return
    backup=S3SQLiteBackup(settings)
    if args.command=='restore':backup.restore_latest(allow_overwrite=args.allow_overwrite)
    elif args.command=='backup':backup.create_snapshot()
    elif args.command=='verify':print(json.dumps(backup.verify_latest(),sort_keys=True))
    else:run_daemon(backup)

if __name__=='__main__':main()
