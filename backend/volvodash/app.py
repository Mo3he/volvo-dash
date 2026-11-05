"""HTTP API and static file server. Run with: python -m volvodash"""

import asyncio
import csv
import hashlib
import io
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse

from . import sim
from .clock import Clock, ManualClock, SimClock
from .collector import Collector
from .db import DB, DEFAULT_SETTINGS, SECRET_SETTINGS, get_settings, row_dict
from .mqtt import MqttBridge
from .periods import after_charge
from . import raw
from .detect import OBS_FIELDS, Obs, haversine_m
from .enrich import Enricher, Lookups, send_webhook
from .volvo import COMMANDS, DAILY_LIMIT, VolvoClient, VolvoError

log = logging.getLogger("volvodash")

SESSION_COOKIE = "vd_session"
SESSION_DAYS = 30
MAX_LOGIN_FAILURES = 8
LOGIN_WINDOW_S = 900


class Ctx:
    db: DB
    clock: Clock
    client: VolvoClient
    collector: Collector
    sim_mode: str | None = None
    no_auth: bool = False
    backups: Path | None = None
    mqtt: MqttBridge
    login_failures: list[float] = []


ctx = Ctx()


# -- startup ---------------------------------------------------------------

async def _start() -> asyncio.Task[None]:
    data = Path(os.environ.get("VOLVODASH_DATA", "data"))
    ctx.backups = None if os.environ.get("VOLVODASH_SIM") else data / "backups"
    ctx.sim_mode = os.environ.get("VOLVODASH_SIM") or None
    ctx.no_auth = os.environ.get("VOLVODASH_NO_AUTH") == "1"

    if ctx.sim_mode:
        # The simulated car lives in memory, so its database starts fresh every run
        # and never touches the real one.
        path = data / "sim.db"
        for suffix in ("", "-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)
        ctx.db = DB(path)
        days = int(os.environ.get("VOLVODASH_SIM_DAYS", "30"))
        speed = float(os.environ.get("VOLVODASH_SIM_SPEED", "20"))
        seed_clock = ManualClock(sim.default_start(days))
        car = sim.SimCar(seed_clock.t, mode=ctx.sim_mode)
        sim.connect(ctx.db, seed_clock)
        ctx.db.set("settings", {"vehicle_name": "EX40 (simulated)", "timezone": "Europe/Stockholm",
                                "default_price_per_kwh": 0.55})
        ctx.db.insert("places", {"name": "Home", "lat": sim.SPOTS["home"][1],
                                 "lon": sim.SPOTS["home"][2], "radius_m": 150,
                                 "price_per_kwh": 0.14})
        ctx.clock = seed_clock
        ctx.client = VolvoClient(ctx.db, seed_clock, sim.transport(car, lambda: ctx.clock))
        enricher = Enricher(ctx.db, sim.SimLookups())
        ctx.collector = Collector(ctx.db, ctx.client, seed_clock, enricher)
        ctx.collector.live = False
        log.info("Simulating %d days of history...", days)
        end = time.time()
        with ctx.db.tx():
            while seed_clock.t < end:
                seed_clock.t += await ctx.collector.tick()
            while await enricher.run(seed_clock.t, limit=50):
                pass
        ctx.clock = SimClock(seed_clock.t, speed)
        ctx.client.clock = ctx.collector.clock = ctx.clock
        ctx.collector.live = True
        log.info("Simulator ready, running at %gx speed.", speed)
    else:
        ctx.db = DB(data / "volvodash.db")
        ctx.clock = Clock()
        ctx.client = VolvoClient(ctx.db, ctx.clock)
        ctx.collector = Collector(ctx.db, ctx.client, ctx.clock, Enricher(ctx.db, Lookups()),
                                  backups=data / "backups")
    ctx.mqtt = MqttBridge(ctx.db, ctx.collector, _run_command)
    asyncio.create_task(ctx.mqtt.run())
    return asyncio.create_task(ctx.collector.run())


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    task = await _start()
    yield
    task.cancel()


# Every route handler below is `async def`, even those that never await. FastAPI
# runs plain `def` handlers in a thread pool, and the one SQLite connection is
# shared with the collector on the event loop; keeping everything on that single
# thread is what makes the sharing safe.
app = FastAPI(title="Volvo Dash", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")


# -- dashboard login -------------------------------------------------------

def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1).hex()


def _set_password(password: str) -> None:
    if len(password) < 8:
        raise HTTPException(400, "Use at least 8 characters.")
    salt = secrets.token_bytes(16)
    ctx.db.set("password", {"salt": salt.hex(), "hash": _hash_password(password, salt)})


def _check_password(password: str) -> bool:
    stored = ctx.db.get("password")
    if not stored:
        return False
    candidate = _hash_password(password, bytes.fromhex(stored["salt"]))
    return secrets.compare_digest(candidate, stored["hash"])


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _signed_in(request: Request) -> bool:
    if ctx.no_auth:
        return True
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return False
    row = ctx.db.one("SELECT expires_at FROM sessions WHERE token_hash = ?", _token_hash(token))
    return bool(row and row["expires_at"] > time.time())


async def user(request: Request) -> None:
    # async on purpose: see the note on threads above `app`.
    if not _signed_in(request):
        raise HTTPException(401, "Sign in first.")


def _start_session(request: Request, response: Response) -> None:
    token = secrets.token_urlsafe(32)
    ctx.db.x("DELETE FROM sessions WHERE expires_at < ?", time.time())
    ctx.db.insert("sessions", {"token_hash": _token_hash(token),
                               "expires_at": time.time() + SESSION_DAYS * 86400})
    https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_DAYS * 86400,
                        httponly=True, samesite="lax", secure=https)


