"""Prepare one private Fly import file from the running local Finbot container.

Never prints secret values. Does not connect to Fly or modify the application.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

CREDENTIAL_KEYS = (
    'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'ETORO_API_KEY',
    'ETORO_USER_KEY', 'OPENAI_API_KEY', 'FINBOT_PASSWORD',
    'FINBOT_SMTP_PASSWORD', 'OPENAI_ADMIN_KEY', 'OPENAI_ORGANIZATION_ID',
    'ETORO_MAIN_ACCOUNT_ID', 'ETORO_DEMO_ACCOUNT_ID',
)
SMTP_FIELDS = {
    'FINBOT_TRADE_EMAIL_ENABLED': 'enabled',
    'FINBOT_SMTP_HOST': 'host',
    'FINBOT_SMTP_PORT': 'port',
    'FINBOT_SMTP_SECURITY': 'security',
    'FINBOT_SMTP_USERNAME': 'username',
    'FINBOT_SMTP_FROM': 'sender',
    'FINBOT_TRADE_EMAIL_TO': 'recipients',
    'FINBOT_TRADE_EMAIL_ATTACH_JSON': 'attach_json',
    'FINBOT_SMTP_TIMEOUT_SECONDS': 'timeout_seconds',
    'FINBOT_TRADE_EMAIL_SUBJECT_PREFIX': 'subject_prefix',
}


def import_values(credentials, smtp):
    values = {key: str(credentials[key]) for key in CREDENTIAL_KEYS if credentials.get(key)}
    for key, field in SMTP_FIELDS.items():
        value = smtp[field]
        if isinstance(value, bool):
            value = str(value).lower()
        elif field == 'recipients':
            value = ','.join(value)
        values[key] = str(value)
    required = CREDENTIAL_KEYS[:6]
    missing = [key for key in required if not values.get(key)]
    if smtp['enabled'] and not values.get('FINBOT_SMTP_PASSWORD'):
        missing.append('FINBOT_SMTP_PASSWORD')
    if missing:
        raise ValueError('Missing required settings: ' + ', '.join(missing))
    if any('\n' in value or '\r' in value for value in values.values()):
        raise ValueError('Multiline values are not supported by this export.')
    return values


def write_private(path, values):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.fly-secrets-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            for key, value in sorted(values.items()):
                # Fly strips literal triple quotes; it does not JSON-unescape.
                # Refuse its ambiguous inline-comment case instead of altering a key.
                if '\n' in value or '\r' in value or ('#' in value and value.split('#', 1)[0].count('"') % 2):
                    raise ValueError('A value needs manual Fly secret entry; export was not replaced.')
                handle.write(key + '=' + '"""' + value + '"""' + '\n')
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='data/fly-secrets.env')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    program = (
        'import os,json; from dataclasses import asdict; '
        'from email_notifications import SMTPSettings; '
        'print(json.dumps({"credentials":{key:os.getenv(key,"") for key in '
        + repr(CREDENTIAL_KEYS) + '},'
        '"smtp":asdict(SMTPSettings.from_environment())}))'
    )
    # Capture in memory: never forward container stdout/stderr to the terminal.
    result = subprocess.run(
        ['docker', 'compose', 'exec', '-T', 'finbot', '/app/.venv/bin/python', '-c', program],
        cwd=root, capture_output=True, text=True,
    )
    if result.returncode:
        raise SystemExit('Could not read running Finbot configuration; no export created.')
    configuration = json.loads(result.stdout)
    values = import_values(configuration['credentials'], configuration['smtp'])
    destination = Path(args.output)
    if not destination.is_absolute():
        destination = root / destination
    write_private(destination, values)
    print(f'Prepared {len(values)} settings in {destination} (mode 600).')
    print('No values printed. No Fly upload performed.')


if __name__ == '__main__':
    main()
