"""LineSwitch web app: the UI plus a small JSON API. Run: uvicorn app:app"""
from __future__ import annotations

import base64
import hmac
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response

from engine import Engine, Row, ScheduleError, parse_schedule
from tcp import API_LOG, Tcp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
HERE = Path(__file__).resolve().parent

tcp = Tcp()
engine = Engine(tcp)
app = FastAPI(title="LineSwitch")
USER = os.environ.get("BOARD_USER", "linesync")
PASS = os.environ.get("BOARD_PASSWORD", "")


@app.middleware("http")
async def auth(request: Request, call_next):
    if PASS and request.url.path != "/health":
        h = request.headers.get("authorization", "")
        ok = False
        if h.startswith("Basic "):
            try:
                u, _, p = base64.b64decode(h[6:]).decode().partition(":")
                ok = hmac.compare_digest(u, USER) and hmac.compare_digest(p, PASS)
            except Exception:  # noqa
                ok = False
        if not ok:
            return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="LineSwitch"'})
    return await call_next(request)


@app.on_event("startup")
def startup():
    engine.refresh()
    engine.start(int(os.environ.get("POLL_SECONDS", "30")))


@app.get("/health")
def health():
    return {"ok": True, "tcp_error": engine.tcp_error, "last_sync": str(engine.last_sync)}


@app.get("/", response_class=HTMLResponse)
def index():
    return (HERE / "index.html").read_text(encoding="utf-8")


@app.get("/api/state")
def state():
    return engine.view()


@app.post("/api/refresh")
def refresh():
    engine.refresh()
    engine.tick()
    return engine.view()


@app.post("/api/schedule")
async def upload(file: UploadFile = File(...)):
    data = await file.read()
    try:
        rows = parse_schedule(file.filename, data)
    except ScheduleError as e:
        engine.note("error", "Schedule", f"{file.filename} was not loaded: {e}")
        return JSONResponse({"ok": False, "message": f"{file.filename} was not loaded: {e}"}, 400)
    except Exception as e:  # noqa
        return JSONResponse({"ok": False, "message": f"Could not read {file.filename}: {e}"}, 400)
    with engine.lock:
        # replace rows for the lines in the new file, keep other lines' rows
        lines = {r.line.lower().replace(" ", "") for r in rows}
        keep = [r for r in engine.schedule if r.line.lower().replace(" ", "") not in lines]
        engine.schedule = sorted(keep + rows, key=lambda r: (r.line.lower(), r.start))
        engine.schedule_file = file.filename
        engine.schedule_updated = datetime.now().strftime("%b %d %H:%M") + f" uploaded {file.filename}"
        engine.note("schedule", "Schedule", f"{file.filename}: {len(rows)} work orders on {len(lines)} lines loaded")
        engine.save()
    engine.version(f"Uploaded {file.filename}")
    probs = engine.validate() if tcp.configured else []
    engine.tick()
    v = engine.view()
    wo_bad = [p for p in probs if p["type"] == "wo"]
    line_bad = [p for p in probs if p["type"] == "line"]
    msg = f"{len(rows)} work orders loaded."
    if probs:
        msg += f" Check needed: {len(wo_bad)} work orders and {len(line_bad)} lines do not match TCP. Map them in the schedule check."
    elif tcp.configured:
        msg += " Every work order and line was found in TCP."
    return {"ok": True, "message": msg, "state": v}


@app.get("/api/schedule")
def get_schedule():
    """The whole schedule, every row, for the schedule editor."""
    return {"file": engine.schedule_file, "updated": engine.schedule_updated,
            "rows": [r.to_json() for r in sorted(engine.schedule, key=lambda x: (x.line.lower(), x.start))]}