@app.get("/api/session")
async def session(request: Request) -> dict[str, Any]:
    return {
        "setup_needed": not ctx.no_auth and not ctx.db.get("password"),
        "signed_in": _signed_in(request),
        "sim": ctx.sim_mode,
    }


@app.post("/api/setup")
async def setup(request: Request, response: Response) -> dict[str, bool]:
    if ctx.db.get("password"):
        raise HTTPException(409, "A password is already set.")
    body = await request.json()
    _set_password(body.get("password", ""))
    if body.get("timezone"):
        _save_settings({"timezone": body["timezone"]})
    _start_session(request, response)
    return {"ok": True}


@app.post("/api/login")
async def login(request: Request, response: Response) -> dict[str, bool]:
    now = time.time()
    ctx.login_failures = [t for t in ctx.login_failures if now - t < LOGIN_WINDOW_S]
    if len(ctx.login_failures) >= MAX_LOGIN_FAILURES:
        raise HTTPException(429, "Too many attempts. Try again in a few minutes.")
    body = await request.json()
    if not _check_password(body.get("password", "")):
        ctx.login_failures.append(now)
        await asyncio.sleep(1)
        raise HTTPException(401, "Wrong password.")
    _start_session(request, response)
    return {"ok": True}


@app.post("/api/logout")
async def logout(request: Request, response: Response) -> dict[str, bool]:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        ctx.db.x("DELETE FROM sessions WHERE token_hash = ?", _token_hash(token))
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@app.post("/api/password", dependencies=[Depends(user)])
async def change_password(request: Request) -> dict[str, bool]:
    body = await request.json()
    if not _check_password(body.get("current", "")):
        raise HTTPException(403, "Current password is wrong.")
    _set_password(body.get("new", ""))
    ctx.db.x("DELETE FROM sessions")
    return {"ok": True}


# -- views -----------------------------------------------------------------

def _charge_view(row: Any, with_after: bool = True) -> dict[str, Any]:
    c = dict(row)
    if with_after:
        c["after"] = after_charge(ctx.db, c, ctx.collector.obs.soc)
    energy = c["energy_override"] if c["energy_override"] is not None else c["energy_kwh"]
    c["energy_billed_kwh"] = energy
    if c["cost_override"] is not None:
        c["cost"] = c["cost_override"]
    elif energy is not None and c["price_per_kwh"] is not None:
        c["cost"] = round(energy * c["price_per_kwh"], 2)
    else:
        c["cost"] = None
    return c


def _range(request: Request) -> tuple[float, float]:
    q = request.query_params
    try:
        start = float(q.get("from", 0))
        end = float(q.get("to", ctx.clock.now() + 86400))
    except ValueError:
        raise HTTPException(400, "from and to must be Unix timestamps.") from None
    return start, end


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(get_settings(ctx.db)["timezone"])
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


