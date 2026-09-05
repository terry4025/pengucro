"""Host request budget shared by independent programs, never their cookies."""
from contextlib import contextmanager
import hashlib
import os
import sqlite3
import threading
import time
import uuid
from email.utils import parsedate_to_datetime

import requests
from engines.browser_session import _pid_alive
from pengucro.storage import get_data_dir


class SharedReadGovernor:
    LIMIT = 32
    INTERVAL = 1 / 32

    def __init__(self, host):
        root = get_data_dir() / 'dpsnnn-budget'
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / (hashlib.sha256(host.encode()).hexdigest() + '.sqlite3')
        self.local = threading.local()
        self.db_lock = threading.Lock()
        with self._db() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS budget (id INTEGER PRIMARY KEY, next REAL, blocked REAL, failures INTEGER)')
            db.execute('INSERT OR IGNORE INTO budget VALUES (1,0,0,0)')
            db.execute('CREATE TABLE IF NOT EXISTS tickets (id TEXT PRIMARY KEY, pid INTEGER, priority INTEGER, created REAL, active INTEGER)')

    @contextmanager
    def _db(self):
        # This is transient scheduling state, not the durable order journal.
        # WAL/NORMAL avoid an fsync for every polling ticket on the hot path.
        with self.db_lock:
            db = sqlite3.connect(self.path, timeout=2)
            try:
                db.execute('PRAGMA synchronous=NORMAL')
                with db:
                    yield db
            finally:
                db.close()

    def acquire(self, priority=False, stop_event=None):
        ticket = uuid.uuid4().hex
        with self._db() as db:
            db.execute('INSERT INTO tickets VALUES (?,?,?,?,0)',
                       (ticket, os.getpid(), int(priority), time.time()))
        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    raise requests.RequestException('reservation stopped')
                with self._db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    for (pid,) in db.execute('SELECT DISTINCT pid FROM tickets').fetchall():
                        if not _pid_alive(pid):
                            db.execute('DELETE FROM tickets WHERE pid=?', (pid,))
                    next_at, blocked = db.execute('SELECT next,blocked FROM budget WHERE id=1').fetchone()
                    count = db.execute('SELECT COUNT(*) FROM tickets WHERE active=1').fetchone()[0]
                    first = db.execute('SELECT id FROM tickets WHERE active=0 ORDER BY priority DESC,created,id LIMIT 1').fetchone()
                    now = time.time()
                    if first and first[0] == ticket and count < self.LIMIT and now >= max(blocked, 0 if priority else next_at):
                        db.execute('UPDATE tickets SET active=1 WHERE id=?', (ticket,))
                        if not priority:
                            db.execute('UPDATE budget SET next=? WHERE id=1', (now+self.INTERVAL,))
                        self.local.ticket = ticket
                        return
                if stop_event is not None:
                    stop_event.wait(.02)
                else:
                    time.sleep(.02)
        except BaseException:
            with self._db() as db:
                db.execute('DELETE FROM tickets WHERE id=?', (ticket,))
            raise

    def release(self, response=None, failed=False):
        ticket = self.local.ticket
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM tickets WHERE id=?', (ticket,))
            blocked, failures = db.execute('SELECT blocked,failures FROM budget WHERE id=1').fetchone()
            status = getattr(response, 'status_code', 0)
            if failed or status == 429 or status >= 500:
                failures = min(failures+1, 6)
                delay = min(8, .25 * 2 ** (failures-1))
                retry = getattr(response, 'headers', {}).get('Retry-After', '')
                try:
                    delay = max(delay, float(retry))
                except (TypeError, ValueError):
                    try:
                        delay = max(delay, parsedate_to_datetime(retry).timestamp()-time.time())
                    except (TypeError, ValueError, OverflowError):
                        pass
                blocked = max(blocked, time.time()+delay)
            elif 200 <= status < 300:
                failures = 0
            db.execute('UPDATE budget SET blocked=?,failures=? WHERE id=1', (blocked, failures))