@app.put("/api/schedule/line")
async def put_line(request: Request):
    """Replace all rows of one line with what was edited in the UI.
    Body: {line, rows: [{wo, start, end, note, codes}]}, times as 'YYYY-MM-DDTHH:MM' in plant time."""
    b = await request.json()
    line = str(b.get("line", "")).strip()
    if not line:
        return JSONResponse({"ok": False, "message": "Missing line"}, 400)
    try:
        rows = []
        for i, r in enumerate(b.get("rows") or [], start=1):
            wo = str(r.get("wo", "")).strip()
            if not wo:
                raise ScheduleError(f"row {i} has no work order")
            try:
                start, end = datetime.fromisoformat(str(r["start"])), datetime.fromisoformat(str(r["end"]))
            except Exception:  # noqa
                raise ScheduleError(f"row {i} (WO {wo}) has a start or end time that can't be read")
            codes = r.get("codes") or []
            if isinstance(codes, str):
                codes = [c.strip() for c in codes.replace(";", ",").split(",") if c.strip()]
            num = lambda v: None if v in (None, "") else float(str(v).replace(",", ""))
            rows.append(Row(line, wo, start.replace(second=0, microsecond=0), end.replace(second=0, microsecond=0),
                            codes, str(r.get("note") or "").strip(), sku=str(r.get("sku") or "").strip(),
                            sku_desc=str(r.get("sku_desc") or "").strip(), units=num(r.get("units")),
                            workers=num(r.get("workers")), so_sku=str(r.get("so_sku") or "").strip()))
        engine.replace_line(line, rows)
    except ScheduleError as e:
        return JSONResponse({"ok": False, "message": str(e)}, 400)
    probs = engine.validate() if tcp.configured else []
    engine.tick()
    bad = [p for p in probs if p["type"] == "wo" and any(p["key"] == r.wo for r in rows)]
    msg = f"{line} saved with {len(rows)} work orders." + (f" {len(bad)} of them aren't in TCP yet, see Issues." if bad else "")
    return {"ok": True, "message": msg, "state": engine.view(), "schedule": get_schedule()}


@app.get("/api/schedule/versions")
def schedule_versions():
    """Every saved copy of the schedule (kept in Neon), newest first."""
    return {"storage": engine.store.status(), "versions": engine.store.versions()}


@app.post("/api/schedule/versions/{vid}/restore")
def restore_version(vid: int):
    try:
        engine.restore(vid)
    except ScheduleError as e:
        return JSONResponse({"ok": False, "message": str(e)}, 400)
    if tcp.configured:
        engine.validate()
    engine.tick()
    return {"ok": True, "message": f"Schedule restored to version {vid}", "state": engine.view(), "schedule": get_schedule()}


@app.post("/api/validate")
def validate():
    engine.refresh()
    engine.validate()
    engine.tick()
    return engine.view()


@app.post("/api/map/wo")
async def map_wo(request: Request):
    b = await request.json()
    wo, target = str(b.get("wo", "")).strip(), str(b.get("tcp", "")).strip()
    if not wo:
        return JSONResponse({"ok": False, "message": "Missing work order"}, 400)
    if target and target != wo:
        engine.wo_map[wo] = target
        engine.note("map", "Schedule", f"WO {wo} now maps to TCP job code {target}")
    else:
        engine.wo_map.pop(wo, None)
        engine.note("map", "Schedule", f"WO {wo} mapping removed")
    for ln in engine.lines.values():
        ln["error"], ln["retry_at"] = "", 0
    engine.validate([wo])
    engine.tick()
    return engine.view()


@app.post("/api/map/line")
async def map_line(request: Request):
    from engine import norm
    b = await request.json()
    name, gid = str(b.get("line", "")).strip(), str(b.get("group", "")).strip()
    if gid:
        engine.line_map[norm(name)] = gid
        engine.note("map", "Schedule", f"Line {name} now maps to cost code group {engine.groups.get(gid, {}).get('description', gid)}")
    else:
        engine.line_map.pop(norm(name), None)
    engine.save()
    engine.tick()
    return engine.view()