@app.get("/api/status", dependencies=[Depends(user)])
async def status() -> dict[str, Any]:
    db, c = ctx.db, ctx.collector
    settings = get_settings(db)
    vehicle = db.get("volvo_vehicle") or {}
    d = c.detector.s
    trip = row_dict(db.one("SELECT * FROM trips WHERE id = ?", d["trip"]["id"])) if d["trip"] else None
    if trip and not d["trip"]["announced"]:
        trip = None  # switched on but has not moved yet
    charge = db.one("SELECT * FROM charges WHERE id = ?", d["charge"]["id"]) if d["charge"] else None
    last_trip = db.one("SELECT * FROM trips WHERE ended_at IS NOT NULL ORDER BY started_at DESC LIMIT 1")
    last_charge = db.one("SELECT * FROM charges WHERE ended_at IS NOT NULL ORDER BY started_at DESC LIMIT 1")
    vin = ctx.client.vin
    return {
        "now": ctx.clock.now(),
        "settings": {k: settings[k] for k in ("vehicle_name", "units", "currency", "timezone")},
        "vehicle": {
            "model": (vehicle.get("descriptions") or {}).get("model"),
            "year": vehicle.get("modelYear"),
            "colour": vehicle.get("externalColour"),
            "vin_tail": vin[-6:] if vin else None,
            "capacity_kwh": c.capacity(),
            "image": (vehicle.get("images") or {}).get("exteriorImageUrl") or None,
        },
        "connection": {
            "configured": ctx.client.configured(),
            "connected": ctx.client.connected(),
            "auth_error": db.get("volvo_auth_error"),
            "consent_at": db.get("volvo_consent_at"),
            "calls_today": ctx.client.calls_today(),
            "daily_limit": DAILY_LIMIT,
            "unavailable": db.get("volvo_unavailable", []),
            "commands": db.get("volvo_commands", []),
            "sim": ctx.sim_mode,
            "mqtt": ctx.mqtt.status,
            **c.status,
        },
        "obs": {k: getattr(c.obs, k) for k in OBS_FIELDS} if c.status["last_poll"] or c.obs.t else None,
        "state": {
            r["key"]: {"value": json.loads(r["value"]), "unit": r["unit"], "ts": r["ts"]}
            for r in db.q("SELECT * FROM state")
        },
        "trip": trip,
        "charge": _charge_view(charge) if charge else None,
        "last_trip": row_dict(last_trip),
        "last_charge": _charge_view(last_charge) if last_charge else None,
    }


@app.get("/api/history", dependencies=[Depends(user)])
async def history(request: Request) -> dict[str, Any]:
    start, end = _range(request)
    rows = ctx.db.q(
        "SELECT t, soc, range_km, power_w, charging_status FROM observations "
        "WHERE t BETWEEN ? AND ? AND soc IS NOT NULL ORDER BY t", start, end)
    # One reading from just before the window so the line starts at its left edge.
    before = ctx.db.q(
        "SELECT t, soc, range_km, power_w, charging_status FROM observations "
        "WHERE t < ? AND soc IS NOT NULL ORDER BY t DESC LIMIT 1", start)
    out, last = [], None
    for r in [*before, *rows]:
        # Keep a reading whenever anything the chart draws changed.
        if last is None or any(r[k] != last[k] for k in ("soc", "range_km", "power_w", "charging_status")):
            out.append(dict(r))
            last = r
    # Driving and charging stretches overlapping the window, ongoing ones up to now.
    now = ctx.clock.now()
    spans = [
        {"kind": kind, "from": r["started_at"], "to": r["ended_at"] or now}
        for kind, table in (("drive", "trips"), ("charge", "charges"))
        for r in ctx.db.q(
            f"SELECT started_at, ended_at FROM {table} "
            "WHERE started_at <= ? AND (ended_at IS NULL OR ended_at >= ?)", end, start)
    ]
    return {"points": out, "spans": spans}


@app.get("/api/events", dependencies=[Depends(user)])
async def events(limit: int = 30) -> list[dict[str, Any]]:
    return [
        {"id": r["id"], "ts": r["ts"], "type": r["type"], **json.loads(r["detail"] or "{}")}
        for r in ctx.db.q("SELECT * FROM events ORDER BY ts DESC, id DESC LIMIT ?", min(limit, 200))
    ]


@app.get("/api/trips", dependencies=[Depends(user)])
async def trips(request: Request) -> list[dict[str, Any]]:
    start, end = _range(request)
    return [dict(r) for r in ctx.db.q(
        "SELECT * FROM trips WHERE ended_at IS NOT NULL AND started_at BETWEEN ? AND ? "
        "ORDER BY started_at DESC", start, end)]


@app.get("/api/trips/{trip_id}", dependencies=[Depends(user)])
async def trip(trip_id: int) -> dict[str, Any]:
    row = ctx.db.one("SELECT * FROM trips WHERE id = ?", trip_id)
    if not row:
        raise HTTPException(404, "No such trip.")
    points = ctx.db.q("SELECT ts, lat, lon FROM trip_points WHERE trip_id = ? ORDER BY ts", trip_id)
    return dict(row) | {"points": [dict(p) for p in points]}


@app.patch("/api/trips/{trip_id}", dependencies=[Depends(user)])
async def edit_trip(trip_id: int, request: Request) -> dict[str, Any]:
    body = await request.json()
    row = ctx.db.one("SELECT * FROM trips WHERE id = ?", trip_id)
    if not row:
        raise HTTPException(404, "No such trip.")
    if "note" in body:
        ctx.db.update("trips", trip_id, {"note": (body.get("note") or "").strip() or None})
    for end in ("start", "end"):
        if end in body:
            _set_trip_position(row, end, body[end])
    if "ended_at" in body or "distance_km" in body:
        _set_trip_end(row, body)
    return await trip(trip_id)


