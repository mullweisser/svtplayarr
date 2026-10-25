import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path


class JobStore:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS releases (
                    token TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    category TEXT NOT NULL,
                    size INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    nzo_id TEXT PRIMARY KEY,
                    token TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    category TEXT NOT NULL,
                    status TEXT NOT NULL,
                    percentage INTEGER NOT NULL,
                    storage TEXT,
                    fail_message TEXT,
                    bytes INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    completed_at REAL
                );
                """
            )
            connection.commit()
        finally:
            connection.close()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _session(self):
        with self._lock:
            connection = self._connect()
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def save_release(self, title, url, category, size):
        token = secrets.token_hex(16)
        with self._session() as connection:
            connection.execute(
                "INSERT INTO releases (token, title, url, category, size) VALUES (?, ?, ?, ?, ?)",
                (token, title, url, category, int(size)),
            )
        return token

    def get_release(self, token):
        with self._session() as connection:
            row = connection.execute(
                "SELECT token, title, url, category, size FROM releases WHERE token = ?",
                (token,),
            ).fetchone()
        return dict(row) if row else None

    def add_job(self, release, category):
        nzo_id = "SABnzbd_nzo_" + secrets.token_hex(8)
        with self._session() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    nzo_id, token, title, url, category, status, percentage,
                    storage, fail_message, bytes, created_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, 'Queued', 0, NULL, '', ?, ?, NULL)
                """,
                (
                    nzo_id,
                    release["token"],
                    release["title"],
                    release["url"],
                    category,
                    int(release["size"]),
                    time.time(),
                ),
            )
        return nzo_id

    def claim_next(self):
        with self._session() as connection:
            row = connection.execute(
                """
                SELECT nzo_id, token, title, url, category, bytes
                FROM jobs
                WHERE status = 'Queued'
                ORDER BY created_at
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            updated = connection.execute(
                """
                UPDATE jobs
                SET status = 'Downloading', percentage = 50
                WHERE nzo_id = ? AND status = 'Queued'
                """,
                (row["nzo_id"],),
            )
            if updated.rowcount != 1:
                return None
        return dict(row)

    def mark_completed(self, nzo_id, storage):
        with self._session() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'Completed', percentage = 100, storage = ?,
                    fail_message = '', completed_at = ?
                WHERE nzo_id = ?
                """,
                (storage, time.time(), nzo_id),
            )

    def mark_failed(self, nzo_id, message):
        with self._session() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'Failed', percentage = 0, fail_message = ?, completed_at = ?
                WHERE nzo_id = ?
                """,
                (message[-500:], time.time(), nzo_id),
            )

    def queue(self, category=None, limit=None):
        return self._jobs(("Queued", "Downloading"), category, limit)

    def history(self, category=None, limit=None):
        return self._jobs(("Completed", "Failed"), category, limit, newest_first=True)

    def delete(self, nzo_id):
        with self._session() as connection:
            connection.execute("DELETE FROM jobs WHERE nzo_id = ?", (nzo_id,))

    def retry(self, nzo_id):
        with self._session() as connection:
            updated = connection.execute(
                """
                UPDATE jobs
                SET status = 'Queued', percentage = 0, fail_message = '',
                    storage = NULL, completed_at = NULL
                WHERE nzo_id = ? AND status = 'Failed'
                """,
                (nzo_id,),
            )
            return nzo_id if updated.rowcount == 1 else None

    def _jobs(self, statuses, category, limit, newest_first=False):
        placeholders = ", ".join("?" for _ in statuses)
        sql = f"""
            SELECT nzo_id, token, title, url, category, status, percentage,
                   storage, fail_message, bytes, created_at, completed_at
            FROM jobs
            WHERE status IN ({placeholders})
        """
        parameters = list(statuses)
        if category:
            sql += " AND category = ?"
            parameters.append(category)
        sql += " ORDER BY completed_at DESC" if newest_first else " ORDER BY created_at"
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(int(limit))
        with self._session() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [dict(row) for row in rows]
