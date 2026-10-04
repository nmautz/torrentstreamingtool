"""Persistent store behind the admin Devices tab: every device ever seen, its raw
requests, and its collapsed activity history.

SQLite (stdlib), WAL mode, one file: `logs/devices/activity.sqlite3`. It lives in a
SUBDIRECTORY of `logs/` on purpose -- `_archive_old_logs()`, `DELETE
/api/admin/logs` and the log-bundle download all iterate top-level *files* only,
so a directory is invisible to every sweep (the same trick `logs/client/` uses;
see docs/DIAGNOSTICS.md). Nothing deletes it but retention and an explicit forget.

Three tables, three lifetimes:

  * `devices`    -- one row per device, **kept forever** (the "ever connected" list).
  * `activities` -- the simple view: one row per run of same-kind requests. Small;
                    kept `ACT_KEEP_DAYS`.
  * `requests`   -- the raw view: one row per request. By far the biggest (a phone
                    streaming sends a segment every few seconds), so bounded by age
                    AND by row count, whichever bites first.

Plus `token_map`: a hashed pairing token -> the device that last sent it. Unused
since 19.8.0 removed pairing (the app's native requests now send `X-Device-Id`
themselves); the table stays so an existing store opens unchanged. Likewise the
`paired` column on `devices`, which no longer changes.

Writes happen in batches from one background task (`device_activity_loop` in
`main.py`), through `asyncio.to_thread`; the lock serialises the writer against
retention. Reads open their own connection, which WAL lets run alongside a write.
Leaf module: stdlib only, no `main` import. Tests in `tests/test_devstore.py`.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

RAW_KEEP_DAYS = 30
RAW_MAX_ROWS = 1_500_000
ACT_KEEP_DAYS = 365

_SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id           TEXT PRIMARY KEY,
    label        TEXT NOT NULL DEFAULT '',
    name         TEXT NOT NULL DEFAULT '',
    ua           TEXT NOT NULL DEFAULT '',
    ua_summary   TEXT NOT NULL DEFAULT '',
    ip           TEXT NOT NULL DEFAULT '',
    via          TEXT NOT NULL DEFAULT '',
    first_seen   REAL NOT NULL,
    last_seen    REAL NOT NULL,
    requests     INTEGER NOT NULL DEFAULT 0,
    profile_id   TEXT NOT NULL DEFAULT '',
    paired       INTEGER NOT NULL DEFAULT 0,
    app_version  TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS requests (
    id         INTEGER PRIMARY KEY,
    device_id  TEXT NOT NULL,
    ts         REAL NOT NULL,
    method     TEXT NOT NULL,
    path       TEXT NOT NULL,
    query      TEXT NOT NULL DEFAULT '',
    status     INTEGER NOT NULL DEFAULT 0,
    ms         REAL NOT NULL DEFAULT 0,
    ip         TEXT NOT NULL DEFAULT '',
    ua         TEXT NOT NULL DEFAULT '',
    profile_id TEXT NOT NULL DEFAULT '',
    kind       TEXT NOT NULL DEFAULT '',
    via        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS requests_dev_ts ON requests(device_id, ts);
CREATE INDEX IF NOT EXISTS requests_ts ON requests(ts);
CREATE TABLE IF NOT EXISTS activities (
    id         INTEGER PRIMARY KEY,
    device_id  TEXT NOT NULL,
    kind       TEXT NOT NULL,
    key        TEXT NOT NULL DEFAULT '',
    subject    TEXT NOT NULL DEFAULT '',
    start      REAL NOT NULL,
    end        REAL NOT NULL,
    count      INTEGER NOT NULL DEFAULT 1,
    profile_id TEXT NOT NULL DEFAULT '',
    errors     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS activities_dev_end ON activities(device_id, end);
CREATE TABLE IF NOT EXISTS token_map (
    token_hash TEXT PRIMARY KEY,
    device_id  TEXT NOT NULL,
    updated    REAL NOT NULL
);
"""

_DEVICE_COLS = ("id", "label", "name", "ua", "ua_summary", "ip", "via", "first_seen",
                "last_seen", "requests", "profile_id", "paired", "app_version")
_REQ_COLS = ("device_id", "ts", "method", "path", "query", "status", "ms", "ip", "ua",
             "profile_id", "kind", "via")
_ACT_COLS = ("id", "device_id", "kind", "key", "subject", "start", "end", "count",
             "profile_id", "errors")


