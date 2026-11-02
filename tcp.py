r"""TimeClock Plus Open API client for LineSwitch.

Only the calls LineSwitch needs:
  auth                 POST auth.api.tcplusondemand.com/oauth2/token (client credentials, 1 hour)
  cost code groups     GET  /v1/costcodegroups
                       PUT  /v1/costcodegroups/{id}   addCostCodes / removeCostCodes
  group members        GET  /v1/costcodegroupmembers?costCodeGroupDescriptions=
  cost codes           GET  /v1/costcodes?isActive=true   (WO 112 = codes 112\SETUP, 112\FILLING; default)
  job codes            GET  /v1/jobcodes/{jobCodeId}   (TCP_WO_SOURCE=jobcode: its costCodeGroups give the codes)
  health               GET  /v1/ping
Every call is kept in API_LOG so the UI can show exactly what was sent and what came back.
"""
from __future__ import annotations

import collections
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any

import requests

AUTH_URL = os.environ.get("TCP_AUTH_URL", "https://auth.api.tcplusondemand.com/oauth2/token")
BASE_URL = "https://api.tcplusondemand.com/v1"
SCOPE = "tcp-openapi/tcpopenapi.write tcp-openapi/tcpopenapi.read"

API_LOG: collections.deque = collections.deque(maxlen=400)


class TcpError(RuntimeError):
    def __init__(self, status, msg):
        super().__init__(f"{status}: {msg}")
        self.status = status


def _log(method, path, params, body, status, text, ms):
    API_LOG.append({"time": datetime.now().strftime("%H:%M:%S"), "method": method, "path": path,
                    "params": params, "body": body, "status": status, "response": (text or "")[:5000], "ms": ms})


