"""One local writer runtime; cold restores cannot race a running Finbot."""
from contextlib import contextmanager
from pathlib import Path
import fcntl


@contextmanager
def database_lock(path, purpose='runtime'):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    lock_path=path.with_name('.'+path.name+'.'+purpose+'.lock')
    with lock_path.open('a') as handle:
        try:fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise RuntimeError(f'A database {purpose} operation is already running; no overlapping operation was started.') from None
        try:yield
        finally:fcntl.flock(handle.fileno(),fcntl.LOCK_UN)
