"""LineSwitch engine: reads the schedule and keeps each line's cost code group in TCP
holding the cost codes of the work order that should be running now.

A "line" is a cost code group in TCP ("Line 1") attached to that line's job code (LINE 1).
Each kiosk's clock configuration is filtered to its line job code, so the kiosk shows the group's codes.
A WO's cost codes are the active cost codes whose Level 1 is the WO number (112\\SETUP, 112\\FILLING).
Switching a line from WO 112 to WO 113 = in that group, remove 112's cost codes, add 113's.
Cost codes and old punches are never deleted; only group membership changes.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from tcp import Tcp, TcpError

log = logging.getLogger("lineswitch")
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))


def norm(s) -> str:
    return "".join(ch for ch in str(s or "").lower() if ch.isalnum())


# ---------------------------------------------------------------- schedule
@dataclass
class Row:
    line: str            # cost code group name as written in the file
    wo: str              # work order = job code id
    start: datetime
    end: datetime
    codes: list[str] = field(default_factory=list)   # optional: cost codes listed in the file
    note: str = ""

    def covers(self, t: datetime) -> bool:
        return self.start <= t < self.end

    def to_json(self):
        d = asdict(self)
        d["start"], d["end"] = self.start.isoformat(), self.end.isoformat()
        return d

    @staticmethod
    def from_json(d):
        return Row(d["line"], d["wo"], datetime.fromisoformat(d["start"]), datetime.fromisoformat(d["end"]),
                   d.get("codes") or [], d.get("note", ""))


HEAD = {
    "line": "line", "clockconfiguration": "line", "costcodegroup": "line", "kiosk": "line", "group": "line",
    "workorder": "wo", "wo": "wo", "wonumber": "wo", "jobcode": "wo", "job": "wo",
    "start": "start", "starttime": "start", "from": "start", "end": "end", "endtime": "end", "to": "end",
    "date": "date", "costcodes": "codes", "costcode": "codes", "note": "note", "notes": "note",
    "description": "note",
}
FORMATS = ["%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y %I:%M %p",
           "%m/%d/%y %H:%M", "%m/%d/%Y %I:%M:%S %p", "%Y-%m-%d %I:%M %p"]


class ScheduleError(ValueError):
    pass


def _dt(v, date=None) -> datetime:
    if isinstance(v, datetime):
        return v
    if hasattr(v, "hour") and date is not None:              # a time cell plus a Date column
        return datetime.combine(_dt(date).date(), v)
    s = str(v).strip()
    if date is not None and len(s) <= 8:                     # "5:00 AM" or "17:00" with a Date column
        d = _dt(date).date()
        for f in ("%H:%M", "%I:%M %p", "%H:%M:%S", "%I %p"):
            try:
                return datetime.combine(d, datetime.strptime(s.upper(), f).time())
            except ValueError:
                pass
    for f in FORMATS:
        try:
            return datetime.strptime(s, f)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        raise ScheduleError(f"cannot read the date or time {s!r}")


def parse_schedule(name: str, data: bytes) -> list[Row]:
    if name.lower().endswith((".xlsx", ".xlsm")):
        import openpyxl
        ws = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True).active
        it = ws.iter_rows(values_only=True)
        header = [HEAD.get(norm(h), norm(h)) for h in next(it)]
        raw = [dict(zip(header, r)) for r in it if r and any(c not in (None, "") for c in r)]
    else:
        text = data.decode("utf-8-sig")
        rd = csv.reader(io.StringIO(text))
        header = [HEAD.get(norm(h), norm(h)) for h in next(rd)]
        raw = [dict(zip(header, r)) for r in rd if r and any(c.strip() for c in r)]
    rows = []
    for i, r in enumerate(raw, start=2):
        for k in ("line", "wo", "start", "end"):
            if r.get(k) in (None, ""):
                raise ScheduleError(f"row {i}: the {k.upper() if k == 'wo' else k.title()} column is empty or missing")
        date = r.get("date") or None
        start, end = _dt(r["start"], date), _dt(r["end"], date)
        if end <= start:
            end += timedelta(days=1)                           # 6pm to 2am crosses midnight
        wo = str(r["wo"]).strip()
        if wo.endswith(".0"):
            wo = wo[:-2]
        codes = [c.strip() for c in str(r.get("codes") or "").replace(";", ",").split(",") if c.strip()]
        rows.append(Row(str(r["line"]).strip(), wo, start, end, codes, str(r.get("note") or "").strip()))
    rows.sort(key=lambda x: (norm(x.line), x.start))
    for a, b in zip(rows, rows[1:]):
        if norm(a.line) == norm(b.line) and b.start < a.end:
            raise ScheduleError(f"{a.line}: WO {a.wo} and WO {b.wo} overlap ({a.start:%H:%M} to {a.end:%H:%M} and {b.start:%H:%M})")
    return rows


# ---------------------------------------------------------------- engine
class Engine:
    def __init__(self, tcp: Tcp):
        self.tcp = tcp
        self.lock = threading.RLock()
        self.groups: dict[str, dict] = {}       # group id -> {id, description, jobCodeAssignments, codes, ...}
        self.schedule: list[Row] = []
        self.lines: dict[str, dict] = {}        # group id -> {live_wo, hold, override, error, is_line, retry_at}
        self.preview = True
        self.activity: list[dict] = []
        self.last_sync = None
        self.tcp_error = ""
        self.schedule_file = ""
        self.wo_map: dict[str, str] = {}      # WO as written in the schedule -> TCP job code id
        self.line_map: dict[str, str] = {}    # normalized line name in the schedule -> cost code group id
        self.checks: dict[str, dict] = {}     # schedule WO -> {status, tcp, codes, groups, message, suggestions}
        self._load()

    # ---------------- persistence (plain JSON file, no database)
    def _load(self):
        if STATE_FILE.exists():
            try:
                s = json.loads(STATE_FILE.read_text())
                self.schedule = [Row.from_json(r) for r in s.get("schedule", [])]
                self.lines = s.get("lines", {})
                self.preview = s.get("preview", True)
                self.activity = s.get("activity", [])[:300]
                self.schedule_file = s.get("schedule_file", "")
                self.wo_map = s.get("wo_map", {})
                self.line_map = s.get("line_map", {})
                self.checks = s.get("checks", {})
            except Exception as e:  # noqa
                log.warning("state file unreadable: %s", e)

    def save(self):
        with self.lock:
            STATE_FILE.write_text(json.dumps({"schedule": [r.to_json() for r in self.schedule], "lines": self.lines,
                                              "preview": self.preview, "activity": self.activity[:300],
                                              "schedule_file": self.schedule_file, "wo_map": self.wo_map,
                                              "line_map": self.line_map, "checks": self.checks}, default=str))

    def note(self, kind: str, line: str, text: str):
        self.activity.insert(0, {"time": datetime.now().strftime("%b %d %H:%M:%S"), "kind": kind,
                                 "line": line, "text": text})
        del self.activity[300:]
        log.info("%s %s %s", kind, line, text)

    # ---------------- TCP sync
    def refresh(self):
        """Pull every cost code group and what is in it right now."""
        try:
            gs = self.tcp.groups()
            new = {}
            for g in gs:
                gid = str(g.get("id"))
                new[gid] = {"id": gid, "description": g.get("description") or f"Group {gid}",
                            "jobCodeAssignments": g.get("jobCodeAssignments") or 0,
                            "employeeAssignments": g.get("employeeAssignments") or 0,
                            "totalCostCodes": g.get("totalCostCodes") or 0, "codes": []}
                ln = self.lines.setdefault(gid, {"live_wo": None, "hold": False, "override": None, "error": "",
                                                 "is_line": None, "retry_at": 0})
                if ln.get("is_line") is None:
                    # groups named like "Line 1" are the line (kiosk) groups; in job code mode,
                    # groups not tied to any job code count as lines too
                    named = norm(new[gid]["description"]).startswith("line")
                    ln["is_line"] = named if self.tcp.wo_source == "level1" else (named or not new[gid]["jobCodeAssignments"])
            for gid, g in new.items():
                if self.lines[gid]["is_line"]:
                    try:
                        g["codes"] = self.tcp.group_codes(g["description"])
                    except TcpError as e:
                        self.lines[gid]["error"] = str(e)
            with self.lock:
                self.groups = new
            self.last_sync = datetime.now()
            self.tcp_error = ""
        except TcpError as e:
            self.tcp_error = str(e)
        except Exception as e:  # noqa
            self.tcp_error = f"{type(e).__name__}: {e}"

    def group_for(self, line_name: str) -> dict | None:
        n = norm(line_name)
        if n in self.line_map and self.line_map[n] in self.groups:
            return self.groups[self.line_map[n]]
        for g in self.groups.values():
            if norm(g["description"]) == n:
                return g
        return None

    def rows_for(self, gid: str) -> list[Row]:
        if gid not in self.groups:
            return []
        return [r for r in self.schedule if (self.group_for(r.line) or {}).get("id") == gid]

    def resolve(self, wo: str) -> str:
        """The TCP job code for a schedule WO: the manual mapping if there is one, else the same number."""
        return self.wo_map.get(wo, wo)

    # ---------------- schedule check
    def validate(self, only: list[str] | None = None):
        """Look every scheduled WO up in TCP and record ok / no cost codes / missing, with suggestions."""
        now = datetime.now()
        wos = sorted({r.wo for r in self.schedule if r.end > now}) if only is None else only
        all_jobs = None
        for wo in wos:
            tcp_id = self.resolve(wo)
            chk = {"status": "ok", "tcp": tcp_id, "mapped": wo in self.wo_map, "codes": [], "groups": [],
                   "message": "", "suggestions": []}
            try:
                if self.tcp.wo_source == "level1":
                    codes, groups = self.tcp.wo_cost_codes(tcp_id)
                    chk["codes"], chk["groups"] = codes, groups
                    if not codes:
                        chk["status"] = "missing"
                        chk["message"] = f"No active cost codes with Level 1 = {tcp_id} in TCP"
                        if all_jobs is None:
                            try:
                                all_jobs = self.tcp.level1_values()
                            except Exception:  # noqa
                                all_jobs = []
                        digits = "".join(ch for ch in wo if ch.isdigit())
                        chk["suggestions"] = [{"id": v, "description": f"Cost codes {v}\\..."} for v in all_jobs
                                              if (digits and digits in v) or norm(wo) in norm(v)][:6]
                    self.checks[wo] = chk
                    continue
                jc = self.tcp.job_code(tcp_id)
                if not jc:
                    chk["status"] = "missing"
                    chk["message"] = f"No job code {tcp_id} in TCP"
                    if all_jobs is None:
                        try:
                            all_jobs = self.tcp.get_all("/jobcodes", {"active": "true"})
                        except Exception:  # noqa
                            all_jobs = []
                    digits = "".join(ch for ch in wo if ch.isdigit())
                    sug = []
                    for j in all_jobs:
                        jid, desc = str(j.get("jobCodeId", "")), str(j.get("description") or "")
                        if (digits and (digits in jid or digits in desc)) or norm(wo) in norm(desc):
                            sug.append({"id": jid, "description": desc})
                    chk["suggestions"] = sug[:6]
                else:
                    codes, groups = self.tcp.wo_cost_codes(tcp_id)
                    chk["codes"], chk["groups"] = codes, groups
                    if not codes:
                        chk["status"] = "nocodes"
                        chk["message"] = (f"Job code {tcp_id} is in TCP but has no cost codes"
                                          + (f" (its groups {', '.join(groups)} are empty)" if groups else " (no cost code group attached)"))
            except Exception as e:  # noqa
                chk["status"], chk["message"] = "error", str(e)
            self.checks[wo] = chk
        self.save()
        return self.problems()

    def problems(self) -> list[dict]:
        now = datetime.now()
        out = []
        active_wos = {r.wo for r in self.schedule if r.end > now}
        for wo in sorted(active_wos):
            c = self.checks.get(wo)
            if c and c["status"] != "ok":
                lines = sorted({r.line for r in self.schedule if r.wo == wo})
                out.append({"type": "wo", "key": wo, "lines": lines, **c})
        for name in sorted({r.line for r in self.schedule if r.end > now}):
            if not self.group_for(name):
                out.append({"type": "line", "key": name, "status": "missing",
                            "message": f"No cost code group called {name} in TCP"})
        return out

    def now_row(self, gid, t=None):
        t = t or datetime.now()
        return next((r for r in self.rows_for(gid) if r.covers(t)), None)

    def next_row(self, gid, t=None):
        t = t or datetime.now()
        return next((r for r in self.rows_for(gid) if r.start > t), None)

    # ---------------- switching
    def desired_codes(self, wo: str, row: Row | None) -> tuple[list[dict], str]:
        if row and row.codes:
            all_codes = self.tcp.get_all("/costcodes", {"isActive": "true"})
            found, missing = [], []
            for want in row.codes:
                hit = next((c for c in all_codes if norm(c.get("fullName")) == norm(want) or norm(c.get("description")) == norm(want)), None)
                (found if hit else missing).append(hit or want)
            if missing:
                raise TcpError(404, "cost codes not found in TCP: " + ", ".join(missing))
            return [{"id": str(c.get("id")), "fullName": c.get("fullName") or "", "description": c.get("description") or ""}
                    for c in found], "listed in the schedule"
        tcp_id = self.resolve(wo)
        codes, groups = self.tcp.wo_cost_codes(tcp_id)
        if not codes:
            if self.tcp.wo_source == "level1":
                raise TcpError(404, f"no active cost codes with Level 1 = {tcp_id} in TCP")
            raise TcpError(404, f"job code {tcp_id} has no cost codes (its cost code groups: {', '.join(groups) or 'none'})")
        if self.tcp.wo_source == "level1":
            return codes, f"cost codes {tcp_id}\\..."
        return codes, "from job code " + wo + " groups " + ", ".join(groups)

    def apply(self, gid: str, wo: str, reason: str, row: Row | None = None):
        g = self.groups.get(gid)
        ln = self.lines[gid]
        name = g["description"] if g else gid
        try:
            want, source = self.desired_codes(wo, row)
            have = self.tcp.group_codes(g["description"])
            key = lambda c: norm(c.get("fullName") or c.get("id"))
            want_k, have_k = {key(c) for c in want}, {key(c) for c in have}
            remove = [c for c in have if key(c) not in want_k]
            add = [c for c in want if key(c) not in have_k]
            label = lambda cs: ", ".join(c.get("fullName") or c.get("description") or c.get("id") for c in cs) or "nothing"
            if self.preview:
                self.note("preview", name, f"WO {wo} ({reason}): would remove {label(remove)}; add {label(add)}. Cost codes {source}.")
            else:
                if remove or add:
                    self.tcp.swap_group(g, remove, add)
                self.note("switch", name, f"Now WO {wo} ({reason}). Removed {label(remove)}; added {label(add)}.")
                g["codes"] = self.tcp.group_codes(g["description"])
            ln["live_wo"], ln["error"], ln["retry_at"] = wo, "", 0
            ln["mode"] = "preview" if self.preview else "live"
        except Exception as e:  # noqa
            ln["error"] = str(e)
            ln["retry_at"] = time.time() + 120
            self.note("error", name, f"Could not switch to WO {wo}: {e}")
        self.save()

    def tick(self):
        if not self.tcp.configured:
            return
        now = datetime.now()
        with self.lock:
            for gid, ln in list(self.lines.items()):
                g = self.groups.get(gid)
                if not g or not ln.get("is_line") or ln.get("hold"):
                    continue
                if ln.get("override"):
                    target, row, reason = ln["override"], None, "set by hand"
                else:
                    row = self.now_row(gid, now)
                    if not row:
                        continue                         # gap: keep the last WO (change over time stays on it)
                    target, reason = row.wo, f"schedule {row.start:%H:%M} to {row.end:%H:%M}"
                chk = self.checks.get(target)
                if not ln.get("override") and chk and chk["status"] in ("missing", "nocodes"):
                    msg = f"WO {target} is not ready in TCP: {chk['message']}. Map it in the schedule check."
                    if ln.get("error") != msg:
                        ln["error"] = msg
                        self.note("error", g["description"], msg)
                        self.save()
                    continue
                same_mode = ln.get("mode") == ("preview" if self.preview else "live")
                if target == ln.get("live_wo") and same_mode:
                    continue
                if ln.get("error") and time.time() < ln.get("retry_at", 0):
                    continue
                self.apply(gid, target, reason, row)

    # ---------------- background loop
    def start(self, every: int = 30):
        def loop():
            n = 0
            while True:
                try:
                    if n % 4 == 0:
                        self.refresh()                  # full TCP read every 2 minutes
                    if n % 20 == 10 and self.tcp.configured:
                        bad = [p["key"] for p in self.problems() if p["type"] == "wo"]
                        if bad:
                            self.validate(bad)          # someone may have fixed it in TCP since
                    self.tick()
                except Exception:  # noqa
                    log.exception("loop error")
                n += 1
                time.sleep(every)
        threading.Thread(target=loop, daemon=True).start()

    # ---------------- view model for the UI
    def view(self):
        now = datetime.now()
        lines = []
        for gid, g in sorted(self.groups.items(), key=lambda kv: kv[1]["description"].lower()):
            ln = self.lines.get(gid, {})
            cur, nxt = self.now_row(gid, now), self.next_row(gid, now)
            rows = [r for r in self.rows_for(gid) if r.end > now - timedelta(hours=12) and r.start < now + timedelta(hours=36)]
            lines.append({
                "id": gid, "name": g["description"], "is_line": bool(ln.get("is_line")),
                "jobCodeAssignments": g["jobCodeAssignments"], "codes": g["codes"],
                "live_wo": ln.get("live_wo"), "mode": ln.get("mode"), "hold": ln.get("hold", False),
                "override": ln.get("override"), "error": ln.get("error", ""),
                "now": cur.to_json() if cur else None, "next": nxt.to_json() if nxt else None,
                "rows": [r.to_json() for r in rows],
            })
        unmatched = sorted({r.line for r in self.schedule if not self.group_for(r.line)})
        now_wos = sorted({r.wo for r in self.schedule if r.end > now})
        today = [r for r in self.schedule if r.end > now and r.start < now + timedelta(days=1)]
        return {"now": now.isoformat(), "preview": self.preview, "configured": self.tcp.configured,
                "tcp_error": self.tcp_error, "last_sync": self.last_sync.isoformat() if self.last_sync else None,
                "company": self.tcp.company_id, "lines": lines, "unmatched": unmatched,
                "schedule_file": self.schedule_file, "schedule_count": len(self.schedule),
                "upcoming": len(today), "activity": self.activity[:120],
                "problems": self.problems(), "checked": sum(1 for w in now_wos if w in self.checks),
                "ok_count": sum(1 for w in now_wos if self.checks.get(w, {}).get("status") == "ok"),
                "wo_total": len(now_wos), "wo_map": self.wo_map,
                "line_map": {next((r.line for r in self.schedule if norm(r.line) == k), k): self.groups.get(v, {}).get("description", v)
                             for k, v in self.line_map.items()},
                "group_names": [{"id": gid, "name": g["description"]} for gid, g in sorted(self.groups.items(), key=lambda kv: kv[1]["description"].lower())]}