class Tcp:
    def __init__(self):
        self.api_key = os.environ.get("TCP_API_KEY", "")
        self.client_id = os.environ.get("TCP_CLIENT_ID", "")
        self.client_secret = os.environ.get("TCP_CLIENT_SECRET", "")
        self.company_id = os.environ.get("TCP_COMPANY_ID", "1")
        self.base = os.environ.get("TCP_BASE_URL", BASE_URL).rstrip("/")
        # how a cost code is named in addCostCodes/removeCostCodes: fullName (default) or id
        self.code_ref = os.environ.get("TCP_COST_CODE_REF", "fullName")
        # where a WO's cost codes come from: level1 (default, cost codes like 112\SETUP)
        # or jobcode (members of the groups attached to job code 112)
        self.wo_source = os.environ.get("TCP_WO_SOURCE", "level1").strip().lower()
        self.s = requests.Session()
        self._token, self._exp = None, 0.0
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.client_id and self.client_secret)

    # ------------------------------------------------------------ auth + http
    def token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._exp - 120:
                return self._token
            t0 = time.time()
            r = requests.post(AUTH_URL, data={"grant_type": "client_credentials", "scope": SCOPE,
                                              "client_id": self.client_id, "client_secret": self.client_secret},
                              timeout=30)
            _log("POST", "/oauth2/token", None, {"grant_type": "client_credentials", "client_id": "(hidden)"},
                 r.status_code, "token received" if r.status_code == 200 else r.text, int((time.time() - t0) * 1000))
            if r.status_code != 200:
                raise TcpError(r.status_code, "TCP sign in failed: " + r.text[:200])
            j = r.json()
            self._token, self._exp = j["access_token"], time.time() + int(j.get("expires_in", 3600))
            return self._token

    def req(self, method: str, path: str, params: dict | None = None, body: Any = None) -> Any:
        if not self.configured:
            raise TcpError(0, "TCP keys are not set (TCP_API_KEY, TCP_CLIENT_ID, TCP_CLIENT_SECRET)")
        for attempt in range(1, 5):
            headers = {"x-api-key": self.api_key, "Authorization": f"Bearer {self.token()}",
                       "X-Tcp-CompanyId": str(self.company_id), "Accept": "application/json"}
            t0 = time.time()
            try:
                r = self.s.request(method, self.base + path, params=params, json=body, headers=headers, timeout=30)
            except requests.RequestException as e:
                _log(method, path, params, body, "network error", str(e), int((time.time() - t0) * 1000))
                time.sleep(min(2 ** attempt, 20))
                continue
            _log(method, path, params, body, r.status_code, r.text, int((time.time() - t0) * 1000))
            if r.status_code == 401 and attempt == 1:
                self._token = None
                continue
            if r.status_code in (429, 502, 503, 504) or (r.status_code == 500 and method != "POST"):   # a POST is not retried on 500: it may have half landed
                time.sleep(min(2 ** attempt, 20))
                continue
            if r.status_code >= 400:
                raise TcpError(r.status_code, f"{method} {path}: {_errors(r)}")
            j = r.json() if r.content else {}
            errs = j.get("errors") if isinstance(j, dict) else None
            if errs and method != "GET":
                raise TcpError(422, f"{method} {path} rejected by TCP: {_errors(r)}")
            return j
        raise TcpError(0, f"{method} {path} kept failing")

    def get_all(self, path, params=None, per_page=500):
        out, page = [], 1
        while True:
            j = self.req("GET", path, dict(params or {}, perPage=per_page, page=page)) or {}
            data = j.get("data") or []
            if isinstance(data, dict):
                data = [data]
            out.extend(data)
            if len(data) < per_page:
                return out
            page += 1

    # ------------------------------------------------------------ reads
    def ping(self):
        return self.req("GET", "/ping")

    def groups(self) -> list[dict]:
        return self.get_all("/costcodegroups")

    def group_codes(self, description: str) -> list[dict]:
        """Cost codes currently in one group: [{'id','fullName','description'}]."""
        rows = self.get_all("/costcodegroupmembers", {"costCodeGroupDescriptions": description})
        act = {str(c.get("fullName", "")).lower(): c.get("isActive", True) is not False for c in self._all_codes()}
        out = []
        for r in rows:
            if r.get("groupDescription") not in (None, description):
                continue
            for c in r.get("costCodes") or []:
                full = c.get("fullName") or ""
                on = c.get("isActive") if "isActive" in c else act.get(full.lower(), True)
                out.append({"id": str(c.get("id", "")), "fullName": full,
                            "description": c.get("description") or "", "isActive": on is not False})
        return out

    def job_code(self, wo: str) -> dict | None:
        try:
            j = self.req("GET", f"/jobcodes/{wo}")
        except TcpError as e:
            if e.status == 404:
                return None
            raise
        d = j.get("data")
        if isinstance(d, list):
            d = d[0] if d else None
        return d

    def _all_codes(self) -> list[dict]:
        now = time.time()
        if getattr(self, "_codes_cache", None) and now - self._codes_at < 60:
            return self._codes_cache
        rows = self.get_all("/costcodes")            # active and inactive: finished WOs are turned off, not deleted
        self._codes_cache, self._codes_at = rows, now
        return rows

    @staticmethod
    def _parts(c: dict) -> list[str]:
        full = str(c.get("fullName") or "")
        return [p for p in full.split("\\") if p] if full else \
            [str(c.get(f"level{i}") or "") for i in range(1, 6) if c.get(f"level{i}")]

    def level1_codes(self, wo: str) -> list[dict]:
        """Cost codes whose Level 1 is the WO and that have a Level 2 (112\\SETUP, not the bare 112 node).
        Inactive ones are included (with isActive False): LineSwitch turns them back on when the WO runs again."""
        out = []
        for c in self._all_codes():
            p = self._parts(c)
            if len(p) >= 2 and p[0].strip().lower() == str(wo).strip().lower():
                out.append({"id": str(c.get("id", "")), "fullName": c.get("fullName") or "\\".join(p),
                            "description": c.get("description") or "", "isActive": c.get("isActive", True) is not False})
        return out

    def is_active(self, full_name: str) -> bool:
        k = str(full_name).lower()
        for c in self._all_codes():
            if str(c.get("fullName", "")).lower() == k:
                return c.get("isActive", True) is not False
        return True

    def set_active(self, codes: list[dict], active: bool) -> list[str]:
        """Turn cost codes on or off. PUT /costcodes/{id} takes the whole record, so read it and send it back
        with only isActive changed (groups, schedule org and the rest stay as they are)."""
        done = []
        for c in codes:
            cur = (self.req("GET", f"/costcodes/{c['id']}") or {}).get("data")
            if isinstance(cur, list):
                cur = cur[0] if cur else None
            if not cur or (cur.get("isActive", True) is not False) == active:
                continue
            parts = self._parts(cur)
            body = {f"level{i + 1}": v for i, v in enumerate(parts[:5])}
            body.update({
                "description": cur.get("description") or "",
                "isActive": active,
                "notes": cur.get("notes") or None,
                "numericAlias": cur.get("alias"),
                "expirationDate": cur.get("expirationDate"),
                "geoFenceGroup": cur.get("geoFenceGroup"),
                "costCodeGroups": [g.get("description") if isinstance(g, dict) else str(g) for g in (cur.get("costCodeGroups") or [])],
                "scheduleOrg": [o.get("name") if isinstance(o, dict) else str(o) for o in (cur.get("scheduleOrg") or [])] or ["default"],
            })
            for k in ("budgetHours", "budgetDollars", "stage", "billable", "udf1", "udf2", "udf3", "udf4"):
                v = (cur.get("settings") or {}).get(k, cur.get(k))
                if v is not None:
                    body[k] = v
            body = {k: v for k, v in body.items() if v is not None}
            self.req("PUT", f"/costcodes/{c['id']}", body=body)
            done.append(cur.get("fullName") or "\\".join(parts))
        if done:
            self._codes_cache = None
        return done

    def level1_values(self) -> list[str]:
        return sorted({self._parts(c)[0] for c in self._all_codes() if len(self._parts(c)) >= 2})

    def wo_cost_codes(self, wo: str) -> tuple[list[dict], list[str]]:
        """Cost codes of a work order. level1 mode: codes 112\\*. jobcode mode: members of job code 112's groups."""
        if self.wo_source == "level1":
            return self.level1_codes(wo), []
        jc = self.job_code(wo)
        if not jc:
            raise TcpError(404, f"job code {wo} not found in TCP")
        groups = []
        for g in jc.get("costCodeGroups") or []:
            groups.append(g.get("description") if isinstance(g, dict) else str(g))
        codes, seen = [], set()
        for g in groups:
            for c in self.group_codes(g):
                k = c["id"] or c["fullName"]
                if k not in seen:
                    seen.add(k)
                    codes.append(c)
        return codes, groups

    # ------------------------------------------------------------ people on a line
    @staticmethod
    def stamp(dt: datetime | None = None) -> str:
        """Company local wall time with no offset, like 2026-10-31T09:57:00.
        TCP turns a time with an offset into UTC and then stores that as local time (a 09:57-07:00 punch was saved at 4:57 PM),
        so LineSwitch sends local time as is. The server's TZ must be the company's time zone (render.yaml sets it)."""
        d = dt or datetime.now()
        if d.tzinfo is not None:
            d = d.astimezone().replace(tzinfo=None)
        return d.replace(microsecond=0).isoformat()

    def line_job_code(self, group_description: str) -> str | None:
        """The job code a line group is attached to (LINE 1 = 901). Cached for 10 minutes."""
        now = time.time()
        if not getattr(self, "_jc_cache", None) or now - self._jc_at > 600:
            self._jc_cache = self.get_all("/jobcodes")
            self._jc_at = now
        for j in self._jc_cache:
            for g in j.get("costCodeGroups") or []:
                name = g.get("description") if isinstance(g, dict) else str(g)
                if str(name).strip().lower() == group_description.strip().lower():
                    return str(j.get("jobCodeId"))
        return None

    def segments(self, start: datetime, stop: datetime, job_code: str | None = None) -> list[dict]:
        p = {"startDate": self.stamp(start), "stopDate": self.stamp(stop)}
        if job_code:
            p["jobCodeIds"] = str(job_code)
        return self.get_all("/worksegments", p)

    def on_line(self, job_code: str) -> list[dict]:
        """Workers clocked in on a line right now: today's segments on its job code with no time out."""
        now = datetime.now()
        # look back 3 days: someone who never clocked out yesterday is still on the line and still holds a code
        rows = self.segments(now.replace(hour=0, minute=0, second=0) - timedelta(days=3), now, job_code)
        return [r for r in rows if not r.get("timeOut")]

    def employees(self) -> dict:
        """employeeId -> name. Cached for 30 minutes."""
        now = time.time()
        if getattr(self, "_emp_cache", None) is None or now - self._emp_at > 1800:
            out = {}
            try:
                for e in self.get_all("/employees"):
                    eid = str(e.get("employeeId") or e.get("id") or "")
                    nm = " ".join(str(e.get(k) or "") for k in ("firstName", "lastName")).strip()
                    out[eid] = nm or eid
            except TcpError:
                pass
            self._emp_cache, self._emp_at = out, now
        return self._emp_cache

    def punch(self, rows: list[dict]):
        """POST /v1/punches. Each row needs operationType and employeeId (changeCostCode, clockOut, ...)."""
        return self.req("POST", "/punches", body=rows)

    def create_cost_codes(self, codes: list[dict]):
        """POST /v1/costcodes with [{level1, level2, level3, description, isActive, scheduleOrg}]."""
        return self.req("POST", "/costcodes", body=codes)

    def drop_code_cache(self):
        self._codes_cache = None

    # ------------------------------------------------------------ write
    def ref(self, c: dict) -> str:
        return c.get(self.code_ref) or c.get("fullName") or c.get("id")

    def swap_group(self, group: dict, remove: list[dict], add: list[dict]):
        try:
            return self._swap(group, remove, add)
        except TcpError as ex:
            if "already assigned" not in str(ex) and "not assigned" not in str(ex):
                raise
            # someone else changed the group a moment ago: only send what is still needed
            have = {str(c.get("fullName", "")).lower() for c in self.group_codes(group["description"])}
            add = [c for c in add if str(c.get("fullName", "")).lower() not in have]
            remove = [c for c in remove if str(c.get("fullName", "")).lower() in have]
            return self._swap(group, remove, add) if (add or remove) else None

    def _swap(self, group: dict, remove: list[dict], add: list[dict]):
        body = {"description": group["description"]}
        if remove:
            body["removeCostCodes"] = [self.ref(c) for c in remove]
        if add:
            body["addCostCodes"] = [self.ref(c) for c in add]
        return self.req("PUT", f"/costcodegroups/{group['id']}", body=body)


def _errors(r) -> str:
    try:
        j = r.json()
    except Exception:  # noqa
        return r.text[:300]
    msgs = []
    for e in (j.get("errors") or []) if isinstance(j, dict) else []:
        det = "; ".join(f"{d.get('field')}: {d.get('error')}" for d in (e.get("details") or []) if isinstance(d, dict))
        msgs.append(det or e.get("message", ""))
    return " | ".join(m for m in msgs if m)[:400] or r.text[:300]