class DeviceStore:
    def __init__(self, path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._w = self._connect()
        with self._lock:
            self._w.executescript(_SCHEMA)
            # Columns added after a store already existed. CREATE TABLE IF NOT
            # EXISTS leaves an old table alone, so they are added here.
            cols = {r["name"] for r in self._w.execute("PRAGMA table_info(devices)")}
            if "app_version" not in cols:     # the iOS app's version (19.7.0)
                self._w.execute("ALTER TABLE devices ADD COLUMN app_version TEXT NOT NULL DEFAULT ''")
            self._w.commit()

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self.path), timeout=10, check_same_thread=False)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.row_factory = sqlite3.Row
        return c

    def close(self) -> None:
        with self._lock:
            try:
                self._w.close()
            except sqlite3.Error:
                pass

    # ── writes ────────────────────────────────────────────────────────────────
    def write(self, requests=(), devices=(), activities=(), tokens=()) -> list:
        """One transaction for one drain of the event queue.

        `requests`: dicts with `_REQ_COLS`. `devices`: dicts with `_DEVICE_COLS`
        minus `label` -- upserted; `first_seen` only sticks on insert,
        `requests` is ADDED to the stored count. `activities`: dicts with
        `_ACT_COLS`; one with an `id` updates that row, one without is inserted.
        `tokens`: `(token_hash, device_id, ts)`.

        Returns the ids of the activities, in the order given, so the caller can
        keep extending a row it just created.
        """
        ids = []
        with self._lock:
            c = self._w
            if requests:
                c.executemany(
                    f"INSERT INTO requests ({','.join(_REQ_COLS)}) VALUES ({','.join('?' * len(_REQ_COLS))})",
                    [tuple(r.get(k, "") if k not in ("ts", "ms", "status") else (r.get(k) or 0)
                           for k in _REQ_COLS) for r in requests])
            for d in devices:
                c.execute(
                    """INSERT INTO devices (id, name, ua, ua_summary, ip, via, first_seen,
                                            last_seen, requests, profile_id, paired, app_version)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET
                         name       = CASE WHEN excluded.name <> '' THEN excluded.name ELSE devices.name END,
                         ua         = CASE WHEN excluded.ua <> '' THEN excluded.ua ELSE devices.ua END,
                         ua_summary = CASE WHEN excluded.ua_summary <> '' THEN excluded.ua_summary ELSE devices.ua_summary END,
                         ip         = CASE WHEN excluded.ip <> '' THEN excluded.ip ELSE devices.ip END,
                         via        = CASE WHEN excluded.via <> '' THEN excluded.via ELSE devices.via END,
                         last_seen  = MAX(devices.last_seen, excluded.last_seen),
                         first_seen = MIN(devices.first_seen, excluded.first_seen),
                         requests   = devices.requests + excluded.requests,
                         profile_id = CASE WHEN excluded.profile_id <> '' THEN excluded.profile_id ELSE devices.profile_id END,
                         paired     = MAX(devices.paired, excluded.paired),
                         app_version = CASE WHEN excluded.app_version <> '' THEN excluded.app_version ELSE devices.app_version END""",
                    (d["id"], d.get("name", ""), d.get("ua", ""), d.get("ua_summary", ""),
                     d.get("ip", ""), d.get("via", ""), d["first_seen"], d["last_seen"],
                     int(d.get("requests", 0)), d.get("profile_id", ""), int(bool(d.get("paired"))),
                     d.get("app_version", "")))
            for a in activities:
                if a.get("id"):
                    c.execute("UPDATE activities SET end=?, count=?, subject=?, key=?, profile_id=?, errors=? WHERE id=?",
                              (a["end"], a["count"], a.get("subject", ""), a.get("key", ""),
                               a.get("profile_id", ""), int(a.get("errors", 0)), a["id"]))
                    ids.append(a["id"])
                else:
                    cur = c.execute(
                        "INSERT INTO activities (device_id, kind, key, subject, start, end, count, profile_id, errors) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (a["device_id"], a["kind"], a.get("key", ""), a.get("subject", ""),
                         a["start"], a["end"], a.get("count", 1), a.get("profile_id", ""),
                         int(a.get("errors", 0))))
                    ids.append(cur.lastrowid)
            for th, did, ts in tokens:
                c.execute("INSERT INTO token_map (token_hash, device_id, updated) VALUES (?,?,?) "
                          "ON CONFLICT(token_hash) DO UPDATE SET device_id=excluded.device_id, updated=excluded.updated",
                          (th, did, ts))
            c.commit()
        return ids

    def set_label(self, device_id: str, label: str) -> bool:
        with self._lock:
            cur = self._w.execute("UPDATE devices SET label=? WHERE id=?", ((label or "")[:80], device_id))
            self._w.commit()
            return cur.rowcount > 0

    def forget(self, device_id: str) -> int:
        """Delete a device and everything recorded about it. Returns rows removed."""
        with self._lock:
            c = self._w
            n = c.execute("DELETE FROM requests WHERE device_id=?", (device_id,)).rowcount
            n += c.execute("DELETE FROM activities WHERE device_id=?", (device_id,)).rowcount
            n += c.execute("DELETE FROM token_map WHERE device_id=?", (device_id,)).rowcount
            n += c.execute("DELETE FROM devices WHERE id=?", (device_id,)).rowcount
            c.commit()
            return n

    def prune(self, now: float, raw_days: float = RAW_KEEP_DAYS,
              act_days: float = ACT_KEEP_DAYS, max_raw_rows: int = RAW_MAX_ROWS) -> dict:
        """Retention. Devices are never pruned; raw requests by age then count;
        activities by age."""
        with self._lock:
            c = self._w
            raw = c.execute("DELETE FROM requests WHERE ts < ?", (now - raw_days * 86400,)).rowcount
            total = c.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
            if total > max_raw_rows:
                cut = c.execute("SELECT ts FROM requests ORDER BY ts DESC LIMIT 1 OFFSET ?",
                                (max_raw_rows,)).fetchone()
                if cut:
                    raw += c.execute("DELETE FROM requests WHERE ts <= ?", (cut[0],)).rowcount
            act = c.execute("DELETE FROM activities WHERE end < ?", (now - act_days * 86400,)).rowcount
            c.commit()
        return {"requests": raw, "activities": act}

    # ── reads (own connection each: WAL lets them run beside the writer) ──────
    def _read(self, sql: str, args=()) -> list:
        c = self._connect()
        try:
            return [dict(r) for r in c.execute(sql, args).fetchall()]
        finally:
            c.close()

    def devices(self) -> list:
        return self._read("SELECT * FROM devices ORDER BY last_seen DESC")

    def device(self, device_id: str):
        rows = self._read("SELECT * FROM devices WHERE id=?", (device_id,))
        return rows[0] if rows else None

    def token_map(self) -> dict:
        return {r["token_hash"]: r["device_id"] for r in self._read("SELECT * FROM token_map")}

    def requests(self, device_id: str, before=None, limit: int = 200, q: str = "") -> list:
        sql = "SELECT * FROM requests WHERE device_id=?"
        args: list = [device_id]
        if before:
            sql += " AND ts < ?"
            args.append(float(before))
        if q:
            sql += " AND (path LIKE ? OR query LIKE ? OR kind = ?)"
            like = f"%{q}%"
            args += [like, like, q]
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 1000)))
        return self._read(sql, args)

    def activities(self, device_id: str, before=None, limit: int = 100) -> list:
        sql = "SELECT * FROM activities WHERE device_id=?"
        args: list = [device_id]
        if before:
            sql += " AND end < ?"
            args.append(float(before))
        sql += " ORDER BY end DESC, id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        return self._read(sql, args)

    def last_activities(self, kinds_excluded=()) -> dict:
        """device_id -> its newest activity whose kind is not excluded."""
        ph = ",".join("?" * len(kinds_excluded)) or "''"
        rows = self._read(
            f"""SELECT a.* FROM activities a JOIN (
                   SELECT device_id, MAX(end) AS m FROM activities
                   WHERE kind NOT IN ({ph}) GROUP BY device_id) t
                ON a.device_id = t.device_id AND a.end = t.m
                WHERE a.kind NOT IN ({ph})""", tuple(kinds_excluded) * 2)
        return {r["device_id"]: r for r in rows}

    def stats(self) -> dict:
        r = self._read("SELECT (SELECT COUNT(*) FROM devices) AS devices, "
                       "(SELECT COUNT(*) FROM requests) AS requests, "
                       "(SELECT COUNT(*) FROM activities) AS activities, "
                       "(SELECT MIN(ts) FROM requests) AS oldest_request")[0]
        size = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                size += os.path.getsize(str(self.path) + suffix)
            except OSError:
                pass
        r["bytes"] = size
        return r