def _set_trip_end(row: Any, body: dict[str, Any]) -> None:
    """Correct a trip's end time or distance by hand (None goes back to the car's)."""
    if row["ended_at"] is None:
        raise HTTPException(400, "This trip is still going.")
    values: dict[str, Any] = {}
    if "ended_at" in body:
        ended = _number(body["ended_at"]) if body["ended_at"] is not None else None
        if ended is not None:
            nxt = ctx.db.one(
                "SELECT started_at FROM trips WHERE started_at > ? ORDER BY started_at LIMIT 1",
                row["started_at"])
            if ended <= row["started_at"]:
                raise HTTPException(400, "The end has to be after the start.")
            if nxt and ended > nxt["started_at"]:
                raise HTTPException(400, "The end can't be after the next trip started.")
        values["ended_override"] = ended
        values["ended_car"] = row["ended_car"] or row["ended_at"]
    if "distance_km" in body:
        km = _number(body["distance_km"]) if body["distance_km"] is not None else None
        if km is not None and not 0 < km < 2000:
            raise HTTPException(400, "Enter a distance in kilometres.")
        values["distance_override"] = km
    ctx.db.update("trips", row["id"], values)
    ctx.collector.detector.refinalize(row["id"])
    ctx.collector.broadcast()


def _set_trip_position(row: Any, end: str, loc: dict[str, Any] | None) -> None:
    """Enter (or with None, clear) a start or end position by hand.

    Only where the car had no GPS. The car did not move between one trip's
    end and the next one's start, so the neighbouring trip gets it too.
    """
    if row[f"{end}_lat"] is not None and not row[f"{end}_manual"]:
        raise HTTPException(400, f"The car reported this trip's {end} position itself.")
    if loc is None:
        values = {"lat": None, "lon": None, "place": None, "manual": 0}
    else:
        lat, lon = _number(loc.get("lat")), _number(loc.get("lon"))
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise HTTPException(400, "A position needs a valid lat and lon.")
        # No name: the address lookup fills it in.
        values = {"lat": lat, "lon": lon, "place": (loc.get("name") or "").strip() or None, "manual": 1}

    def apply(trip_id: int, which: str) -> None:
        ctx.db.update("trips", trip_id, {f"{which}_{k}": v for k, v in values.items()})

    apply(row["id"], end)
    if end == "end":
        other = ctx.db.one(
            "SELECT * FROM trips WHERE started_at > ? ORDER BY started_at LIMIT 1", row["started_at"])
        which, here, there = "start", row["odo_end"], other and other["odo_start"]
    else:
        other = ctx.db.one(
            "SELECT * FROM trips WHERE started_at < ? ORDER BY started_at DESC LIMIT 1", row["started_at"])
        which, here, there = "end", row["odo_start"], other and other["odo_end"]
    parked_between = other and (here is None or there is None or here == there)
    if parked_between and (other[f"{which}_lat"] is None or other[f"{which}_manual"]):
        apply(other["id"], which)


@app.get("/api/charges", dependencies=[Depends(user)])
async def charges(request: Request) -> list[dict[str, Any]]:
    start, end = _range(request)
    return [_charge_view(r) for r in ctx.db.q(
        "SELECT * FROM charges WHERE ended_at IS NOT NULL AND started_at BETWEEN ? AND ? "
        "ORDER BY started_at DESC", start, end)]


@app.get("/api/charges/{charge_id}", dependencies=[Depends(user)])
async def charge(charge_id: int) -> dict[str, Any]:
    row = ctx.db.one("SELECT * FROM charges WHERE id = ?", charge_id)
    if not row:
        raise HTTPException(404, "No such charging session.")
    samples = ctx.db.q(
        "SELECT ts, power_w, soc FROM charge_samples WHERE charge_id = ? ORDER BY ts", charge_id)
    return _charge_view(row) | {"samples": [dict(s) for s in samples]}


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise HTTPException(400, f"Not a number: {value!r}") from None


@app.patch("/api/charges/{charge_id}", dependencies=[Depends(user)])
async def edit_charge(charge_id: int, request: Request) -> dict[str, Any]:
    body = await request.json()
    values: dict[str, Any] = {}
    for key in ("price_per_kwh", "cost_override", "energy_override"):
        if key in body:
            values[key] = _number(body[key])
    if "note" in body:
        values["note"] = (body["note"] or "").strip() or None
    if "place_name" in body:
        values["place_name"] = (body["place_name"] or "").strip() or None
    ctx.db.update("charges", charge_id, values)
    return await charge(charge_id)


