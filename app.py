"""LineSwitch web app: the UI plus a small JSON API. Run: uvicorn app:app"""
from __future__ import annotations

import base64
import hmac
import logging
import os
from datetime import datetime
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
        engine.note("schedule", "Schedule", f"{file.filename}: {len(rows)} work orders on {len(lines)} lines loaded")
        engine.save()
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
    ln["override"], ln["hold"] = wo, False
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
    ln["hold"], ln["override"], ln["retry_at"], ln["error"] = False, None, 0, ""
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


@app.get("/api/sample")
def sample():
    """A ready to edit schedule for today using the line names found in TCP."""
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    names = [g["description"] for gid, g in engine.groups.items() if engine.lines.get(gid, {}).get("is_line")] or ["Line 1", "Line 2", "Line 3"]
    out = ["Line,Work Order,Start,End,Cost Codes,Note"]
    for i, n in enumerate(names[:6]):
        a = now.replace(hour=5)
        b = now.replace(hour=18)
        c = now.replace(hour=23)
        out.append(f"{n},{112 + i * 10},{a:%Y-%m-%d %H:%M},{b:%Y-%m-%d %H:%M},,Day run")
        out.append(f"{n},{113 + i * 10},{b:%Y-%m-%d %H:%M},{c:%Y-%m-%d %H:%M},,Evening run")
    return Response("\n".join(out) + "\n", media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=lineswitch_schedule_template.csv"})