@app.delete("/api/schedule")
def clear_schedule():
    with engine.lock:
        engine.schedule = []
        engine.note("schedule", "Schedule", "Schedule cleared")
        engine.save()
    engine.version("Cleared the schedule")
    return engine.view()


def _line(gid):
    ln = engine.lines.get(gid)
    if ln is None:
        raise ValueError("unknown line")
    return ln


@app.post("/api/lines/{gid}/switch")
async def switch(gid: str, request: Request):
    wo = str((await request.json()).get("wo", "")).strip()
    if not wo:
        return JSONResponse({"ok": False, "message": "Type a work order number"}, 400)
    ln = _line(gid)
    ln["override"], ln["hold"], ln["started_by"] = wo, False, ""
    engine.apply(gid, wo, "set by hand")
    return engine.view()


@app.post("/api/lines/{gid}/hold")
def hold(gid: str):
    ln = _line(gid)
    ln["hold"] = True
    engine.note("hold", engine.groups.get(gid, {}).get("description", gid), "Automatic switching paused")
    engine.save()
    return engine.view()


@app.post("/api/lines/{gid}/resume")
def resume(gid: str):
    ln = _line(gid)
    ln["hold"], ln["override"], ln["retry_at"], ln["error"], ln["started_by"] = False, None, 0, "", ""
    engine.note("resume", engine.groups.get(gid, {}).get("description", gid), "Following the schedule again")
    engine.save()
    engine.tick()
    return engine.view()


@app.post("/api/lines/{gid}/toggle")
def toggle(gid: str):
    ln = _line(gid)
    ln["is_line"] = not ln.get("is_line")
    engine.save()
    if ln["is_line"]:
        engine.refresh()
    return engine.view()


@app.post("/api/preview")
async def preview(request: Request):
    on = bool((await request.json()).get("on"))
    engine.preview = on
    for ln in engine.lines.values():
        ln["retry_at"] = 0
    engine.note("mode", "System", "Preview mode on: nothing is changed in TCP" if on else "Live mode on: LineSwitch now changes TCP")
    engine.save()
    engine.tick()
    return engine.view()


@app.get("/api/wo/{wo}")
def wo_preview(wo: str):
    try:
        codes, groups = tcp.wo_cost_codes(wo)
        return {"ok": True, "wo": wo, "groups": groups, "codes": codes}
    except Exception as e:  # noqa
        return {"ok": False, "wo": wo, "message": str(e)}


@app.get("/api/apilog")
def apilog(errors: int = 0):
    rows = list(reversed(API_LOG))
    if errors:
        rows = [r for r in rows if not isinstance(r["status"], int) or r["status"] >= 400 or '"errors":[{' in r["response"]]
    return rows


@app.delete("/api/apilog")
def apilog_clear():
    API_LOG.clear()
    return []


