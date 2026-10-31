"""Polls Volvo, records what changed, and runs the detector.

Volvo allows 10,000 calls a day, so the cadence adapts: a handful of cheap
calls every few minutes while parked, every minute while driving or charging,
and the rarely-changing things (doors, tyres, warnings) on slow timers.
"""

import asyncio
import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .clock import Clock
from .db import DB, get_settings
from .detect import OBS_FIELDS, Detector, Obs
from .enrich import Enricher, describe, send_webhook
from .fold import field_value, fold
from .raw import RawLog, backup
from .volvo import (
    DAILY_LIMIT, VolvoAuthError, VolvoClient, VolvoError, VolvoForbidden,
    VolvoRateLimited, parse_ts,
)

log = logging.getLogger(__name__)

# Needed on every poll to notice trips and charging.
CORE = ("energy", "engine-status", "command-accessibility", "odometer")
# Seconds between polls of everything else: (while parked, while driving or charging).
SLOW = {
    "statistics": (900, 60),
    "location": (1800, 60),
    "doors": (1800, 300),
    "windows": (1800, 300),
    "tyres": (3600, 3600),
    "warnings": (3600, 3600),
    "diagnostics": (3600, 3600),
    # These need scopes added after the first sign-in; until the next sign-in
    # Volvo refuses them and they are retried once a day.
    "brakes": (3600, 3600),
    "engine": (3600, 3600),
    "fuel": (3600, 3600),
}
# Things that rarely change, kept in the raw log once a day.
STATIC_INTERVAL_S = 86400
ACTIVE_LINGER_S = 600
WAITING_INTERVAL_S = 120
SOFT_BUDGET = 0.85
HARD_BUDGET = 0.97
class Collector:
    def __init__(self, db: DB, client: VolvoClient, clock: Clock,
                 enricher: Enricher | None = None, backups: Path | None = None) -> None:
        self.db = db
        self.client = client
        self.backups = backups
        self.raw = RawLog(db)
        self._poll_t: float | None = None
        client.recorder = lambda method, endpoint, status, body: self.raw.record(
            self._poll_t or self.clock.now(), method, endpoint, status, body)
        self.clock = clock
        self.enricher = enricher
        self.detector = Detector(db, self.capacity)
        cols = ", ".join(OBS_FIELDS)
        last = db.one(f"SELECT {cols} FROM observations ORDER BY id DESC LIMIT 1")
        self.obs = Obs(**dict(last)) if last else Obs()
        self.due: dict[str, float] = {}
        self.off: dict[str, float] = {}
        self.last_change = 0.0
        self.status: dict[str, Any] = {"last_poll": None, "last_error": None, "paused": False,
                                       "mode": "idle", "next_poll": None}
        self.listeners: set[asyncio.Queue[str]] = set()
        self.live = True  # False while seeding: no webhooks
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()

    def capacity(self) -> float | None:
        return (
            get_settings(self.db)["usable_capacity_kwh"]
            or (self.db.get("volvo_vehicle") or {}).get("batteryCapacityKWH")
        )

    def broadcast(self, message: str = "update") -> None:
        for q in self.listeners:
            q.put_nowait(message)

    def wake(self) -> None:
        self._wake.set()

    # -- one poll ----------------------------------------------------------

    async def tick(self, force: bool = False) -> float:
        """Poll once. Returns how many seconds to wait before the next poll."""
        async with self._lock:
            # Every answer in one poll gets the same time in the raw log, so a
            # rebuild can group them back into the same poll.
            self._poll_t = self.clock.now()
            try:
                delay = await self._tick(force)
            finally:
                self._poll_t = None
        self.status["next_poll"] = self.clock.now() + delay
        self.broadcast()
        return delay

    async def _tick(self, force: bool) -> float:
        now = self.clock.now()
        settings = get_settings(self.db)
        if not self.client.connected():
            self.status["mode"] = "disconnected"
            return 30
        if self.db.get("volvo_auth_error"):
            self.status["mode"] = "needs_reauth"
            return 300

        used = self.client.calls_today() / DAILY_LIMIT
        self.status["paused"] = used >= HARD_BUDGET
        if self.status["paused"] and not force:
            midnight = (int(now) // 86400 + 1) * 86400
            return min(midnight - now + 5, 900)

        d = self.detector.s
        driving = bool(d["trip"])
        active = driving or bool(d["charge"]) or now - self.last_change < ACTIVE_LINGER_S

        wanted = set(CORE)
        for res, (parked_s, active_s) in SLOW.items():
            if force or now >= self.due.get(res, 0):
                wanted.add(res)
                self.due[res] = now + (active_s if driving else parked_s)
        wanted = {r for r in wanted if now >= self.off.get(r, 0)}

        try:
            results = await self._fetch(wanted, now)
            obs = self._merge(results, now)
            # When the car starts or stops moving, grab trip meter, position and
            # lock state in the same poll so the trip's first and last readings line up.
            movement = (obs.odometer_km, obs.engine, obs.availability) != (
                self.obs.odometer_km, self.obs.engine, self.obs.availability)
            extra = {"statistics", "location", "doors"} - wanted
            extra = {r for r in extra if now >= self.off.get(r, 0)}
            if movement and extra:
                results = await self._fetch(extra, now)
                self.obs, saved = obs, self.obs
                obs = self._merge(results, now)
                self.obs = saved
            await self._refresh_static(now, force)
        except VolvoAuthError as e:
            log.warning("Volvo sign-in no longer valid: %s", e)
            self.db.set("volvo_auth_error", {"at": now, "message": str(e)})
            self.status["mode"] = "needs_reauth"
            self.db.insert("events", {"ts": now, "type": "needs_reauth",
                                      "detail": json.dumps({"message": str(e)})})
            await self._notify([{"ts": now, "type": "needs_reauth"}], settings)
            return 300
        except VolvoRateLimited:
            self.status["last_error"] = "Volvo is rate limiting us; backing off for 15 minutes."
            return 900

        self.status["last_poll"] = now
        if not obs.same_data(self.obs):
            self.db.insert("observations", asdict(obs))
            events = self.detector.feed(obs)
            self.last_change = now
            await self._notify(events, settings)
        self.obs = obs

        d = self.detector.s
        active = bool(d["trip"]) or bool(d["charge"]) or now - self.last_change < ACTIVE_LINGER_S
        self.status["mode"] = (
            "driving" if d["trip"] else "charging" if d["charge"] else
            "active" if active else "idle"
        )
        idle_s = max(settings["idle_interval_s"], 60)
        if active and used < SOFT_BUDGET:
            return max(settings["active_interval_s"], 30)
        if obs.connection == "CONNECTED" and obs.charging_status not in ("DONE", "CHARGING"):
            # Plugged in and waiting (scheduled, paused): look more often so the
            # start of charging is caught within a couple of minutes.
            return min(idle_s, WAITING_INTERVAL_S)
        return idle_s

    async def _fetch(self, resources: set[str], now: float) -> dict[str, dict[str, Any]]:
        names = sorted(resources)

        async def one(res: str) -> dict[str, Any]:
            if res == "energy":
                return await self.client.energy_state()
            if res == "location":
                return await self.client.location()
            return await self.client.connected_fields(res)

        answers = await asyncio.gather(*(one(r) for r in names), return_exceptions=True)
        results: dict[str, dict[str, Any]] = {}
        errors = []
        for res, answer in zip(names, answers):
            if isinstance(answer, (VolvoAuthError, VolvoRateLimited)):
                raise answer
            if isinstance(answer, VolvoForbidden):
                # Missing scope or unsupported region. Ask again tomorrow.
                self.off[res] = now + 86400
                self.db.set("volvo_unavailable", sorted(
                    set(self.db.get("volvo_unavailable", [])) | {res}))
            elif isinstance(answer, VolvoError):
                errors.append(f"{res}: {answer}")
            elif isinstance(answer, BaseException):
                raise answer
            elif not answer and res not in CORE:
                self.off[res] = now + 6 * 3600
            else:
                results[res] = answer
        self.status["last_error"] = "; ".join(errors) or None
        return results

    def _merge(self, results: dict[str, dict[str, Any]], now: float) -> Obs:
        """Keep the latest value of every field, and fold this poll into a new Obs."""
        for res, fields in results.items():
            if res == "location":
                coords = (fields.get("geometry") or {}).get("coordinates") or []
                if len(coords) >= 2:
                    props = fields.get("properties") or {}
                    self._state("location", {"lat": coords[1], "lon": coords[0],
                                             "heading": props.get("heading")},
                                None, parse_ts(props.get("timestamp")), now)
                continue
            for key, f in fields.items():
                if isinstance(f, dict):
                    ts = parse_ts(f.get("timestamp") or f.get("updatedAt"))
                    value = field_value(key, f)
                    if key == "centralLock":
                        self._lock_event(value, ts or now)
                    self._state(key, value, f.get("unit"), ts, now)
        return fold(self.obs, results, now)

    def _state(self, key: str, value: Any, unit: str | None, ts: float | None, now: float) -> None:
        self.db.x(
            "INSERT OR REPLACE INTO state (key, value, unit, ts, fetched_at) VALUES (?, ?, ?, ?, ?)",
            key, json.dumps(value), unit, ts, now,
        )

    def _lock_event(self, value: Any, ts: float) -> None:
        row = self.db.one("SELECT value FROM state WHERE key = 'centralLock'")
        if row and json.loads(row["value"]) != value and value in ("LOCKED", "UNLOCKED"):
            self.db.insert("events", {"ts": ts, "type": value.lower(), "detail": "{}"})

    async def _refresh_static(self, now: float, force: bool) -> None:
        """Once a day: car details, charging capabilities and available commands.
        Mostly so the raw log has them; also keeps the app's copies current."""
        if not force and now < self.due.get("static", 0):
            return
        self.due["static"] = now + STATIC_INTERVAL_S
        for name, call in (("vehicle", self.client.vehicle),
                           ("capabilities", self.client.energy_capabilities),
                           ("commands", self.client.available_commands)):
            try:
                answer = await call()
            except (VolvoAuthError, VolvoRateLimited):
                raise
            except VolvoError:
                continue
            if name == "vehicle" and answer:
                self.db.set("volvo_vehicle", answer)
            elif name == "commands" and answer:
                self.db.set("volvo_commands", answer)

    def _daily_backup(self) -> None:
        if not self.backups:
            return
        day = datetime.fromtimestamp(self.clock.now(), timezone.utc).strftime("%Y-%m-%d")
        if self.db.get("last_backup_day") == day:
            return
        try:
            backup(self.db, self.backups, "daily")
            self.db.set("last_backup_day", day)
        except Exception:
            log.exception("Daily backup failed")

    async def _notify(self, events: list[dict[str, Any]], settings: dict[str, Any]) -> None:
        url = settings["webhook_url"]
        if not (self.live and url):
            return
        for e in events:
            if e["type"] in settings["webhook_events"]:
                await send_webhook(url, *describe(e, settings))

    # -- loop --------------------------------------------------------------

    async def run(self) -> None:
        while True:
            # Clear before polling, not after: a wake() that arrives mid-poll (say,
            # sign-in finishing) must still cut the following sleep short.
            self._wake.clear()
            try:
                delay = await self.tick()
                if self.enricher:
                    if await self.enricher.run(self.clock.now()):
                        self.broadcast()
                self._daily_backup()
            except Exception:
                log.exception("Poll failed")
                delay = 120
            sleeper = asyncio.ensure_future(self.clock.sleep(delay))
            waker = asyncio.ensure_future(self._wake.wait())
            _, pending = await asyncio.wait({sleeper, waker}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
