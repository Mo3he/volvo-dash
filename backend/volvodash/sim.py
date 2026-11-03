"""A pretend EX40 behind a pretend Volvo API.

The simulator answers the same URLs with the same JSON shapes as Volvo's real
API, so the whole pipeline (HTTP client, collector, detector) runs unchanged
against it. It exists so the app can be developed and tested without a car.

Reporting modes:
  ex40    what a real 2027 EX40 does (seen Oct 2026): "car on" and battery
          level about once a minute while driving, but odometer, trip meter
          and position stay frozen until it is switched off
  live    everything about once a minute while driving or charging
  sparse  reports only when something ends (parked, plugged, done)
"""

import asyncio
import json
import math
import random
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs
from typing import Any, Callable

import httpx

from .clock import Clock

VIN = "YV1SIMULATED00001"
CAPACITY_KWH = 79.0
LOCAL_OFFSET_S = 3600  # the simulated owner lives at UTC+1
AC_KW = 11.0

SPOTS = {
    "home": ("Kastanjgatan, Göteborg", 57.6870, 11.9460),
    "work": ("Lindholmspiren, Göteborg", 57.7065, 11.9380),
    "shop": ("Frölunda Torg, Göteborg", 57.6520, 11.9110),
    "lake": ("Strandvägen, Ulricehamn", 57.7920, 13.4180),
    "fast": ("Snabbladdning Borås", 57.7250, 12.9400),
}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def outside_temp(ts: float) -> float:
    day = datetime.fromtimestamp(ts, timezone.utc).timetuple().tm_yday
    hour = (ts + LOCAL_OFFSET_S) % 86400 / 3600
    seasonal = 8.5 - 10.5 * math.cos(2 * math.pi * (day - 20) / 365)
    return round(seasonal + 3.5 * math.sin(2 * math.pi * (hour - 9) / 24), 1)