@app.get("/api/schedule.csv")
def schedule_csv():
    """The schedule LineSwitch is following right now, as a CSV the client can open in Excel."""
    q = lambda v: '"' + str("" if v is None else (int(v) if isinstance(v, float) and v.is_integer() else v)).replace('"', "'") + '"'
    out = ["Line,Work Order,Start,End,SO-SKU,SKU,SKU Description,Units,Workers,Cost Codes,Note"]
    for r in sorted(engine.schedule, key=lambda x: (x.line.lower(), x.start)):
        out.append(",".join([q(r.line), q(r.wo), f"{r.start:%Y-%m-%d %H:%M}", f"{r.end:%Y-%m-%d %H:%M}", q(r.so_sku), q(r.sku),
                             q(r.sku_desc), q(r.units), q(r.workers), q(";".join(r.codes)), q(r.note)]))
    return Response("\n".join(out) + "\n", media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=lineswitch_current_schedule.csv"})


# ---------------------------------------------------------------- line tablet (setup person)
def _ok(fn):
    try:
        out = fn()
    except (ValueError, ScheduleError) as e:
        return JSONResponse({"ok": False, "message": str(e)}, 400)
    except Exception as e:  # noqa
        return JSONResponse({"ok": False, "message": f"TCP: {e}"}, 502)
    return {"ok": True, "result": out, "state": engine.view()}


@app.get("/api/lines/{gid}/today")
def line_today(gid: str):
    """The WOs the setup person can start on this line today, and who is clocked in on it right now."""
    out = {"wos": engine.ops.todays_wos(gid), "people": [], "people_error": ""}
    try:
        out["people"] = engine.ops.people(gid)
    except Exception as e:  # noqa
        out["people_error"] = str(e)
    return out


@app.post("/api/lines/{gid}/start")
async def line_start(gid: str, request: Request):
    b = await request.json()
    return _ok(lambda: engine.ops.start_wo(gid, str(b.get("wo", "")).strip(), str(b.get("by", "")).strip()))


@app.post("/api/lines/{gid}/end")
async def line_end(gid: str, request: Request):
    b = await request.json()
    kind = "shift" if b.get("kind") == "shift" else "wo"
    return _ok(lambda: engine.ops.end(gid, kind, str(b.get("by", "")).strip()))


@app.post("/api/lines/{gid}/down")
async def line_down(gid: str, request: Request):
    b = await request.json()
    return _ok(lambda: engine.ops.down_start(gid, str(b.get("by", "")).strip()))


@app.post("/api/lines/{gid}/up")
async def line_up(gid: str, request: Request):
    b = await request.json()
    return _ok(lambda: engine.ops.down_end(gid, str(b.get("reason", "")).strip(), str(b.get("notes", "")).strip(), str(b.get("by", "")).strip()))


@app.post("/api/lines/{gid}/move")
def line_move(gid: str):
    """Move everyone on the line who is still on an older WO to the line's current WO."""
    ln = engine.lines.get(gid) or {}
    def go():
        wo = ln.get("live_wo")
        if not wo:
            raise ValueError("this line has no work order yet")
        moved, fails = [], []
        olds = {(p["costCode"] or "").split("\\")[0] for p in engine.ops.people(gid)}
        for old in sorted(o for o in olds if o and o != wo):
            moved += engine.ops.move_workers(gid, old, wo)
            fails += engine.ops.last_failed
        if fails and not moved:
            raise ValueError("TCP would not move: " + "; ".join(fails))
        engine.tidy(gid)
        engine.note("switch", engine.groups.get(gid, {}).get("description", gid), f"Moved {len(moved)} people to WO {wo}" + (": " + ", ".join(moved) if moved else ""))
        engine.save()
        return moved
    return _ok(go)


# ---------------------------------------------------------------- WO runs, downtime, reports, settings
def _range(start: str, end: str):
    now = datetime.now()
    a = datetime.fromisoformat(start) if start else now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=6)
    z = datetime.fromisoformat(end) if end else now
    if len(end or "") <= 10 and end:
        z = z.replace(hour=23, minute=59, second=59)
    return a, z


@app.get("/api/runs")
def runs(start: str = "", end: str = "", line: str = "", hours: int = 1):
    a, z = _range(start, end)
    rows = [engine.ops.run_report(r, with_labor=bool(hours)) for r in engine.ops.runs_between(a, z, line)]
    return {"start": a.isoformat(), "end": z.isoformat(), "runs": rows, "so_sku": engine.ops.so_sku_report(rows),
            "rate": engine.ops.settings.get("labor_rate"), "exclude": engine.ops.settings.get("exclude_from_cost")}


@app.put("/api/runs/{rid}")
async def run_edit(rid: str, request: Request):
    b = await request.json()
    return _ok(lambda: engine.ops.run_report(engine.ops.edit_run(rid, b)))


