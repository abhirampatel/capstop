import os
import sqlite3
import threading
import time

NANO = 1_000_000_000


class Ledger:
    def __init__(self, path: str):
        old = os.umask(0o077)
        try:
            self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
            self.db.execute("PRAGMA journal_mode=WAL")
        finally:
            os.umask(old)
        for f in (path, path + "-wal", path + "-shm"):
            if path != ":memory:" and os.path.exists(f):
                os.chmod(f, 0o600)
        self.lock = threading.Lock()
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, cap_nano INTEGER NOT NULL,"
            " spent_nano INTEGER NOT NULL DEFAULT 0, reserved_nano INTEGER NOT NULL DEFAULT 0,"
            " calls INTEGER NOT NULL DEFAULT 0, over_cap INTEGER NOT NULL DEFAULT 0, updated REAL)"
        )
        self.db.execute("UPDATE runs SET reserved_nano = 0")

    def reserve(self, run_id: str, cap_nano: int, amount: int, global_nano: int):
        """Returns (ok, row, headroom_nano); row None = global ceiling hit (nothing written)."""
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                total = self.db.execute(
                    "SELECT COALESCE(SUM(spent_nano + reserved_nano), 0) FROM runs"
                ).fetchone()[0]
                if total + amount > global_nano:
                    self.db.execute("COMMIT")
                    return False, None, 0
                self.db.execute(
                    "INSERT OR IGNORE INTO runs (run_id, cap_nano, updated) VALUES (?, ?, ?)",
                    (run_id, cap_nano, time.time()),
                )
                row = self._get(run_id)
                headroom = row["cap_nano"] - row["spent_nano"] - row["reserved_nano"]
                ok = not row["over_cap"] and amount <= headroom
                if ok:
                    self.db.execute(
                        "UPDATE runs SET reserved_nano = reserved_nano + ? WHERE run_id = ?", (amount, run_id)
                    )
                self.db.execute("COMMIT")
                return ok, row, headroom
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def settle(self, run_id: str, reserved: int, actual: int, over_cap: bool = False) -> bool:
        """Returns the run's over_cap after settling (as GET /runs reports it)."""
        with self.lock:
            self.db.execute(
                "UPDATE runs SET reserved_nano = reserved_nano - ?, spent_nano = spent_nano + ?,"
                " calls = calls + ?, over_cap = MAX(over_cap, ?, spent_nano + ? > cap_nano), updated = ? WHERE run_id = ?",
                (reserved, actual, 1 if actual else 0, int(over_cap), actual, time.time(), run_id),
            )
            return bool(self._get(run_id)["over_cap"])

    def _get(self, run_id: str):
        cur = self.db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        r = cur.fetchone()
        return dict(zip([c[0] for c in cur.description], r)) if r else None

    def _raw(self, run_id: str):
        with self.lock:
            return self._get(run_id)

    def get(self, run_id: str):
        with self.lock:
            r = self._get(run_id)
        return to_public(r) if r else None

    def all(self):
        with self.lock:
            cur = self.db.execute("SELECT * FROM runs ORDER BY updated DESC")
            names = [c[0] for c in cur.description]
            return [to_public(dict(zip(names, r))) for r in cur.fetchall()]


def usd(nano: int) -> float:
    return round(nano / NANO, 9)


def to_public(r: dict) -> dict:
    return {
        "run_id": r["run_id"],
        "spent_usd": usd(r["spent_nano"]),
        "cap_usd": usd(r["cap_nano"]),
        "calls": r["calls"],
        "over_cap": bool(r["over_cap"]),
    }
