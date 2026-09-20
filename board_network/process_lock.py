"""Process ownership backed by an OS lock; stale files never imply a live owner."""
import errno
import json
import os
import tempfile
from pathlib import Path

from . import VERSION
from .common import NetworkError, now


class InstanceLock:
    def __init__(self, path, **metadata):
        self.path = Path(path)
        self.metadata = metadata
        self.stream = None

    def acquire(self):
        if self.stream is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                if stream.seek(0, 2) == 0:
                    stream.write(b'0'); stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                return False
            raise
        self.stream = stream
        if self.metadata:
            owner = dict(self.metadata, pid=os.getpid(), version=VERSION, started_at=now())
            fd, temporary = tempfile.mkstemp(prefix='.owner-', dir=self.path.parent)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as out:
                    json.dump(owner, out, ensure_ascii=False); out.flush(); os.fsync(out.fileno())
                os.replace(temporary, str(self.path) + '.json')
            except BaseException:
                self.close()
                raise
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return True

    def close(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None
        # Do not unlink the lock file: waiters must continue to lock the same inode.

    def __enter__(self):
        if not self.acquire():
            raise NetworkError('another local service owns ' + str(self.path) + '; reuse or stop that service first', 409)
        return self

    def __exit__(self, *exc):
        self.close()


def inspect_lock(path):
    path = Path(path)
    if not path.exists():
        return {'running': False}
    lock = InstanceLock(path)
    try:
        running = not lock.acquire()
        try:
            owner = json.loads(Path(str(path) + '.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            owner = None
        return {'running': running, 'owner': owner if running else None}
    finally:
        lock.close()
