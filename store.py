"""Where LineSwitch keeps its state: the schedule, mappings, holds and activity.

With DATABASE_URL set (Neon Postgres), everything lives in the database and survives
redeploys. Without it, it falls back to the state.json file.

Tables (created on first start):
  lineswitch_state     one row per key, the current state as JSON
  lineswitch_versions  a copy of the schedule each time it changes, for history and undo
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger("lineswitch.store")
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
KEY = os.environ.get("LINESWITCH_KEY", "main")

DDL = """
create table if not exists lineswitch_state (
  key text primary key,
  data jsonb not null,
  updated_at timestamptz not null default now()
);
create table if not exists lineswitch_versions (
  id bigserial primary key,
  key text not null,
  saved_at timestamptz not null default now(),
  reason text not null default '',
  rows jsonb not null
);
create index if not exists lineswitch_versions_key on lineswitch_versions (key, id desc);
"""


class Store:
    def __init__(self):
        self.kind = "neon" if DATABASE_URL else "file"
        self.error = ""
        self._conn = None
        self._lock = threading.Lock()
        if self.kind == "neon":
            try:
                self._run(lambda c: c.execute(DDL))
            except Exception as e:  # noqa
                self.error = f"database not reachable: {e}"
                log.error(self.error)

    # -------------------------------------------------- postgres
    def _connect(self):
        import psycopg
        self._conn = psycopg.connect(DATABASE_URL, autocommit=True, connect_timeout=10)
        return self._conn

    def _run(self, fn):
        """Run fn(conn) with one reconnect, since Neon closes idle connections."""
        with self._lock:
            for attempt in (1, 2):
                try:
                    c = self._conn if self._conn and not self._conn.closed else self._connect()
                    out = fn(c)
                    self.error = ""
                    return out
                except Exception as e:  # noqa
                    try:
                        if self._conn:
                            self._conn.close()
                    except Exception:  # noqa
                        pass
                    self._conn = None
                    if attempt == 2:
                        raise e

    def status(self) -> dict:
        return {"kind": "postgres" if self.kind == "neon" else "file", "error": self.error}

    # -------------------------------------------------- api
    def load(self) -> dict:
        if self.kind == "neon":
            try:
                row = self._run(lambda c: c.execute("select data from lineswitch_state where key=%s", (KEY,)).fetchone())
                if row:
                    d = row[0]
                    return d if isinstance(d, dict) else json.loads(d)
                # first start with a database: move an old state.json in if there is one
                if STATE_FILE.exists():
                    d = json.loads(STATE_FILE.read_text())
                    self.save(d)
                    return d
                return {}
            except Exception as e:  # noqa
                self.error = f"could not read from the database: {e}"
                log.error(self.error)
        if STATE_FILE.exists():
            try:
                return json.loads(STATE_FILE.read_text())
            except Exception as e:  # noqa
                log.warning("state file unreadable: %s", e)
        return {}

    def save(self, data: dict):
        text = json.dumps(data, default=str)
        if self.kind == "neon":
            try:
                self._run(lambda c: c.execute(
                    "insert into lineswitch_state (key, data, updated_at) values (%s, %s::jsonb, now()) "
                    "on conflict (key) do update set data = excluded.data, updated_at = now()", (KEY, text)))
                return
            except Exception as e:  # noqa
                self.error = f"could not save to the database, saved to file instead: {e}"
                log.error(self.error)
        try:
            STATE_FILE.write_text(text)
        except Exception as e:  # noqa
            log.error("could not write %s: %s", STATE_FILE, e)

    def version(self, rows: list, reason: str):
        """Keep a copy of the schedule each time it changes (database only)."""
        if self.kind != "neon":
            return
        try:
            self._run(lambda c: c.execute("insert into lineswitch_versions (key, reason, rows) values (%s, %s, %s::jsonb)",
                                          (KEY, reason[:200], json.dumps(rows, default=str))))
            self._run(lambda c: c.execute(
                "delete from lineswitch_versions where key=%s and id not in "
                "(select id from lineswitch_versions where key=%s order by id desc limit 200)", (KEY, KEY)))
        except Exception as e:  # noqa
            log.warning("could not save a schedule version: %s", e)

    def versions(self, limit: int = 30) -> list:
        if self.kind != "neon":
            return []
        try:
            rows = self._run(lambda c: c.execute(
                "select id, saved_at, reason, jsonb_array_length(rows) from lineswitch_versions where key=%s order by id desc limit %s",
                (KEY, limit)).fetchall())
            return [{"id": r[0], "saved_at": r[1].isoformat(), "reason": r[2], "count": r[3]} for r in rows]
        except Exception as e:  # noqa
            log.warning("could not list versions: %s", e)
            return []

    def version_rows(self, vid: int) -> list | None:
        if self.kind != "neon":
            return None
        row = self._run(lambda c: c.execute("select rows from lineswitch_versions where key=%s and id=%s", (KEY, vid)).fetchone())
        if not row:
            return None
        d = row[0]
        return d if isinstance(d, list) else json.loads(d)
