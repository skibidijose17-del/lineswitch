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
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from store import Store
from tcp import Tcp, TcpError

log = logging.getLogger("lineswitch")


def _num(v):
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


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
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    sku: str = ""          # optional order details from the scheduler's file
    sku_desc: str = ""
    units: float | None = None      # production units planned (budget units)
    workers: float | None = None    # workers planned (budget workers)
    so_sku: str = ""                # Sales Order + SKU, links WOs together (387-100312)
    steps: list[str] = field(default_factory=list)   # optional: this WO's steps, SETUP, FILLING\CAPPER, QC

    def covers(self, t: datetime) -> bool:
        return self.start <= t < self.end

    def to_json(self):
        d = asdict(self)
        d["start"], d["end"] = self.start.isoformat(), self.end.isoformat()
        return d

    @staticmethod
    def from_json(d):
        return Row(d["line"], d["wo"], datetime.fromisoformat(d["start"]), datetime.fromisoformat(d["end"]),
                   d.get("codes") or [], d.get("note", ""), d.get("id") or uuid.uuid4().hex[:10],
                   d.get("sku", "") or "", d.get("sku_desc", "") or "", _num(d.get("units")), _num(d.get("workers")), d.get("so_sku", "") or "",
                   _steps(d.get("steps")))


HEAD = {
    "line": "line", "clockconfiguration": "line", "costcodegroup": "line", "kiosk": "line", "group": "line",
    "workorder": "wo", "wo": "wo", "wonumber": "wo", "jobcode": "wo", "job": "wo",
    "start": "start", "starttime": "start", "from": "start", "end": "end", "endtime": "end", "to": "end",
    "date": "date", "costcodes": "codes", "costcode": "codes", "note": "note", "notes": "note", "comment": "note",
    "sku": "sku", "item": "sku", "skudescription": "sku_desc", "description": "sku_desc", "product": "sku_desc", "productdescription": "sku_desc",
    "units": "units", "productionunits": "units", "plannedunits": "units", "budgetunits": "units", "qty": "units", "quantity": "units",
    "workers": "workers", "workersassigned": "workers", "numberofworkers": "workers", "budgetworkers": "workers", "crew": "workers", "headcount": "workers",
    "steps": "steps", "tasks": "steps", "activities": "steps", "wosteps": "steps",
    "sosku": "so_sku", "salesordersku": "so_sku", "salesorder": "so_sku", "so": "so_sku",
}
FORMATS = ["%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y %I:%M %p",
           "%m/%d/%y %H:%M", "%m/%d/%Y %I:%M:%S %p", "%Y-%m-%d %I:%M %p"]


def _steps(v) -> list[str]:
    """'Setup, Filling\\Capper; QC' -> ['SETUP', 'FILLING\\CAPPER', 'QC']. Also takes 'Filling/Capper' or 'Filling > Capper'."""
    if not v:
        return []
    items = v if isinstance(v, list) else str(v).replace(";", ",").replace("\n", ",").split(",")
    out = []
    for x in items:
        x = re.sub(r"\s*(\\\\|/|>)\s*", "\\\\", str(x).strip()).upper()
        if x and x not in out:
            out.append(x)
    return out


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
        so = str(r.get("so_sku") or "").strip()
        if so.endswith(".0"):
            so = so[:-2]
        rows.append(Row(str(r["line"]).strip(), wo, start, end, codes, str(r.get("note") or "").strip(),
                        sku=str(r.get("sku") or "").strip().removesuffix(".0"), sku_desc=str(r.get("sku_desc") or "").strip(),
                        units=_num(r.get("units")), workers=_num(r.get("workers")), so_sku=so, steps=_steps(r.get("steps"))))
    check_overlaps(rows)
    return rows