@app.get("/api/runs.csv")
def runs_csv(start: str = "", end: str = "", line: str = ""):
    a, z = _range(start, end)
    rows = [engine.ops.run_report(r) for r in engine.ops.runs_between(a, z, line)]
    head = ["Line", "WO", "SO-SKU", "SKU", "SKU description", "Start", "End", "Started by", "People", "Hours", "Hours in cost",
            "Labor cost", "Units planned", "FG units accepted", "Units used", "Unit filling cost", "Downtime min"]
    out = [",".join(head)]
    q = lambda v: '"' + str("" if v is None else v).replace('"', "'") + '"'
    for r in rows:
        out.append(",".join(q(x) for x in [r["line"], r["wo"], r.get("so_sku"), r.get("sku"), r.get("sku_desc"), r["start"], r.get("end") or "",
                                            r.get("started_by"), r.get("people"), r.get("hours"), r.get("cost_hours"), r.get("labor_cost"),
                                            r.get("units"), r.get("units_accepted"), r.get("units_used"), r.get("unit_cost"), r.get("downtime_min")]))
    return Response("\n".join(out) + "\n", media_type="text/csv", headers={"Content-Disposition": "attachment; filename=lineswitch_work_orders.csv"})


@app.get("/api/downtime")
def downtime(start: str = "", end: str = ""):
    a, z = _range(start, end)
    rows = [d for d in engine.ops.downtime if datetime.fromisoformat(d["start"]) <= z and (not d.get("end") or datetime.fromisoformat(d["end"]) >= a)]
    for d in rows:
        e = datetime.fromisoformat(d["end"]) if d.get("end") else datetime.now()
        d["minutes"] = round((e - datetime.fromisoformat(d["start"])).total_seconds() / 60)
    return {"rows": sorted(rows, key=lambda x: x["start"], reverse=True), "reasons": engine.ops.settings.get("downtime_reasons")}


@app.put("/api/downtime/{did}")
async def downtime_edit(did: str, request: Request):
    b = await request.json()
    return _ok(lambda: engine.ops.edit_downtime(did, b))


@app.get("/api/settings")
def settings_get():
    return engine.ops.settings


@app.put("/api/settings")
async def settings_put(request: Request):
    b = await request.json()
    return _ok(lambda: engine.ops.set_settings(b))


@app.get("/api/wo/{wo}/plan")
def wo_plan(wo: str):
    """The cost codes LineSwitch would create in TCP for this WO, and which already exist."""
    have = {c["fullName"].upper() for c in tcp.wo_cost_codes(wo)[0]} if tcp.configured else set()
    plan = engine.ops.planned_codes(wo)
    return {"wo": wo, "codes": [{"name": "\\".join(c[k] for k in ("level1", "level2", "level3") if c.get(k)),
                                  "exists": "\\".join(c[k] for k in ("level1", "level2", "level3") if c.get(k)).upper() in have} for c in plan]}


@app.post("/api/wo/{wo}/create")
def wo_create(wo: str):
    def go():
        out = engine.ops.create_codes(wo)
        engine.validate([wo])
        engine.tick()
        return out
    return _ok(go)


@app.get("/api/sample")
def sample():
    """A ready to edit schedule for today using the line names found in TCP."""
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    names = [g["description"] for gid, g in engine.groups.items() if engine.lines.get(gid, {}).get("is_line")] or ["Line 1", "Line 2", "Line 3"]
    out = ["Line,Work Order,Start,End,SO-SKU,SKU,SKU Description,Units,Workers,Note"]
    for i, n in enumerate(names[:6]):
        a = now.replace(hour=5)
        b = now.replace(hour=18)
        c = now.replace(hour=23)
        out.append(f"{n},{112 + i * 10},{a:%Y-%m-%d %H:%M},{b:%Y-%m-%d %H:%M},387-1003{i}2,1003{i}2,Body Lotion 8oz,2400,6,Day run")
        out.append(f"{n},{113 + i * 10},{b:%Y-%m-%d %H:%M},{c:%Y-%m-%d %H:%M},387-1003{i}3,1003{i}3,Face Cream 2oz,1200,5,Evening run")
    return Response("\n".join(out) + "\n", media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=lineswitch_schedule_template.csv"})
