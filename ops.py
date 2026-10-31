"""Floor operations on top of the schedule: who started which WO on which line, ending a WO or a shift,
line downtime with reason codes, units and cost per WO run, the SO-SKU report, and cost code creation.

Everything here is saved with the rest of the state (Neon when DATABASE_URL is set).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from tcp import TcpError

log = logging.getLogger("lineswitch.ops")

DEFAULTS = {
    # who decides when a WO starts on a line: "schedule" (start time in the file) or "setup" (the setup person taps Start)
    "start_mode": "schedule",
    # when a line switches WO, move everyone clocked in on it to the same step of the new WO
    "move_on_switch": True,
    # average loaded labor rate used for cost when TCP does not return a rate
    "labor_rate": 25.0,
    # steps (level 2) every WO gets, and the line positions (level 3) under FILLING
    "roles": ["SETUP", "VERIFY", "FILLING", "QC", "LINE REPAIR", "CHANGE OVER"],
    "functions": [],
    # steps that are not counted in unit filling cost
    "exclude_from_cost": ["LINE REPAIR"],
    # create the cost codes in TCP for a WO in the schedule that doesn't have them yet
    "auto_create": False,
    "downtime_reasons": ["Machine jam", "Labeler issue", "Material shortage", "Changeover", "Quality hold", "Waiting on maintenance", "Other"],
}


def _now():
    return datetime.now().replace(microsecond=0)


def _iso(d):
    return d.isoformat() if d else None


def _dt(s):
    return datetime.fromisoformat(s) if s else None


class Ops:
    def __init__(self, engine):
        self.e = engine
        self.settings = dict(DEFAULTS)
        self.runs: list[dict] = []       # one per WO run on a line: start, end, who, units
        self.downtime: list[dict] = []   # line stops with reason codes

    # ------------------------------------------------------------ persistence
    def dump(self):
        return {"settings": self.settings, "runs": self.runs[-3000:], "downtime": self.downtime[-3000:]}

    def load(self, d: dict | None):
        d = d or {}
        self.settings = {**DEFAULTS, **(d.get("settings") or {})}
        self.runs = d.get("runs") or []
        self.downtime = d.get("downtime") or []

    def set_settings(self, patch: dict):
        for k, v in (patch or {}).items():
            if k not in DEFAULTS:
                continue
            if k == "labor_rate":
                v = max(0.0, float(v))
            elif k in ("move_on_switch", "auto_create"):
                v = bool(v)
            elif k == "start_mode":
                v = "setup" if v == "setup" else "schedule"
            elif k in ("roles", "functions", "exclude_from_cost", "downtime_reasons"):
                v = [str(x).strip() for x in (v or []) if str(x).strip()]
                if k != "downtime_reasons":
                    v = [x.upper() for x in v]
            self.settings[k] = v
        self.e.note("settings", "System", "Settings updated")
        self.e.save()

    # ------------------------------------------------------------ helpers
    def _group(self, gid):
        g = self.e.groups.get(gid)
        if not g:
            raise ValueError("that line is not in TCP anymore")
        return g

    def open_run(self, line: str):
        return next((r for r in reversed(self.runs) if r["line"] == line and not r.get("end")), None)

    def _row_details(self, line: str, wo: str, at: datetime):
        """SKU, units and workers for this WO from the schedule row closest to now on that line."""
        from engine import norm
        rows = [r for r in self.e.schedule if norm(r.line) == norm(line) and r.wo == wo]
        if not rows:
            return {}
        r = min(rows, key=lambda x: abs((x.start - at).total_seconds()))
        return {k: getattr(r, k, "") for k in ("sku", "sku_desc", "units", "workers", "so_sku")}

    def _start_run(self, line: str, wo: str, by: str, how: str, at: datetime | None = None):
        at = at or _now()
        cur = self.open_run(line)
        if cur and cur["wo"] == wo:
            return cur
        if cur:
            cur["end"], cur["end_reason"] = _iso(at), "switched to WO " + wo
        run = {"id": uuid.uuid4().hex[:10], "line": line, "wo": wo, "start": _iso(at), "end": None,
               "started_by": by or "", "how": how, "end_reason": "", "units_accepted": None, "units_used": None,
               "notes": "", **self._row_details(line, wo, at)}
        self.runs.append(run)
        return run

    # called by the engine every time a line switches WO in live mode
    def on_switch(self, gid: str, old_wo: str | None, new_wo: str, reason: str):
        g = self.e.groups.get(gid) or {}
        name = g.get("description", gid)
        how = "by hand" if "hand" in reason else ("setup" if "setup" in reason else "schedule")
        by = (self.e.lines.get(gid) or {}).get("started_by", "") if how == "setup" else ""
        at = None
        if how == "schedule":
            row = self.e.now_row(gid)
            if row and row.wo == new_wo and row.start >= _now() - timedelta(hours=12):
                at = row.start.replace(second=0, microsecond=0)   # the WO started at its scheduled time
        self._start_run(name, new_wo, by, how, at)
        if self.settings.get("move_on_switch") and old_wo and old_wo != new_wo:
            try:
                moved = self.move_workers(gid, old_wo, new_wo)
                if moved:
                    self.e.note("switch", name, f"Moved {len(moved)} people from WO {old_wo} to WO {new_wo}: " + ", ".join(moved))
            except Exception as ex:  # noqa
                self.e.note("error", name, f"Could not move people to WO {new_wo}: {ex}")

    # ------------------------------------------------------------ people
    def people(self, gid: str) -> list[dict]:
        g = self._group(gid)
        jc = self.e.tcp.line_job_code(g["description"])
        if not jc:
            raise TcpError(404, f"no job code in TCP has the {g['description']} cost code group attached")
        names = self.e.tcp.employees()
        out = []
        for s in self.e.tcp.on_line(jc):
            eid = str(s.get("employeeId"))
            out.append({"employeeId": eid, "name": names.get(eid, eid), "costCode": s.get("costCode") or "",
                        "timeIn": s.get("timeIn"), "segment": s.get("id")})
        return sorted(out, key=lambda x: x["name"])

    def move_workers(self, gid: str, old_wo: str, new_wo: str) -> list[str]:
        """Everyone on the line still on an old WO step moves to the same step of the new WO, or its first step."""
        new_codes = [c["fullName"] for c in self.e.tcp.wo_cost_codes(new_wo)[0]]
        if not new_codes:
            return []
        moved, rows = [], []
        for p in self.people(gid):
            code = p["costCode"]
            if not code or not code.upper().startswith(str(old_wo).upper() + "\\"):
                continue
            rest = code.split("\\", 1)[1].upper()
            target = next((c for c in new_codes if c.split("\\", 1)[1].upper() == rest), None) \
                or next((c for c in new_codes if c.split("\\", 1)[1].split("\\")[0].upper() == rest.split("\\")[0]), None) \
                or new_codes[0]
            rows.append({"operationType": "changeCostCode", "employeeId": int(p["employeeId"]), "costCodeName": target,
                         "timeOut": self.e.tcp.stamp()})
            moved.append(f"{p['name']} to {target}")
        if rows:
            self.e.tcp.punch(rows)
        return moved

    def clock_off(self, gid: str, why: str) -> list[str]:
        rows, names = [], []
        for p in self.people(gid):
            rows.append({"operationType": "clockOut", "employeeId": int(p["employeeId"]), "timeOut": self.e.tcp.stamp()})
            names.append(p["name"])
        if rows and not self.e.preview:
            self.e.tcp.punch(rows)
        return names

    # ------------------------------------------------------------ setup person and line actions
    def todays_wos(self, gid: str) -> list[dict]:
        """WOs scheduled on this line for today (the setup person picks from these)."""
        now = _now()
        day0, day1 = now.replace(hour=0, minute=0, second=0), now.replace(hour=23, minute=59, second=59)
        out = []
        for r in sorted(self.e.rows_for(gid), key=lambda x: x.start):
            if r.end >= day0 - timedelta(hours=6) and r.start <= day1:
                chk = self.e.checks.get(self.e.resolve(r.wo)) or self.e.checks.get(r.wo) or {}
                out.append({"wo": r.wo, "start": _iso(r.start), "end": _iso(r.end), "note": r.note,
                            "sku": getattr(r, "sku", ""), "sku_desc": getattr(r, "sku_desc", ""), "units": getattr(r, "units", None),
                            "workers": getattr(r, "workers", None), "ok": chk.get("status", "ok") == "ok",
                            "steps": [c.get("fullName") for c in chk.get("codes", [])]})
        # one entry per WO
        seen, uniq = set(), []
        for x in out:
            if x["wo"] not in seen:
                seen.add(x["wo"])
                uniq.append(x)
        return uniq

    def start_wo(self, gid: str, wo: str, by: str):
        g = self._group(gid)
        allowed = [x["wo"] for x in self.todays_wos(gid)]
        if allowed and wo not in allowed:
            raise ValueError(f"WO {wo} is not on {g['description']}'s schedule for today. Pick one of: {', '.join(allowed)}")
        ln = self.e.lines[gid]
        ln["override"], ln["hold"], ln["started_by"] = wo, False, by
        self.e.apply(gid, wo, f"started by setup{(' (' + by + ')') if by else ''}")
        if self.e.preview:
            self.e.note("preview", g["description"], f"Setup would start WO {wo}" + (f" ({by})" if by else ""))
        return self.open_run(g["description"])

    def end(self, gid: str, kind: str, by: str):
        """kind: 'wo' (WO End) or 'shift' (Shift/Day End). Closes the run and clocks everyone off the line."""
        g = self._group(gid)
        name = g["description"]
        try:
            off = self.clock_off(gid, kind)
        except Exception as ex:  # noqa
            off = []
            self.e.note("error", name, f"Could not clock people off the line: {ex}")
        run = self.open_run(name)
        if run and not self.e.preview:
            run["end"], run["end_reason"] = _iso(_now()), ("WO end" if kind == "wo" else "shift end") + (f" ({by})" if by else "")
        ln = self.e.lines[gid]
        ln["hold"], ln["override"] = True, None          # the line waits for the next setup or resume
        label = "WO end" if kind == "wo" else "Shift end"
        who = f" Clocked off: {', '.join(off)}." if off else " Nobody was clocked in."
        self.e.note("preview" if self.e.preview else "switch", name,
                    f"{label} for WO {run['wo'] if run else ln.get('live_wo')}" + (f" by {by}" if by else "") + "." +
                    (" Would clock off " + str(len(off)) + " people (preview)." if self.e.preview else who))
        self.e.save()
        return {"clocked_off": off}

    def down_start(self, gid: str, by: str):
        g = self._group(gid)
        name = g["description"]
        if any(d for d in self.downtime if d["line"] == name and not d.get("end")):
            raise ValueError(f"{name} is already down")
        run = self.open_run(name)
        d = {"id": uuid.uuid4().hex[:10], "line": name, "wo": (run or {}).get("wo") or self.e.lines[gid].get("live_wo") or "",
             "start": _iso(_now()), "end": None, "reason": "", "by": by or "", "notes": ""}
        self.downtime.append(d)
        self.e.note("hold", name, f"Line down on WO {d['wo']}" + (f", reported by {by}" if by else ""))
        self.e.save()
        return d

    def down_end(self, gid: str, reason: str, notes: str, by: str):
        g = self._group(gid)
        name = g["description"]
        d = next((x for x in reversed(self.downtime) if x["line"] == name and not x.get("end")), None)
        if not d:
            raise ValueError(f"{name} is not down")
        if not reason:
            raise ValueError("pick a downtime reason")
        d["end"], d["reason"], d["notes"] = _iso(_now()), reason, notes or ""
        mins = round((_dt(d["end"]) - _dt(d["start"])).total_seconds() / 60)
        self.e.note("resume", name, f"Line back up after {mins} min on WO {d['wo']}. Reason: {reason}")
        self.e.save()
        return d

    def edit_downtime(self, did: str, patch: dict):
        d = next((x for x in self.downtime if x["id"] == did), None)
        if not d:
            raise ValueError("that downtime record is gone")
        for k in ("reason", "notes", "start", "end"):
            if k in patch:
                d[k] = patch[k] or (None if k == "end" else "")
        self.e.save()
        return d

    # ------------------------------------------------------------ WO runs: units, hours, cost
    def edit_run(self, rid: str, patch: dict):
        r = next((x for x in self.runs if x["id"] == rid), None)
        if not r:
            raise ValueError("that work order run is gone")
        for k in ("units_accepted", "units_used"):
            if k in patch:
                v = patch[k]
                r[k] = None if v in (None, "") else float(v)
        for k in ("notes", "so_sku", "sku", "sku_desc"):
            if k in patch:
                r[k] = str(patch[k] or "")
        for k in ("units", "workers"):
            if k in patch:
                r[k] = None if patch[k] in (None, "") else float(patch[k])
        self.e.save()
        return r

    def labor(self, run: dict) -> dict:
        """Hours on this run from TCP, split by step: segments on the line's job code whose cost code starts with the WO."""
        g = next((x for x in self.e.groups.values() if x["description"] == run["line"]), None)
        if not g:
            return {"hours": 0, "by_step": {}, "people": 0, "error": "line not found in TCP"}
        jc = self.e.tcp.line_job_code(g["description"])
        a, z = _dt(run["start"]), _dt(run["end"]) or _now()
        segs = self.e.tcp.segments(a - timedelta(hours=1), z + timedelta(minutes=5), jc) if jc else []
        by_step, people, total = {}, set(), 0.0
        for s in segs:
            code = (s.get("costCode") or "")
            if not code.upper().startswith(str(run["wo"]).upper() + "\\"):
                continue
            ti = _parse(s.get("timeIn"))
            to = _parse(s.get("timeOut")) or _now()
            if not ti:
                continue
            ti, to = max(ti, a), min(to, z)
            h = max(0.0, (to - ti).total_seconds() / 3600)
            if not h:
                continue
            step = code.split("\\", 1)[1]
            by_step[step] = by_step.get(step, 0) + h
            people.add(str(s.get("employeeId")))
            total += h
        return {"hours": round(total, 2), "by_step": {k: round(v, 2) for k, v in by_step.items()}, "people": len(people)}

    def run_report(self, r: dict, with_labor: bool = True) -> dict:
        out = dict(r)
        excl = [x.upper() for x in self.settings.get("exclude_from_cost", [])]
        rate = float(self.settings.get("labor_rate") or 0)
        if with_labor:
            try:
                lab = self.labor(r)
            except Exception as ex:  # noqa
                lab = {"hours": 0, "by_step": {}, "people": 0, "error": str(ex)}
        else:
            lab = {"hours": 0, "by_step": {}, "people": 0}
        cost_hours = sum(h for s, h in lab["by_step"].items() if s.split("\\")[0].upper() not in excl)
        out.update(lab)
        out["cost_hours"] = round(cost_hours, 2)
        out["labor_cost"] = round(cost_hours * rate, 2)
        out["unit_cost"] = round(out["labor_cost"] / r["units_accepted"], 4) if r.get("units_accepted") else None
        dts = [d for d in self.downtime if d["line"] == r["line"] and d["wo"] == r["wo"]
               and _dt(d["start"]) >= _dt(r["start"]) - timedelta(minutes=1) and (not r.get("end") or _dt(d["start"]) <= _dt(r["end"]))]
        out["downtime_min"] = round(sum(((_dt(d["end"]) or _now()) - _dt(d["start"])).total_seconds() for d in dts) / 60)
        return out

    def runs_between(self, start: datetime, end: datetime, line: str = "") -> list[dict]:
        out = []
        for r in self.runs:
            a, z = _dt(r["start"]), _dt(r["end"]) or _now()
            if z >= start and a <= end and (not line or r["line"] == line):
                out.append(r)
        return sorted(out, key=lambda x: x["start"], reverse=True)

    def so_sku_report(self, runs: list[dict]) -> list[dict]:
        groups = {}
        for r in runs:
            k = r.get("so_sku") or "(no SO-SKU)"
            g = groups.setdefault(k, {"so_sku": k, "sku_desc": r.get("sku_desc") or "", "wos": [], "hours": 0.0, "cost_hours": 0.0,
                                      "labor_cost": 0.0, "units_accepted": 0.0, "units_used": 0.0, "budget_units": 0.0,
                                      "budget_workers": 0.0, "downtime_min": 0, "runs": 0})
            g["runs"] += 1
            if r["wo"] not in g["wos"]:
                g["wos"].append(r["wo"])
            for k2 in ("hours", "cost_hours", "labor_cost", "downtime_min"):
                g[k2] += r.get(k2) or 0
            g["units_accepted"] += r.get("units_accepted") or 0
            g["units_used"] += r.get("units_used") or 0
            g["budget_units"] += r.get("units") or 0
            g["budget_workers"] = max(g["budget_workers"], r.get("workers") or 0)
            g["sku_desc"] = g["sku_desc"] or r.get("sku_desc") or ""
        out = []
        for g in groups.values():
            g["unit_cost"] = round(g["labor_cost"] / g["units_accepted"], 4) if g["units_accepted"] else None
            g["units_vs_budget"] = round(g["units_accepted"] / g["budget_units"] * 100, 1) if g["budget_units"] else None
            for k in ("hours", "cost_hours", "labor_cost"):
                g[k] = round(g[k], 2)
            out.append(g)
        return sorted(out, key=lambda x: x["so_sku"])

    # ------------------------------------------------------------ cost codes for a WO
    def planned_codes(self, wo: str) -> list[dict]:
        roles = self.settings.get("roles") or []
        funcs = self.settings.get("functions") or []
        out = []
        for role in roles:
            if role == "FILLING" and funcs:
                for f in funcs:
                    out.append({"level1": str(wo).upper(), "level2": role, "level3": f})
            else:
                out.append({"level1": str(wo).upper(), "level2": role})
        return out

    def create_codes(self, wo: str) -> dict:
        have = {c["fullName"].upper() for c in self.e.tcp.wo_cost_codes(wo)[0]}
        want = self.planned_codes(wo)
        missing = [c for c in want if "\\".join(c[k] for k in ("level1", "level2", "level3") if c.get(k)).upper() not in have]
        if not missing:
            return {"created": [], "already": len(want)}
        body = []
        for c in missing:
            name = " ".join(x.capitalize() for x in (c.get("level3") or c["level2"]).split())
            body.append({**c, "description": name[:50], "isActive": True, "scheduleOrg": ["default"]})
        self.e.tcp.create_cost_codes(body)
        self.e.tcp.drop_code_cache()
        made = ["\\".join(c[k] for k in ("level1", "level2", "level3") if c.get(k)) for c in missing]
        self.e.note("map", "Schedule", f"Created {len(made)} cost codes for WO {wo} in TCP: {', '.join(made)}")
        return {"created": made, "already": len(want) - len(missing)}


def _parse(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d.astimezone().replace(tzinfo=None) if d.tzinfo else d
    except ValueError:
        return None