class SimCar:
    def __init__(self, start: float, seed: int = 7, mode: str = "live") -> None:
        self.rng = random.Random(seed)
        self.mode = mode
        self.t = start
        self.soc = 71.0
        self.odo = 1482.3
        self.ta = 12.4
        self.ta_seconds = 1100.0
        # Manual trip meter and the energy the car counted over it.
        self.tm = 26.2
        self.tm_kwh = 26.2 * 27.5 / 100
        self.where = "home"
        self.lat, self.lon = SPOTS["home"][1:]
        self.loc_ts = start
        # Parking spots with no GPS (underground): the car keeps its last fix.
        self.no_gps: set[str] = set()
        self.fix = (self.lat, self.lon, start)
        self.engine = "STOPPED"
        self.engine_ts = start
        self.parked_since = start - 6 * 3600
        self.locked = True
        self.plugged = False
        self.charge: dict[str, Any] | None = None
        self.charge_status = "IDLE"
        self.target = 80
        self.target_ts = start
        self.plug_at: float | None = None
        self.drive: dict[str, Any] | None = None
        self.queue: list[dict[str, Any]] = []
        self.planned_day = int((start + LOCAL_OFFSET_S) // 86400) - 1
        self.big_days: set[int] = set()
        self.last_report = 0.0
        self.revoked = False
        # What really happened, for tests to compare the detector against.
        self.truth: dict[str, list[dict[str, Any]]] = {"drives": [], "charges": []}
        self.frozen: dict[str, Any] | None = None
        self.rep: dict[str, Any] = {}
        self._report()

    # -- the owner's week --------------------------------------------------

    def _plan(self) -> None:
        """Keep tomorrow planned, so tonight's charging can know about a long trip."""
        today = int((self.t + LOCAL_OFFSET_S) // 86400)
        while self.planned_day <= today + 1:
            self.planned_day += 1
            self._plan_day(self.planned_day)

    def _plan_day(self, day: int) -> None:
        midnight = day * 86400 - LOCAL_OFFSET_S
        weekday = datetime.fromtimestamp(day * 86400, timezone.utc).weekday()
        r = self.rng

        def at(hour: float) -> float:
            return midnight + hour * 3600

        def drive(to: str, km: float, kmh: float, **when: Any) -> None:
            self.queue.append({"kind": "drive", "to": to, "km": km, "kmh": kmh, **when})

        if weekday < 5:
            drive("work", r.uniform(20.5, 22.5), r.uniform(36, 46), at=at(7.4 + r.random() * 0.4))
            drive("home", r.uniform(20.5, 23.5), r.uniform(32, 44), at=at(16.8 + r.random() * 0.6))
            if r.random() < 0.35:
                drive("shop", r.uniform(5.5, 6.5), 31, at=at(19 + r.random() * 0.5))
                # Now and then a pickup stop short enough to count as the same trip.
                stop = 150 if r.random() < 0.3 else r.uniform(900, 2400)
                drive("home", r.uniform(5.5, 6.5), 31, after=stop)
        elif weekday == 5 and r.random() < 0.55:
            self.big_days.add(day)
            drive("lake", r.uniform(96, 100), r.uniform(82, 90), at=at(9 + r.random()))
            drive("fast", r.uniform(31, 33), 84, at=at(16 + r.random()))
            self.queue.append({"kind": "dc", "after": 120})
            drive("home", r.uniform(65, 68), 86, after=120)
        elif r.random() < 0.7:
            drive("shop", r.uniform(5.5, 6.5), 30, at=at(10.5 + r.random() * 3))
            drive("home", r.uniform(5.5, 6.5), 30, after=r.uniform(1500, 4000))

    # -- physics -----------------------------------------------------------

    def advance(self, to: float) -> None:
        while self.t < to:
            self._step(min(30.0, to - self.t))

    def _step(self, dt: float) -> None:
        self._plan()
        self.t += dt
        if self.drive:
            self._drive_step(dt)
        elif self.charge:
            self._charge_step(dt)
        else:
            self._parked_step()
        busy = self.drive or self.charge
        every = 900 if self.mode == "sparse" else 60
        if busy and self.t - self.last_report >= every:
            if self.mode != "sparse" or self.charge:
                self._report()

    def _parked_step(self) -> None:
        if self.plug_at and self.t >= self.plug_at:
            self.plug_at = None
            self.plugged = True
            self.charge_status = "SCHEDULED"
            self._report()
        if self.plugged and self.charge_status == "SCHEDULED":
            # Charge off-peak: start at 23:30 local.
            hour = (self.t + LOCAL_OFFSET_S) % 86400 / 3600
            if hour >= 23.5 or hour < 6:
                self.charge = {"type": "AC", "started": self.t, "soc_start": self.soc, "wall": 0.0}
                self.charge_status = "CHARGING"
                self._report()
                return
        if not self.queue:
            return
        step = self.queue[0]
        if step.get("at") and self.t < step["at"]:
            return
        if step.get("after") and self.t - self.parked_since < step["after"]:
            return
        self.queue.pop(0)
        if step["kind"] == "dc":
            self.plugged = True
            self.charge = {"type": "DC", "started": self.t, "soc_start": self.soc, "wall": 0.0}
            self.charge_status = "CHARGING"
            self.target, self.target_ts = 80, self.t
            self._report()
            return
        if self.plugged:
            self.plugged = False
            self.charge_status = "IDLE"
        if self.t - self.parked_since >= 4 * 3600:
            self.ta = self.ta_seconds = 0.0
        temp = outside_temp(self.t)
        consumption = (
            16.2 * (1 + 0.016 * max(0.0, 16 - temp))
            * (1 + ((step["kmh"] - 45) / 110) ** 2 * (2.4 if step["kmh"] > 45 else 0.3))
            * self.rng.uniform(0.94, 1.07)
        )
        name, lat, lon = SPOTS[step["to"]]
        self.drive = {
            **step, "done": 0.0, "consumption": consumption, "started": self.t,
            "soc_start": self.soc,
            "from": SPOTS[self.where][1:], "dest": (lat, lon),
            "bend": self.rng.uniform(-0.12, 0.12),
        }
        self.engine, self.engine_ts = "RUNNING", self.t
        self.locked = False
        if self.mode != "sparse":
            self._report()

    def _drive_step(self, dt: float) -> None:
        d = self.drive
        assert d
        km = min(d["kmh"] * dt / 3600, d["km"] - d["done"])
        d["done"] += km
        self.odo += km
        self.ta += km
        self.ta_seconds += dt
        self.tm += km
        self.tm_kwh += km * d["consumption"] / 100
        self.soc = max(self.soc - km * d["consumption"] / 100 / CAPACITY_KWH * 100, 1.0)
        f = d["done"] / d["km"]
        (lat0, lon0), (lat1, lon1) = d["from"], d["dest"]
        bulge = math.sin(math.pi * f) * d["bend"]
        self.lat = lat0 + (lat1 - lat0) * f + (lon1 - lon0) * bulge
        self.lon = lon0 + (lon1 - lon0) * f - (lat1 - lat0) * bulge
        self.loc_ts = self.t
        if d["done"] >= d["km"] - 1e-9:
            self.truth["drives"].append({
                "start": d["started"], "end": self.t, "km": d["km"], "to": d["to"],
                "kwh": (d["soc_start"] - self.soc) / 100 * CAPACITY_KWH,
            })
            self.where = d["to"]
            self.lat, self.lon = d["dest"]
            if self.where in self.no_gps:
                self.lat, self.lon, self.loc_ts = self.fix
            self.fix = (self.lat, self.lon, self.loc_ts)
            self.drive = None
            self.engine, self.engine_ts = "STOPPED", self.t
            self.parked_since = self.t
            self.locked = True
            tomorrow = int((self.t + LOCAL_OFFSET_S) // 86400) + 1
            if self.where == "home" and (self.soc < 60 or tomorrow in self.big_days):
                self.plug_at = self.t + 120
                self.target = 90 if tomorrow in self.big_days else 80
                self.target_ts = self.t
            self._report()

    def _power_kw(self) -> float:
        assert self.charge
        if self.charge["type"] == "AC":
            return AC_KW
        if self.soc < 35:
            return 148.0
        return max(148.0 - (self.soc - 35) * 2.1, 40.0)

    def _charge_step(self, dt: float) -> None:
        assert self.charge
        efficiency = 0.90 if self.charge["type"] == "AC" else 0.95
        self.charge["wall"] += self._power_kw() * dt / 3600
        self.soc += self._power_kw() * efficiency * dt / 3600 / CAPACITY_KWH * 100
        if self.soc >= self.target:
            self.soc = float(self.target)
            was_dc = self.charge["type"] == "DC"
            self.truth["charges"].append({
                "start": self.charge["started"], "end": self.t, "type": self.charge["type"],
                "wall_kwh": self.charge["wall"],
                "soc_start": self.charge["soc_start"], "soc_end": self.soc,
            })
            self.charge = None
            self.charge_status = "DONE"
            self._report()
            if was_dc:
                self.plugged = False
                self.charge_status = "IDLE"
                self.parked_since = self.t

    # -- what the car has told Volvo --------------------------------------

    def _report(self) -> None:
        """Publish the car's current state. Until the next report, the API serves this."""
        self.last_report = self.t
        now = _iso(self.t)

        def f(value: Any, unit: str | None = None, ts: str = now) -> dict[str, Any]:
            out = {"value": value, "timestamp": ts}
            if unit:
                out["unit"] = unit
            return out

        def e(value: Any, unit: str | None = None, ts: str = now) -> dict[str, Any]:
            out = {"status": "OK", "value": value, "updatedAt": ts}
            if unit:
                out["unit"] = unit
            return out

        power = self._power_kw() * 1000 if self.charge else 0
        remaining = (
            max(self.target - self.soc, 0) / 100 * CAPACITY_KWH / (self._power_kw() * 0.9) * 60
            if self.charge else 0
        )
        closed = f("CLOSED")
        ok = f("NO_WARNING")
        self.rep = {
            "energy": {
                "batteryChargeLevel": e(round(self.soc), "percentage"),
                "electricRange": e(round(self.soc / 100 * CAPACITY_KWH / 0.178), "km"),
                "chargerConnectionStatus": e("CONNECTED" if self.plugged else "DISCONNECTED"),
                "chargingStatus": e(self.charge_status),
                "chargingType": e(self.charge["type"] if self.charge else "NONE"),
                "chargerPowerStatus": e(
                    "PROVIDING_POWER" if self.charge else
                    "POWER_AVAILABLE_BUT_NOT_ACTIVATED" if self.plugged else "NO_POWER_AVAILABLE"),
                "estimatedChargingTimeToTargetBatteryChargeLevel": e(round(remaining), "minutes"),
                "chargingCurrentLimit": e(16, "ampere", _iso(self.target_ts)),
                "targetBatteryChargeLevel": e(self.target, "percentage", _iso(self.target_ts)),
                "chargingPower": e(round(power), "watts"),
            },
            "engine-status": {"engineStatus": f(self.engine, ts=_iso(self.engine_ts))},
            "command-accessibility": {
                "availabilityStatus": (
                    {"value": "UNAVAILABLE", "unavailableReason": "CAR_IN_USE", "timestamp": now}
                    if self.drive else f("AVAILABLE")
                ),
            },
            "odometer": {"odometer": f(int(self.odo), "km")},
            "statistics": {
                "averageEnergyConsumption": f(round(self.tm_kwh / self.tm * 100, 1), "kWh/100km"),
                "tripMeterManual": f(round(self.tm, 1), "km"),
                "averageSpeedAutomatic": f(
                    round(self.ta / (self.ta_seconds / 3600)) if self.ta_seconds > 60 else 0,
                    "km/h"),
                "tripMeterAutomatic": f(round(self.ta, 1), "km"),
                "distanceToEmptyBattery": f(
                    round(self.soc / 100 * CAPACITY_KWH / 0.178), "km"),
            },
            "location": {
                "type": "Feature",
                "properties": {"timestamp": _iso(self.loc_ts), "heading": "90"},
                "geometry": {"type": "Point",
                             "coordinates": [round(self.lon, 6), round(self.lat, 6), 0.0]},
            },
            "doors": {
                "centralLock": f("LOCKED" if self.locked else "UNLOCKED"),
                "frontLeftDoor": closed, "frontRightDoor": closed,
                "rearLeftDoor": closed, "rearRightDoor": closed,
                "hood": closed, "tailgate": closed,
            },
            "windows": {
                "frontLeftWindow": closed, "frontRightWindow": closed,
                "rearLeftWindow": closed, "rearRightWindow": closed,
                "sunroof": f("UNSPECIFIED"),
            },
            "tyres": {"frontLeft": ok, "frontRight": ok, "rearLeft": ok, "rearRight": ok},
            "warnings": {"brakeLightCenterWarning": ok, "lowBeamLeftWarning": ok,
                         "lowBeamRightWarning": ok},
            "diagnostics": {
                "serviceWarning": ok,
                "distanceToService": f(max(30000 - int(self.odo), 0), "km"),
                "timeToService": f(21, "months"),
                "washerFluidLevelWarning": ok,
            },
        }
        self._freeze_while_driving()

    def _freeze_while_driving(self) -> None:
        """EX40: odometer, trip statistics and position only move at switch-off."""
        if self.mode != "ex40":
            return
        if not self.drive:
            self.frozen = None
        elif self.frozen is None:
            self.frozen = {k: self.rep[k] for k in ("odometer", "statistics", "location")}
        else:
            self.rep.update(self.frozen)

    # -- HTTP --------------------------------------------------------------

    def respond(self, method: str, path: str) -> tuple[int, Any]:
        base = "/connected-vehicle/v2/vehicles"
        if path == base:
            return 200, {"data": [{"vin": VIN}]}
        if path == f"{base}/{VIN}":
            return 200, {"data": {
                "vin": VIN, "modelYear": 2026, "gearbox": "AUTOMATIC", "fuelType": "ELECTRIC",
                "externalColour": "Vapour Grey", "batteryCapacityKWH": CAPACITY_KWH,
                "images": {"exteriorImageUrl": "", "internalImageUrl": ""},
                "descriptions": {"model": "EX40", "upholstery": "null", "steering": "LEFT"},
            }}
        if path == f"/energy/v2/vehicles/{VIN}/state":
            return 200, self.rep["energy"]
        if path == f"/energy/v2/vehicles/{VIN}/capabilities":
            return 200, {"getEnergyState": {"isSupported": True}}
        if path == f"/location/v1/vehicles/{VIN}/location":
            return 200, {"data": self.rep["location"]}
        if path == f"{base}/{VIN}/commands" and method == "GET":
            return 200, {"data": [
                {"command": c, "href": f"/v2/vehicles/{VIN}/commands/{c.lower()}"}
                for c in ("LOCK", "UNLOCK", "CLIMATIZATION_START", "CLIMATIZATION_STOP")
            ]}
        if path.startswith(f"{base}/{VIN}/commands/") and method == "POST":
            if path.endswith("/lock"):
                self.locked = True
                self._report()
            return 200, {"data": {"vin": VIN, "invokeStatus": "COMPLETED", "message": ""}}
        resource = path.removeprefix(f"{base}/{VIN}/")
        if resource in self.rep:
            return 200, {"data": self.rep[resource]}
        return 404, {"error": {"message": "NOT_FOUND", "description": path}}


def transport(car: SimCar, clock: Clock | Callable[[], Clock]) -> httpx.MockTransport:
    """`clock` may be a function returning the clock, so the caller can swap clocks
    without replacing the fake server (and the sign-in it has handed out)."""
    current = clock if callable(clock) and not isinstance(clock, Clock) else (lambda: clock)
    # Like Volvo ID, every refresh hands out a new refresh token and the old one
    # stops working, and access tokens expire.
    tokens = {"refresh": "sim-refresh", "access": "sim-access", "expires": float("inf"), "n": 0}

    async def handle(request: httpx.Request) -> httpx.Response:
        # A real round trip lets other requests run meanwhile; so must this.
        await asyncio.sleep(0)
        car.advance(current().now())
        if request.url.host.startswith("volvoid"):
            form = parse_qs(request.content.decode())
            if car.revoked or (
                form.get("grant_type") == ["refresh_token"]
                and form.get("refresh_token") != [tokens["refresh"]]
            ):
                return httpx.Response(400, json={
                    "error": "invalid_grant",
                    "error_description": "unknown, invalid, or expired refresh token"})
            tokens["n"] += 1
            tokens["refresh"] = f"sim-refresh-{tokens['n']}"
            tokens["access"] = f"sim-access-{tokens['n']}"
            tokens["expires"] = current().now() + 1800
            return httpx.Response(200, json={
                "access_token": tokens["access"], "refresh_token": tokens["refresh"],
                "token_type": "Bearer", "expires_in": 1800,
            })
        if (car.revoked or current().now() > tokens["expires"]
                or request.headers.get("authorization") != f"Bearer {tokens['access']}"):
            return httpx.Response(401, json={"error": {"message": "UNAUTHORIZED"}})
        status, body = car.respond(request.method, request.url.path)
        return httpx.Response(status, content=json.dumps(body),
                              headers={"content-type": "application/json"})

    return httpx.MockTransport(handle)


class SimLookups:
    """Offline stand-in for the address and weather lookups."""

    async def place(self, lat: float, lon: float) -> str | None:
        for name, slat, slon in SPOTS.values():
            if abs(lat - slat) < 0.002 and abs(lon - slon) < 0.002:
                return name
        return None

    async def temperature(self, lat: float, lon: float, ts: float) -> float | None:
        return outside_temp(ts)


def connect(db: Any, clock: Clock) -> None:
    """Put the app straight into the 'signed in' state against the simulator."""
    db.set("volvo_creds", {"api_key": "sim", "client_id": "sim", "client_secret": "sim",
                           "redirect_uri": "http://localhost/sim"})
    db.set("volvo_tokens", {"access_token": "none", "refresh_token": "sim-refresh",
                            "expires_at": 0})
    db.set("volvo_vin", VIN)
    db.set("volvo_consent_at", clock.now())
    db.set("volvo_commands", ["LOCK", "UNLOCK", "CLIMATIZATION_START", "CLIMATIZATION_STOP"])
    db.set("volvo_vehicle", {
        "vin": VIN, "modelYear": 2026, "batteryCapacityKWH": CAPACITY_KWH,
        "externalColour": "Vapour Grey", "descriptions": {"model": "EX40"},
    })


def default_start(days: int) -> float:
    start = datetime.now(timezone.utc) - timedelta(days=days)
    return start.replace(hour=3, minute=0, second=0, microsecond=0).timestamp()