@app.get("/api/stats", dependencies=[Depends(user)])
async def stats(request: Request) -> dict[str, Any]:
    start, end = _range(request)
    tz = _tz()
    trip_rows = ctx.db.q(
        "SELECT * FROM trips WHERE ended_at IS NOT NULL AND started_at BETWEEN ? AND ?", start, end)
    charge_rows = [_charge_view(r, with_after=False) for r in ctx.db.q(
        "SELECT * FROM charges WHERE ended_at IS NOT NULL AND started_at BETWEEN ? AND ?", start, end)]

    first = min([r["started_at"] for r in trip_rows] + [c["started_at"] for c in charge_rows],
                default=start)
    span_days = (min(end, ctx.clock.now()) - max(start, first)) / 86400
    unit = "day" if span_days <= 35 else "week" if span_days <= 190 else "month"

    def bucket(ts: float) -> str:
        day = datetime.fromtimestamp(ts, tz).date()
        if unit == "week":
            day = day.fromordinal(day.toordinal() - day.weekday())
        elif unit == "month":
            day = day.replace(day=1)
        return day.isoformat()

    buckets: dict[str, dict[str, float]] = {}

    def slot(ts: float) -> dict[str, float]:
        return buckets.setdefault(bucket(ts), {
            "distance_km": 0.0, "used_kwh": 0.0, "ac_kwh": 0.0, "dc_kwh": 0.0, "cost": 0.0})

    measured_km = measured_kwh = 0.0
    bands: dict[int, list[float]] = {}
    for t in trip_rows:
        b = slot(t["started_at"])
        b["distance_km"] += t["distance_km"] or 0
        b["used_kwh"] += t["energy_kwh"] or 0
        if t["energy_kwh"] is not None and t["distance_km"]:
            measured_km += t["distance_km"]
            measured_kwh += t["energy_kwh"]
            if t["temp_c"] is not None:
                band = bands.setdefault(int(t["temp_c"] // 5) * 5, [0.0, 0.0, 0])
                band[0] += t["distance_km"]
                band[1] += t["energy_kwh"]
                band[2] += 1
    for c in charge_rows:
        b = slot(c["started_at"])
        key = "dc_kwh" if c["charge_type"] == "DC" else "ac_kwh"
        b[key] += c["energy_billed_kwh"] or 0
        b["cost"] += c["cost"] or 0

    distance = sum(t["distance_km"] or 0 for t in trip_rows)
    charged = sum(c["energy_billed_kwh"] or 0 for c in charge_rows)
    cost = sum(c["cost"] or 0 for c in charge_rows)
    return {
        "bucket": unit,
        "totals": {
            "trips": len(trip_rows),
            "distance_km": round(distance, 1),
            "used_kwh": round(sum(t["energy_kwh"] or 0 for t in trip_rows), 1),
            # Total energy over total distance: whole-percent SoC makes single trips
            # jumpy, but the errors cancel over many.
            "efficiency": round(measured_kwh / measured_km * 100, 1) if measured_km >= 20 else None,
            "drive_seconds": sum(t["ended_at"] - t["started_at"] for t in trip_rows),
            "charges": len(charge_rows),
            "charged_kwh": round(charged, 1),
            "dc_kwh": round(sum(c["energy_billed_kwh"] or 0 for c in charge_rows
                                if c["charge_type"] == "DC"), 1),
            "cost": round(cost, 2),
            "unpriced_charges": sum(1 for c in charge_rows if c["cost"] is None),
            "cost_per_100km": round(cost / distance * 100, 2) if distance >= 20 and cost else None,
        },
        "series": [{"bucket": k, **{m: round(v, 2) for m, v in buckets[k].items()}}
                   for k in sorted(buckets)],
        "by_temperature": [
            {"from_c": k, "to_c": k + 5, "trips": v[2], "distance_km": round(v[0], 1),
             "efficiency": round(v[1] / v[0] * 100, 1)}
            for k, v in sorted(bands.items()) if v[0] >= 20
        ],
    }


def _csv(rows: list[dict[str, Any]], columns: list[str], name: str) -> Response:
    tz = _tz()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(columns)
    for r in rows:
        writer.writerow([
            datetime.fromtimestamp(r[c], tz).isoformat(timespec="seconds")
            if c.endswith("_at") and r[c] else r[c]
            for c in columns
        ])
    return Response(out.getvalue(), media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/export/trips.csv", dependencies=[Depends(user)])
async def export_trips(request: Request) -> Response:
    return _csv(list(reversed(await trips(request))), [
        "started_at", "ended_at", "start_estimated", "distance_km", "energy_kwh", "efficiency",
        "avg_speed_kmh", "soc_start", "soc_end", "odo_start", "odo_end", "start_place",
        "end_place", "temp_c", "note"], "trips.csv")


@app.get("/api/export/charges.csv", dependencies=[Depends(user)])
async def export_charges(request: Request) -> Response:
    return _csv(list(reversed(await charges(request))), [
        "started_at", "ended_at", "charge_type", "soc_start", "soc_end", "energy_billed_kwh",
        "energy_metered_kwh", "energy_soc_kwh", "energy_source", "max_power_kw", "avg_power_kw",
        "place_name", "price_per_kwh", "cost", "odometer_km", "note"], "charges.csv")


# -- actions ---------------------------------------------------------------

@app.post("/api/refresh", dependencies=[Depends(user)])
async def refresh() -> dict[str, Any]:
    if not ctx.client.connected():
        raise HTTPException(409, "Not connected to Volvo.")
    await ctx.collector.tick(force=True)
    return {"ok": True}


async def _run_command(name: str) -> dict[str, Any]:
    """Send a command to the car (or "refresh" to poll now). Used by the web app and MQTT."""
    if name == "refresh":
        await ctx.collector.tick(force=True)
        return {"invokeStatus": "COMPLETED"}
    result = await ctx.client.command(name)
    ctx.db.insert("events", {"ts": ctx.clock.now(), "type": "command", "detail": json.dumps(
        {"command": name, "result": result.get("invokeStatus")})})
    ctx.collector.due.pop("doors", None)
    ctx.collector.wake()
    return result


@app.post("/api/command/{name}", dependencies=[Depends(user)])
async def command(name: str) -> dict[str, Any]:
    if name not in COMMANDS:
        raise HTTPException(404, "Unknown command.")
    try:
        result = await _run_command(name)
    except VolvoError as e:
        raise HTTPException(502, f"Volvo refused: {e}") from e
    return {"status": result.get("invokeStatus"), "message": result.get("message")}


@app.post("/api/rebuild", dependencies=[Depends(user)])
async def rebuild_all() -> dict[str, int]:
    async with ctx.collector._lock:  # noqa: SLF001  (no poll may run mid-rebuild)
        counts = raw.rebuild_all(ctx.db, ctx.collector.capacity, ctx.backups)
        cols = ", ".join(OBS_FIELDS)
        last = ctx.db.one(f"SELECT {cols} FROM observations ORDER BY t DESC, id DESC LIMIT 1")
        ctx.collector.obs = Obs(**dict(last)) if last else Obs()
        ctx.collector.detector.s = ctx.db.get("detector_state")
    ctx.collector.broadcast()
    return counts


@app.get("/api/export/raw.jsonl", dependencies=[Depends(user)])
async def export_raw(request: Request) -> StreamingResponse:
    """Every stored Volvo answer, one JSON object per line."""
    start, end = _range(request)
    rows = ctx.db.q(
        "SELECT fetched_at, method, endpoint, status, body FROM raw_responses "
        "WHERE fetched_at BETWEEN ? AND ? ORDER BY fetched_at, id", start, end)

    def lines() -> Any:
        for r in rows:
            text = raw.body_of(r)
            try:
                body: Any = json.loads(text)
            except ValueError:
                body = text
            yield json.dumps({"fetched_at": r["fetched_at"], "method": r["method"],
                              "endpoint": r["endpoint"], "status": r["status"], "body": body}) + "\n"

    return StreamingResponse(lines(), media_type="application/x-ndjson", headers={
        "Content-Disposition": 'attachment; filename="volvo-raw.jsonl"'})


@app.get("/api/stream")
async def stream(request: Request) -> StreamingResponse:
    await user(request)
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=20)
    ctx.collector.listeners.add(queue)

    async def feed() -> AsyncIterator[str]:
        try:
            yield "retry: 5000\n\n"
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=25)
                    yield f"data: {message}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            ctx.collector.listeners.discard(queue)

    return StreamingResponse(feed(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# -- settings and places ---------------------------------------------------

def _save_settings(changes: dict[str, Any]) -> dict[str, Any]:
    current = ctx.db.get("settings", {})
    for key, value in changes.items():
        if key not in DEFAULT_SETTINGS:
            continue
        if key in ("usable_capacity_kwh", "default_price_per_kwh"):
            value = _number(value)
        elif key in ("idle_interval_s", "active_interval_s", "merge_gap_s"):
            low = {"idle_interval_s": 60, "active_interval_s": 30, "merge_gap_s": 0}[key]
            value = max(int(_number(value) or 0), low)
        elif key == "timezone":
            try:
                ZoneInfo(str(value))
            except (ZoneInfoNotFoundError, ValueError):
                raise HTTPException(400, f"Unknown timezone {value!r}.") from None
        elif key in SECRET_SETTINGS and not value:
            continue  # blank means "keep what is stored"
        elif key == "mqtt_port":
            value = int(_number(value) or 1883)
        elif key in ("mqtt_enabled", "mqtt_tls", "lookups_enabled"):
            value = bool(value)
        elif key in ("mqtt_host", "mqtt_username", "mqtt_topic_prefix", "mqtt_discovery_prefix"):
            value = str(value or "").strip()
        elif key == "units" and value not in ("km", "mi"):
            raise HTTPException(400, "Units must be km or mi.")
        current[key] = value
    ctx.db.set("settings", current)
    return get_settings(ctx.db)


@app.get("/api/settings", dependencies=[Depends(user)])
async def read_settings() -> dict[str, Any]:
    return _public_settings()


def _public_settings() -> dict[str, Any]:
    s = get_settings(ctx.db)
    return {k: ("" if k in SECRET_SETTINGS else v) for k, v in s.items()} | {
        f"has_{k}": bool(s[k]) for k in SECRET_SETTINGS}


@app.put("/api/settings", dependencies=[Depends(user)])
async def write_settings(request: Request) -> dict[str, Any]:
    body = await request.json()
    _save_settings(body)
    ctx.collector.wake()
    if any(k.startswith("mqtt_") or k in ("vehicle_name", "currency") for k in body):
        ctx.mqtt.reconfigure()
    return _public_settings()


@app.post("/api/settings/test-webhook", dependencies=[Depends(user)])
async def test_webhook() -> dict[str, bool]:
    url = get_settings(ctx.db)["webhook_url"]
    if not url:
        raise HTTPException(400, "Save a webhook URL first.")
    await send_webhook(url, "Volvo Dash", "Test notification. If you can read this, it works.")
    return {"ok": True}


def _apply_place(place: dict[str, Any]) -> None:
    """Label existing trips and charges that happened at this place."""
    for c in ctx.db.q("SELECT id, lat, lon, price_per_kwh FROM charges WHERE lat IS NOT NULL"):
        if haversine_m(c["lat"], c["lon"], place["lat"], place["lon"]) <= place["radius_m"]:
            values = {"place_id": place["id"], "place_name": place["name"]}
            if place["price_per_kwh"] is not None:
                values["price_per_kwh"] = place["price_per_kwh"]
            ctx.db.update("charges", c["id"], values)
    for end in ("start", "end"):
        for t in ctx.db.q(f"SELECT id, {end}_lat lat, {end}_lon lon FROM trips WHERE {end}_lat IS NOT NULL"):
            if haversine_m(t["lat"], t["lon"], place["lat"], place["lon"]) <= place["radius_m"]:
                ctx.db.update("trips", t["id"], {f"{end}_place": place["name"]})


def _place_values(body: dict[str, Any]) -> dict[str, Any]:
    name = (body.get("name") or "").strip()
    lat, lon = _number(body.get("lat")), _number(body.get("lon"))
    if not name or lat is None or lon is None:
        raise HTTPException(400, "A place needs a name and a position.")
    return {"name": name, "lat": lat, "lon": lon,
            "radius_m": max(_number(body.get("radius_m")) or 150, 25),
            "price_per_kwh": _number(body.get("price_per_kwh"))}


@app.get("/api/places", dependencies=[Depends(user)])
async def places() -> list[dict[str, Any]]:
    return [dict(r) for r in ctx.db.q("SELECT * FROM places ORDER BY name")]


@app.post("/api/places", dependencies=[Depends(user)])
async def add_place(request: Request) -> dict[str, Any]:
    values = _place_values(await request.json())
    values["id"] = ctx.db.insert("places", values)
    _apply_place(values)
    return values


@app.put("/api/places/{place_id}", dependencies=[Depends(user)])
async def edit_place(place_id: int, request: Request) -> dict[str, Any]:
    values = _place_values(await request.json())
    ctx.db.update("places", place_id, values)
    _apply_place(values | {"id": place_id})
    return values | {"id": place_id}


@app.delete("/api/places/{place_id}", dependencies=[Depends(user)])
async def delete_place(place_id: int) -> dict[str, bool]:
    ctx.db.x("DELETE FROM places WHERE id = ?", place_id)
    ctx.db.x("UPDATE charges SET place_id = NULL WHERE place_id = ?", place_id)
    return {"ok": True}


# -- connecting to Volvo ---------------------------------------------------

def _volvo_view(request: Request) -> dict[str, Any]:
    creds = ctx.client.creds
    origin = f"{request.headers.get('x-forwarded-proto', request.url.scheme)}://{request.headers.get('host', '')}"
    return {
        "configured": ctx.client.configured(),
        "connected": ctx.client.connected(),
        "signed_in": bool(ctx.client.tokens.get("refresh_token")),
        "client_id": creds.get("client_id", ""),
        "redirect_uri": creds.get("redirect_uri", ""),
        "has_api_key": bool(creds.get("api_key")),
        "has_client_secret": bool(creds.get("client_secret")),
        "suggested_redirect_uri": f"{origin}/api/volvo/callback",
        "vin": ctx.client.vin,
        "vehicles": ctx.db.get("volvo_vehicles", []),
        "scopes": ctx.client.scopes(),
        "consent_at": ctx.db.get("volvo_consent_at"),
        "auth_error": ctx.db.get("volvo_auth_error"),
        "sim": ctx.sim_mode,
    }


@app.get("/api/volvo", dependencies=[Depends(user)])
async def volvo(request: Request) -> dict[str, Any]:
    return _volvo_view(request)


@app.put("/api/volvo/credentials", dependencies=[Depends(user)])
async def volvo_credentials(request: Request) -> dict[str, Any]:
    body = await request.json()
    creds = dict(ctx.client.creds)
    for key in ("api_key", "client_id", "client_secret", "redirect_uri"):
        value = (body.get(key) or "").strip()
        if value:  # blank keeps what is stored, so secrets never need to be sent back
            creds[key] = value
    if creds.get("client_id") != ctx.client.creds.get("client_id"):
        # A new application may tick different scopes.
        ctx.db.delete("volvo_without_optional_scopes")
    ctx.db.set("volvo_creds", creds)
    return _volvo_view(request)


@app.post("/api/volvo/authorize", dependencies=[Depends(user)])
async def volvo_authorize() -> dict[str, str]:
    if not ctx.client.configured():
        raise HTTPException(400, "Save the API key, client ID, client secret and redirect URI first.")
    return {"url": ctx.client.authorize_url()}


async def _finish_sign_in() -> None:
    vins = await ctx.client.vehicles()
    ctx.db.set("volvo_vehicles", vins)
    if not vins:
        raise VolvoError("Signed in, but this Volvo ID has no cars.")
    if ctx.client.vin not in vins:
        if len(vins) > 1:
            return  # the owner picks one in Settings
        await _select_vehicle(vins[0])
    # A new sign-in may grant scopes the last one lacked: ask for everything again now.
    ctx.db.delete("volvo_unavailable")
    ctx.collector.off.clear()
    ctx.collector.due.clear()
    ctx.collector.wake()


async def _select_vehicle(vin: str) -> None:
    ctx.db.set("volvo_vin", vin)
    ctx.db.set("volvo_vehicle", await ctx.client.vehicle(vin))
    ctx.db.delete("volvo_unavailable")
    try:
        ctx.db.set("volvo_commands", await ctx.client.available_commands())
    except VolvoError:
        ctx.db.set("volvo_commands", [])
    ctx.collector.off.clear()
    ctx.collector.due.clear()
    ctx.collector.wake()
    ctx.mqtt.reconfigure()


@app.get("/api/volvo/callback")
async def volvo_callback(request: Request) -> RedirectResponse:
    # No dashboard session required: the unguessable `state` proves this redirect
    # belongs to a sign-in that a signed-in user started.
    q = request.query_params
    if "error" in q:
        message = ctx.client.refused_scopes(q["error"]) or q.get("error_description") or q["error"]
        return RedirectResponse(f"/settings?volvo_error={quote(message)}")
    try:
        if "state" not in q or "code" not in q:
            raise VolvoError("Volvo did not send a code.")
        await ctx.client.exchange(q["code"], q["state"])
        await _finish_sign_in()
    except VolvoError as e:
        return RedirectResponse(f"/settings?volvo_error={quote(str(e))}")
    return RedirectResponse("/settings?volvo=connected")


@app.post("/api/volvo/exchange", dependencies=[Depends(user)])
async def volvo_exchange(request: Request) -> dict[str, Any]:
    body = await request.json()
    try:
        await ctx.client.exchange(body.get("url", ""))
        await _finish_sign_in()
    except (VolvoError, KeyError) as e:
        raise HTTPException(400, str(e) or "That does not look like the address Volvo sent you to.") from e
    return _volvo_view(request)


@app.post("/api/volvo/vehicle", dependencies=[Depends(user)])
async def volvo_vehicle(request: Request) -> dict[str, Any]:
    body = await request.json()
    if body.get("vin") not in ctx.db.get("volvo_vehicles", []):
        raise HTTPException(400, "That car is not on this Volvo ID.")
    try:
        await _select_vehicle(body["vin"])
    except VolvoError as e:
        raise HTTPException(502, str(e)) from e
    return _volvo_view(request)


@app.post("/api/volvo/disconnect", dependencies=[Depends(user)])
async def volvo_disconnect(request: Request) -> dict[str, Any]:
    ctx.client.disconnect()
    return _volvo_view(request)


# -- the web app -----------------------------------------------------------

STATIC = Path(os.environ.get("VOLVODASH_STATIC") or Path(__file__).parents[2] / "frontend" / "dist")


@app.get("/{path:path}", include_in_schema=False)
async def spa(path: str) -> Response:
    if path.startswith("api/"):
        raise HTTPException(404, "Not found.")
    file = (STATIC / path).resolve()
    if path and file.is_file() and STATIC.resolve() in file.parents:
        return FileResponse(file)
    index = STATIC / "index.html"
    if index.is_file():
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
    return Response("The web app has not been built. Run: cd frontend && npm install && npm run build",
                    status_code=503, media_type="text/plain")


def main() -> None:
    import uvicorn

    os.umask(0o077)  # the database holds API credentials and location history

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8000")),
                log_level="warning", proxy_headers=True)