def check_overlaps(rows: list[Row]):
    rows.sort(key=lambda x: (norm(x.line), x.start))
    for a, b in zip(rows, rows[1:]):
        if norm(a.line) == norm(b.line) and b.start < a.end:
            raise ScheduleError(f"{a.line}: WO {a.wo} ({a.start:%b %d %I:%M %p} to {a.end:%I:%M %p}) and WO {b.wo} "
                                f"(starts {b.start:%I:%M %p}) overlap. Move one of them so they don't share time.")


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
        self.schedule_updated = ""            # when the schedule last changed, and how
        self.store = Store()
        from ops import Ops
        self.ops = Ops(self)
        self._load()

    # ---------------- persistence (Neon Postgres when DATABASE_URL is set, else state.json)
    def _load(self):
        s = self.store.load()
        if not s:
            return
        try:
            self.schedule = [Row.from_json(r) for r in s.get("schedule", [])]
            self.lines = s.get("lines", {})
            self.preview = s.get("preview", True)
            self.activity = s.get("activity", [])[:300]
            self.schedule_file = s.get("schedule_file", "")
            self.wo_map = s.get("wo_map", {})
            self.line_map = s.get("line_map", {})
            self.checks = s.get("checks", {})
            self.schedule_updated = s.get("schedule_updated", "")
            self.ops.load(s.get("ops"))
        except Exception as e:  # noqa
            log.warning("saved state unreadable: %s", e)

    def save(self):
        with self.lock:
            self.store.save({"schedule": [r.to_json() for r in self.schedule], "lines": self.lines,
                             "preview": self.preview, "activity": self.activity[:300],
                             "schedule_file": self.schedule_file, "wo_map": self.wo_map,
                             "line_map": self.line_map, "checks": self.checks,
                             "schedule_updated": self.schedule_updated, "ops": self.ops.dump()})

    def version(self, reason: str):
        self.store.version([r.to_json() for r in self.schedule], reason)

    # ---------------- editing the schedule in the app
    @staticmethod
    def _row_from(d: dict, keep_id: str | None = None) -> Row:
        line, wo = str(d.get("line") or "").strip(), str(d.get("wo") or "").strip()
        if not line:
            raise ScheduleError("pick a line")
        if not wo:
            raise ScheduleError("type a work order number")
        try:
            start, end = datetime.fromisoformat(str(d["start"])), datetime.fromisoformat(str(d["end"]))
        except Exception:  # noqa
            raise ScheduleError("start and end need a date and a time")
        if end <= start:
            end += timedelta(days=1)                       # 10 PM to 2 AM
        if end - start > timedelta(hours=48):
            raise ScheduleError("a work order can run 48 hours at most")
        codes = d.get("codes") or []
        if isinstance(codes, str):
            codes = [c.strip() for c in codes.replace(";", ",").split(",") if c.strip()]
        r = Row(line, wo, start.replace(second=0, microsecond=0), end.replace(second=0, microsecond=0), codes, str(d.get("note") or "").strip(),
                sku=str(d.get("sku") or "").strip(), sku_desc=str(d.get("sku_desc") or "").strip(),
                units=_num(d.get("units")), workers=_num(d.get("workers")), so_sku=str(d.get("so_sku") or "").strip(),
                steps=_steps(d.get("steps")))
        if keep_id:
            r.id = keep_id
        return r

    def _commit(self, rows: list[Row], text: str, line: str):
        check_overlaps(rows)                               # raises before anything changes
        with self.lock:
            self.schedule = sorted(rows, key=lambda r: (r.line.lower(), r.start))
            self.schedule_updated = datetime.now().strftime("%b %d %I:%M %p") + ", " + text[0].lower() + text[1:]
            self.note("schedule", line, text)
            self.save()
        self.version(text)

    def replace_line(self, line: str, rows: list[Row]):
        """Save one line exactly as edited in the app. Other lines are untouched."""
        for r in rows:
            r.line = line
            if r.end <= r.start:
                r.end += timedelta(days=1)
            if r.end - r.start > timedelta(hours=48):
                raise ScheduleError(f"WO {r.wo} runs longer than 48 hours. Check its start and end.")
        keep = [x for x in self.schedule if norm(x.line) != norm(line)]
        self._commit(keep + rows, f"Saved {line} with {len(rows)} work order{'s' if len(rows) != 1 else ''}", line)

    def add_row(self, d: dict) -> Row:
        r = self._row_from(d)
        self._commit(list(self.schedule) + [r], f"Added WO {r.wo} on {r.line}, {r.start:%b %d %I:%M %p} to {r.end:%I:%M %p}", r.line)
        return r

    def update_row(self, rid: str, d: dict) -> Row:
        old = next((x for x in self.schedule if x.id == rid), None)
        if not old:
            raise ScheduleError("that work order is no longer in the schedule")
        r = self._row_from({**old.to_json(), **d}, keep_id=rid)
        rows = [r if x.id == rid else x for x in self.schedule]
        self._commit(rows, f"Changed {old.line} WO {old.wo} to WO {r.wo} on {r.line}, {r.start:%b %d %I:%M %p} to {r.end:%I:%M %p}", r.line)
        return r

    def delete_row(self, rid: str):
        old = next((x for x in self.schedule if x.id == rid), None)
        if not old:
            raise ScheduleError("that work order is no longer in the schedule")
        self._commit([x for x in self.schedule if x.id != rid], f"Removed WO {old.wo} from {old.line} ({old.start:%b %d %I:%M %p})", old.line)

    def move_row(self, rid: str, direction: int):
        """Swap a WO with the one before or after it on the same line. The time slots stay, the work orders trade places."""
        row = next((x for x in self.schedule if x.id == rid), None)
        if not row:
            raise ScheduleError("that work order is no longer in the schedule")
        same = sorted([x for x in self.schedule if norm(x.line) == norm(row.line)], key=lambda x: x.start)
        i = same.index(row)
        j = i + (1 if direction > 0 else -1)
        if j < 0 or j >= len(same):
            raise ScheduleError("nothing to swap with in that direction")
        a, b = same[i], same[j]
        for k in ("wo", "codes", "note"):
            va, vb = getattr(a, k), getattr(b, k)
            setattr(a, k, vb)
            setattr(b, k, va)
        self._commit(list(self.schedule), f"Swapped WO {b.wo} and WO {a.wo} on {row.line}", row.line)

    def restore(self, vid: int):
        rows = self.store.version_rows(vid)
        if rows is None:
            raise ScheduleError("that saved version was not found")
        self._commit([Row.from_json(r) for r in rows], f"Restored the schedule saved as version {vid}", "Schedule")

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
                    named = norm(new[gid]["description"]).startswith(("line", "preweigh", "compounding", "area"))
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
                    steps, desc = self.steps_for(wo)
                    if steps:
                        # the scheduler listed this WO's steps: create the ones TCP doesn't have yet
                        have = {norm(c["fullName"]) for c in codes}
                        if any(norm(f"{tcp_id}\\{st}") not in have for st in steps):
                            try:
                                self.ops.create_codes(tcp_id, steps, desc)
                                codes, groups = self.tcp.wo_cost_codes(tcp_id)
                            except Exception as ex:  # noqa
                                chk["message"] = f"could not create its cost codes in TCP: {ex}"
                    elif not codes and self.ops.settings.get("auto_create"):
                        try:
                            self.ops.create_codes(tcp_id)
                            codes, groups = self.tcp.wo_cost_codes(tcp_id)
                        except Exception as ex:  # noqa
                            chk["message"] = f"could not create its cost codes in TCP: {ex}"

                    chk["codes"], chk["groups"] = codes, groups
                    if not codes:
                        chk["status"] = "missing"
                        chk["message"] = chk["message"] or f"No active cost codes with Level 1 = {tcp_id} in TCP"
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
    def with_down(self, steps: list[str]) -> list[str]:
        """Every WO also gets the Line Down step, so line down time is recorded in TCP against the WO."""
        step = (self.ops.settings.get("line_down_step") or "").strip().upper()
        return steps + [step] if steps and step and step not in steps else steps

    def steps_for(self, wo: str) -> tuple[list[str], str]:
        """The steps the schedule lists for a WO (first row that has them), and its SKU description."""
        steps, desc = [], ""
        for r in sorted(self.schedule, key=lambda x: x.start):
            if norm(r.wo) == norm(wo) and r.steps:
                steps += [st for st in r.steps if st not in steps]   # Pre-Weigh, Compounding and the line each add theirs
                desc = desc or r.sku_desc
        return self.with_down(steps), desc

    def row_for(self, gid: str, wo: str) -> Row | None:
        """Today's schedule row for this WO on this line, so a setup start uses that line's own steps."""
        now = datetime.now()
        rows = [r for r in self.rows_for(gid) if norm(r.wo) == norm(wo) and r.end > now - timedelta(hours=12)]
        return min(rows, key=lambda r: abs((r.start - now).total_seconds()), default=None)

    def desired_codes(self, wo: str, row: Row | None) -> tuple[list[dict], str]:
        steps = (self.with_down(row.steps) if row and row.steps else self.steps_for(wo)[0])
        if steps and not (row and row.codes):
            tcp_id = self.resolve(wo)
            codes = self.tcp.wo_cost_codes(tcp_id)[0]
            names = {norm(f"{tcp_id}\\{st}") for st in steps}
            pick = [c for c in codes if norm(c["fullName"]) in names]
            if not pick:
                # the scheduler listed this area's steps but TCP doesn't have them yet: don't fall back to every code of the WO
                raise TcpError(404, f"WO {tcp_id} steps are not in TCP yet: {', '.join(steps)}")
            if pick:
                order = [norm(f"{tcp_id}\\{st}") for st in steps]
                pick.sort(key=lambda c: order.index(norm(c["fullName"])))
                missing = [st for st in steps if norm(f"{tcp_id}\\{st}") not in {norm(c["fullName"]) for c in pick}]
                return pick, "steps from the schedule" + (f" (not in TCP yet: {', '.join(missing)})" if missing else "")
        if row and row.codes:
            all_codes = self.tcp._all_codes()
            found, missing = [], []
            for want in row.codes:
                hit = next((c for c in all_codes if norm(c.get("fullName")) == norm(want) or norm(c.get("description")) == norm(want)), None)
                (found if hit else missing).append(hit or want)
            if missing:
                raise TcpError(404, "cost codes not found in TCP: " + ", ".join(missing))
            return [{"id": str(c.get("id")), "fullName": c.get("fullName") or "", "description": c.get("description") or "",
                     "isActive": c.get("isActive", True) is not False} for c in found], "listed in the schedule"
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
        with self.lock:   # the tablet and the switch timer can both switch a line; one at a time
            return self._apply(gid, wo, reason, row)

    def _apply(self, gid: str, wo: str, reason: str, row: Row | None = None):
        g = self.groups.get(gid)
        ln = self.lines[gid]
        name = g["description"] if g else gid
        old_wo = ln.get("live_wo") if ln.get("mode") == "live" else None
        try:
            want, source = self.desired_codes(wo, row)
            have = self.tcp.group_codes(g["description"])
            key = lambda c: norm(c.get("fullName") or c.get("id"))
            want_k, have_k = {key(c) for c in want}, {key(c) for c in have}
            add = [c for c in want if key(c) not in have_k]
            turn_on = [c for c in want if c.get("isActive") is False or not self.tcp.is_active(c.get("fullName", ""))]
            # Old WOs stay on the line and are turned off, not removed: TCP only lets a manager edit a punch
            # while its cost code is on the line, and an inactive code is hidden at the kiosk.
            old = [c for c in have if key(c) not in want_k and self.tcp.is_active(c.get("fullName", ""))]
            label = lambda cs: ", ".join(c.get("fullName") or c.get("description") or c.get("id") for c in cs) or "nothing"
            if self.preview:
                if reason != "tidy":
                    self.note("preview", name, f"WO {wo} ({reason}): would turn off {label(old)}; add {label(add)}"
                              + (f"; turn on {label(turn_on)}" if turn_on else "") + f". Cost codes {source}.")
            else:
                if turn_on:
                    self.tcp.set_active(turn_on, True)
                if add:
                    self.tcp.swap_group(g, [], add)
                ln["live_wo"], ln["error"], ln["retry_at"], ln["mode"] = wo, "", 0, "live"
                stuck = self.ops.on_switch(gid, old_wo, wo, reason)   # WO run record, and move people to the new WO
                off, kept = self.retire(gid, old, stuck)
                if reason == "tidy":
                    if off:
                        self.note("switch", name, f"Turned off {label(off)}, nobody is punched in on it now.")
                else:
                    self.note("switch", name, f"Now WO {wo} ({reason}). Added {label(add)}"
                              + (f", turned on {label(turn_on)}" if turn_on else "")
                              + f"; turned off {label(off)}.{kept}")
                g["codes"] = self.tcp.group_codes(g["description"])
            ln["live_wo"], ln["error"], ln["retry_at"] = wo, "", 0
            ln["mode"] = "preview" if self.preview else "live"
        except Exception as e:  # noqa
            ln["error"] = str(e)
            ln["retry_at"] = time.time() + 120
            self.note("error", name, f"Could not switch to WO {wo}: {e}")
        self.save()

    def retire(self, gid: str, old: list[dict], stuck: list[dict]):
        """Turn off old WO codes on a line, except ones someone is still punched in on
        and ones whose WO is running on another line right now. Returns (turned off, note)."""
        if not old:
            return [], ""
        on_them = {norm(p["costCode"]) for p in stuck}
        busy = set()           # codes another line is using right now (same WO, same step)
        for k, l in self.lines.items():
            if k == gid or not l.get("is_line"):
                continue
            row = self.now_row(k)
            wos = {norm(w) for w in (l.get("live_wo"), l.get("override"), row.wo if row else None) if w}
            for c in (self.groups.get(k) or {}).get("codes") or []:
                if norm(str(c.get("fullName", "")).split("\\")[0]) in wos:
                    busy.add(norm(c.get("fullName", "")))
        wo_of = lambda c: norm(str(c.get("fullName", "")).split("\\")[0])
        held = [c for c in old if norm(c.get("fullName", "")) in on_them]
        shared = [c for c in old if c not in held and norm(c.get("fullName", "")) in busy]
        off = [c for c in old if c not in held and c not in shared]
        if off:
            self.tcp.set_active(off, False)
        lab = lambda cs: ", ".join(c.get("fullName", "") for c in cs)
        note = ""
        if held:
            note += f" Left {lab(held)} on so {', '.join(p['name'] for p in stuck)} can punch out; it turns off once they move or clock off."
        if shared:
            note += f" Left {lab(shared)} on because that WO is running on another line; it turns off when that WO ends."
        return off, note

    def tidy(self, gid: str):
        """Turn off leftover codes from an old WO once nobody is punched in on them."""
        ln = self.lines.get(gid) or {}
        if self.preview or ln.get("mode") != "live" or not ln.get("live_wo"):
            return
        self.apply(gid, ln["live_wo"], "tidy")

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
                    target = ln["override"]
                    row = self.row_for(gid, target)
                    reason = ("started by setup" + (f" ({ln['started_by']})" if ln.get("started_by") else "")) if ln.get("started_by") else "set by hand"
                elif self.ops.settings.get("start_mode") == "setup":
                    continue                             # the setup person starts each WO from the line tablet
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
                "jobCodeAssignments": g["jobCodeAssignments"], "codes": [c for c in g["codes"] if c.get("isActive", True)],
                "live_wo": ln.get("live_wo"), "mode": ln.get("mode"), "hold": ln.get("hold", False),
                "override": ln.get("override"), "started_by": ln.get("started_by", ""), "error": ln.get("error", ""),
                "now": cur.to_json() if cur else None, "next": nxt.to_json() if nxt else None,
                "next_codes": (self.checks.get(self.resolve(nxt.wo), self.checks.get(nxt.wo, {})) or {}).get("codes", []) if nxt else [],
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
                "wo_total": len(now_wos), "wo_map": self.wo_map, "wo_source": self.tcp.wo_source,
                "storage": self.store.status(), "schedule_updated": self.schedule_updated,
                "settings": self.ops.settings,
                "runs_open": {r["line"]: r for r in self.ops.runs if not r.get("end")},
                "down_open": {d["line"]: d for d in self.ops.downtime if not d.get("end")},
                "wo_steps": {w: [c.get("fullName") or c.get("description") or c.get("id") for c in (self.checks.get(w) or {}).get("codes", [])]
                             for w in now_wos},
                "plan": [{"time": r.start.isoformat(), "line": r.line, "wo": r.wo, "end": r.end.isoformat(), "note": r.note}
                         for r in sorted(self.schedule, key=lambda x: x.start) if r.start > now][:40],
                "line_map": {next((r.line for r in self.schedule if norm(r.line) == k), k): self.groups.get(v, {}).get("description", v)
                             for k, v in self.line_map.items()},
                "group_names": [{"id": gid, "name": g["description"]} for gid, g in sorted(self.groups.items(), key=lambda kv: kv[1]["description"].lower())]}
