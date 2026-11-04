"""Turns the observation log into trips and charging sessions.

The detector is deterministic: feeding it the same observations in the same
order always produces the same trips and charges. That is what makes
`rebuild()` possible when the rules here change.

Volvo's API is not live. The car decides when to report, and each value
carries the time the car measured it. So wherever the car's timestamp is
plausible we use it instead of the time we happened to poll.
"""

import json
import math
from dataclasses import asdict, dataclass, fields
from typing import Any, Callable

from .db import DB, get_settings

CLOCK_SKEW_S = 120
# With no engine/availability signal, a trip ends after this long without movement.
IDLE_CLOSE_S = 600
# A charge that resumes this soon on the same plug-in is the same session.
CHARGE_RESUME_S = 1800
# After a trip closes, late odometer/location/SoC reports still update it for this long.
LATE_PATCH_S = 600
# Power samples further apart than this are not integrated across...
MAX_SAMPLE_GAP_S = 600
# ...unless power held steady on both sides of the gap. The EX40 can go 20
# minutes between reports while charging at a constant rate overnight.
MAX_STEADY_GAP_S = 3600
STEADY_POWER_TOLERANCE = 0.1
FALLBACK_CAPACITY_KWH = 75.0


@dataclass
class Obs:
    t: float = 0.0
    soc: float | None = None
    soc_ts: float | None = None
    range_km: float | None = None
    charging_status: str | None = None
    charge_ts: float | None = None
    connection: str | None = None
    charge_type: str | None = None
    power_w: float | None = None
    target_soc: float | None = None
    mins_to_target: float | None = None
    engine: str | None = None
    engine_ts: float | None = None
    availability: str | None = None
    availability_ts: float | None = None
    odometer_km: float | None = None
    odometer_ts: float | None = None
    trip_auto_km: float | None = None
    avg_speed_auto: float | None = None
    stats_ts: float | None = None
    lat: float | None = None
    lon: float | None = None
    location_ts: float | None = None
    avg_consumption: float | None = None  # kWh/100 km over the manual trip meter
    trip_manual_km: float | None = None

    def same_data(self, other: "Obs") -> bool:
        a, b = asdict(self), asdict(other)
        a.pop("t"), b.pop("t")
        return a == b

    def car_time(self) -> float | None:
        """Most recent moment the car itself reported anything."""
        stamps = [
            ts
            for ts in (self.soc_ts, self.charge_ts, self.engine_ts,
                       self.availability_ts, self.odometer_ts, self.location_ts)
            if ts
        ]
        return max(stamps) if stamps else None


OBS_FIELDS = [f.name for f in fields(Obs)]


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (
        math.sin((p2 - p1) / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    )
    return 6371000 * 2 * math.asin(math.sqrt(a))


def integrate_power(samples: list[tuple[float, float | None]]) -> tuple[float, float]:
    """Trapezoid-integrate (ts, watts) samples. Returns (kWh, seconds covered)."""
    kwh = covered = 0.0
    points = [(ts, w) for ts, w in samples if w is not None]
    for (t0, w0), (t1, w1) in zip(points, points[1:]):
        dt = t1 - t0
        steady = abs(w1 - w0) <= STEADY_POWER_TOLERANCE * max(w0, w1, 1)
        if 0 < dt <= (MAX_STEADY_GAP_S if steady else MAX_SAMPLE_GAP_S):
            kwh += (w0 + w1) / 2 * dt / 3_600_000
            covered += dt
    return kwh, covered


def car_energy(r: Any, distance: float, capacity_kwh: float) -> float | None:
    """Trip energy from the car's own counter, or None when it can't be trusted.

    Average consumption times the manual trip meter is the energy the car
    measured since that meter was reset; its change across a trip is the trip.
    The average has only 0.1 kWh/100 km resolution, so the error grows with the
    meter reading. Use it only while it beats one percent of battery.
    """
    c0, c1, m0, m1 = r["cons_start"], r["cons_end"], r["tm_start"], r["tm_end"]
    if None in (c0, c1, m0, m1):
        return None
    # The meter must have counted this trip: not reset, and agreeing on distance.
    if m1 < m0 or abs((m1 - m0) - distance) > 1.5:
        return None
    error = 0.05 * (m0 + m1) / 100 + 0.1 * c1 / 100
    if error > capacity_kwh / 100:
        return None
    return max(c1 * m1 - c0 * m0, 0) / 100


class Detector:
    def __init__(self, db: DB, capacity_kwh: Callable[[], float | None]) -> None:
        self.db = db
        self._capacity = capacity_kwh
        self.s: dict[str, Any] = db.get("detector_state") or {
            "prev": None,       # last observation
            "park": None,       # last known state while not on a trip
            "trip": None,       # open trip bookkeeping
            "last_trip": None,  # most recently closed trip, for merging and late data
            "charge": None,
            "last_charge": None,
        }
        self.events: list[dict[str, Any]] = []

    # -- helpers -----------------------------------------------------------

    def capacity(self) -> float:
        return self._capacity() or FALLBACK_CAPACITY_KWH

    def _when(self, car_ts: float | None, o: Obs, prev: Obs | None) -> float:
        """The car's timestamp if it falls since the previous poll, else the poll time."""
        lo = prev.t if prev else o.t - 3600
        if car_ts and lo - CLOCK_SKEW_S < car_ts <= o.t + CLOCK_SKEW_S:
            return min(car_ts, o.t)
        return o.t

    def _event(self, ts: float, type_: str, **detail: Any) -> None:
        self.db.insert("events", {"ts": ts, "type": type_, "detail": json.dumps(detail)})
        self.events.append({"ts": ts, "type": type_, **detail})

    # -- main entry --------------------------------------------------------

    def feed(self, o: Obs) -> list[dict[str, Any]]:
        """Process one observation. Returns the events it produced."""
        self.events = []
        prev = Obs(**self.s["prev"]) if self.s["prev"] else None

        self._plug_events(o, prev)
        self._trip(o, prev)
        self._charge(o, prev)

        if not self.s["trip"]:
            lat, lon = self._position(o)
            self.s["park"] = {
                "car_t": o.car_time() or o.t,
                "odo": o.odometer_km,
                "soc": o.soc,
                "ta": o.trip_auto_km,
                "cons": o.avg_consumption,
                "tm": o.trip_manual_km,
                "lat": lat,
                "lon": lon,
            }
        self.s["prev"] = asdict(o)
        self.db.set("detector_state", self.s)
        return self.events

    def _plug_events(self, o: Obs, prev: Obs | None) -> None:
        if not prev or not prev.connection or o.connection == prev.connection:
            return
        if o.connection == "CONNECTED":
            self._event(self._when(o.charge_ts, o, prev), "plugged")
        elif o.connection == "DISCONNECTED":
            self._event(self._when(o.charge_ts, o, prev), "unplugged")

    # -- trips -------------------------------------------------------------

    def _trip(self, o: Obs, prev: Obs | None) -> None:
        driving = o.engine == "RUNNING" or o.availability == "CAR_IN_USE"
        moved = bool(
            prev
            and o.odometer_km is not None
            and prev.odometer_km is not None
            and o.odometer_km > prev.odometer_km
        )
        trip = self.s["trip"]

        if not trip and self._settle_distance(o):
            # That odometer step belonged to the last trip, not a new one.
            moved = False

        if not trip:
            # A running "engine" while plugged in is preconditioning, not a drive.
            if moved or (driving and o.connection != "CONNECTED"):
                trip = self._open_trip(o, prev, driving)
            else:
                self._patch_last_trip(o, prev)
                return

        trip["speed_hint"] = o.avg_speed_auto
        if driving:
            trip["saw_signal"] = True
        if moved:
            trip["moves"] += 1
        if moved or driving:
            trip["last_active"] = (
                self._when(o.odometer_ts, o, prev) if moved else o.t
            )
        if moved and not trip["announced"]:
            trip["announced"] = True
            self._event(trip["started_at"], "trip_started", trip_id=trip["id"])

        self.db.update("trips", trip["id"], {
            "odo_end": o.odometer_km, "soc_end": o.soc, "ta_end": o.trip_auto_km,
            "cons_end": o.avg_consumption, "tm_end": o.trip_manual_km,
        })
        self._trip_point(trip["id"], o)

        stopped = o.engine is not None and not driving
        # Only believe "stopped" if that field is actually keeping up with the car.
        trusted = trip["saw_signal"] or bool(
            o.engine_ts and o.engine_ts >= (o.odometer_ts or 0) - CLOCK_SKEW_S
        )
        if moved and not (stopped and trusted):
            return
        if stopped and trusted:
            end = self._when(o.engine_ts, o, prev)
            if trip["saw_signal"]:
                end = max(end, trip["started_at"])
        elif o.charging_status == "CHARGING" or o.t - trip["last_active"] >= IDLE_CLOSE_S:
            end = max(trip["last_active"], trip["started_at"])
        else:
            return
        self._close_trip(trip, end, o)

    def _settle_distance(self, o: Obs) -> bool:
        """Give a trip still waiting for its distance the first odometer reading
        the car takes after it set off. Returns True if it did."""
        last = self.s["last_trip"]
        if not last or not last.get("no_distance") or not o.odometer_ts:
            return False
        # The stale reading the trip already has was taken as it set off.
        if o.odometer_km is None or o.odometer_ts <= last["started_at"] + CLOCK_SKEW_S:
            return False
        last["no_distance"] = False
        self.db.update("trips", last["id"], {
            "odo_end": o.odometer_km, "ta_end": o.trip_auto_km,
            "cons_end": o.avg_consumption, "tm_end": o.trip_manual_km,
        })
        row = self._finalize_trip(last["id"], last | {"settled": True})
        if row is None:
            # The odometer says the car never moved after all.
            self.s["last_trip"] = None
        else:
            self._event(o.odometer_ts, "trip_distance", trip_id=last["id"],
                        distance_km=row["distance_km"])
        # The next trip starts from here, not from the stale reading.
        if self.s["park"]:
            self.s["park"] |= {"odo": o.odometer_km, "ta": o.trip_auto_km,
                               "cons": o.avg_consumption, "tm": o.trip_manual_km}
        return True

    def _open_trip(self, o: Obs, prev: Obs | None, driving: bool) -> dict[str, Any]:
        park = self.s["park"] or {}
        if driving:
            ts = o.engine_ts if o.engine == "RUNNING" else o.availability_ts
            started = self._when(ts, o, prev)
        else:
            started = self._when(o.odometer_ts, o, prev)

        last = self.s["last_trip"]
        gap = get_settings(self.db)["merge_gap_s"]
        if last and not last.get("charged") and started - last["ended_at"] <= gap:
            # Short stop: carry on with the previous trip.
            self.db.update("trips", last["id"], {"ended_at": None})
            self.db.x(
                "DELETE FROM events WHERE type = 'trip_ended' AND ts = ?", last["ended_at"])
            self.s["last_trip"] = None
            trip = {
                "id": last["id"], "started_at": last["started_at"],
                "saw_signal": last["saw_signal"], "moves": last["moves"],
                "announced": True, "last_active": started,
                "min_start": last["min_start"],
            }
        else:
            lat, lon = park.get("lat"), park.get("lon")
            start: dict[str, Any] = {"start_lat": lat, "start_lon": lon}
            if lat is None:
                # No GPS where it was parked: use the position entered by hand
                # for the end of the previous trip, if there is one.
                before = self.db.one(
                    "SELECT end_lat, end_lon, end_place FROM trips WHERE end_manual = 1 "
                    "AND id = (SELECT id FROM trips ORDER BY started_at DESC LIMIT 1)")
                if before:
                    lat, lon = before["end_lat"], before["end_lon"]
                    start = {"start_lat": lat, "start_lon": lon,
                             "start_place": before["end_place"], "start_manual": 1}
            trip_id = self.db.insert("trips", {
                "started_at": started,
                "odo_start": park.get("odo", o.odometer_km),
                "soc_start": park.get("soc") if park.get("soc") is not None else o.soc,
                "ta_start": park.get("ta"),
                "cons_start": park.get("cons"), "tm_start": park.get("tm"),
                **start,
            })
            if lat is not None:
                self.db.insert("trip_points", {
                    "trip_id": trip_id, "ts": started, "lat": lat, "lon": lon,
                })
            trip = {
                "id": trip_id, "started_at": started, "saw_signal": False,
                "moves": 0, "announced": False, "last_active": started,
                # The car was known to be parked at this time, so the trip began later.
                "min_start": park.get("car_t"),
            }
        # Positions the car reported before this moment no longer say where it is.
        self.s["moved_at"] = started
        self.s["trip"] = trip
        return trip

    def _position(self, o: Obs) -> tuple[float | None, float | None]:
        """Where the car is, if it reported a position since it last set off.

        Without GPS (an underground car park) the car keeps serving the last
        fix it had, which is where the previous trip began, not where it is now.
        """
        moved_at = self.s.get("moved_at")
        if o.lat is None or not o.location_ts or (moved_at and o.location_ts <= moved_at):
            return None, None
        return o.lat, o.lon

    def _trip_point(self, trip_id: int, o: Obs) -> None:
        if o.lat is None or o.lon is None or not o.location_ts:
            return
        last = self.db.one(
            "SELECT ts, lat, lon FROM trip_points WHERE trip_id = ? ORDER BY ts DESC LIMIT 1",
            trip_id,
        )
        # Same place with a newer fix is kept: that is a trip back to where it began.
        if last and o.location_ts <= last["ts"]:
            return
        self.db.insert("trip_points", {
            "trip_id": trip_id, "ts": o.location_ts, "lat": o.lat, "lon": o.lon,
        })

    def _close_trip(self, trip: dict[str, Any], ended_at: float, o: Obs) -> None:
        self.s["trip"] = None
        self.db.update("trips", trip["id"], {"ended_at": ended_at})
        row = self._finalize_trip(trip["id"], trip)
        if not row:
            return
        self.s["last_trip"] = {
            "id": trip["id"], "started_at": row["started_at"], "ended_at": ended_at,
            "saw_signal": trip["saw_signal"], "moves": trip["moves"],
            "min_start": trip["min_start"], "speed_hint": trip.get("speed_hint"),
            "charged": False, "no_end": row["end_lat"] is None,
            "no_distance": row["distance_km"] is None,
        }
        self._event(
            ended_at, "trip_ended", trip_id=trip["id"],
            distance_km=row["distance_km"], energy_kwh=row["energy_kwh"],
        )

    def _finalize_trip(self, trip_id: int, trip: dict[str, Any]) -> dict[str, Any] | None:
        """Compute the derived numbers for a closed trip. Deletes it if the car never moved."""
        r = self.db.one("SELECT * FROM trips WHERE id = ?", trip_id)
        if r is None:
            return None
        d_odo = (
            r["odo_end"] - r["odo_start"]
            if r["odo_end"] is not None and r["odo_start"] is not None else None
        )
        # The automatic trip meter has 0.1 km resolution but resets to zero after a
        # long stop, so it reads either "start + trip" or just "trip". Take whichever
        # reading agrees with the whole-km odometer; otherwise trust the odometer.
        candidates = []
        if r["ta_end"] is not None:
            candidates.append(r["ta_end"])
            if r["ta_start"] is not None and r["ta_end"] >= r["ta_start"]:
                candidates.insert(0, r["ta_end"] - r["ta_start"])
        if d_odo is None:
            distance = candidates[0] if candidates else None
        else:
            close = [c for c in candidates if abs(c - d_odo) <= 1.5]
            distance = min(close, key=lambda c: abs(c - d_odo)) if close else d_odo

        waiting = not distance or distance <= 0
        if waiting and not (trip["saw_signal"] and not trip.get("settled")):
            self.db.x("DELETE FROM trip_points WHERE trip_id = ?", trip_id)
            self.db.x("DELETE FROM trips WHERE id = ?", trip_id)
            return None
        if waiting:
            # The car said it was driving, but its odometer hasn't caught up: the
            # switch-off report can go missing where there is no signal. Keep the
            # trip; the next odometer report settles it (_settle_distance).
            used = (
                r["soc_start"] - r["soc_end"]
                if r["soc_start"] is not None and r["soc_end"] is not None else None
            )
            values = {
                "distance_km": None, "efficiency": None, "avg_speed_kmh": None,
                "energy_kwh": round(max(used, 0) / 100 * self.capacity(), 2) if used is not None else None,
                "energy_source": "soc" if used is not None else None,
            }
            self.db.update("trips", trip_id, values)
            return dict(r) | values

        ended = r["ended_at"]
        started = min(r["started_at"], ended)
        estimated = 0
        if not trip["saw_signal"] and trip["moves"] <= 1:
            # The whole trip arrived as one report, so nobody saw it start.
            speed = max(trip.get("speed_hint") or 40.0, 10.0)
            started = ended - distance / speed * 3600
            if trip["min_start"]:
                started = max(started, trip["min_start"])
            started = min(started, ended)
            estimated = 1

        used_pct = (
            r["soc_start"] - r["soc_end"]
            if r["soc_start"] is not None and r["soc_end"] is not None else None
        )
        from_soc = max(used_pct, 0) / 100 * self.capacity() if used_pct is not None else None
        from_car = car_energy(r, distance, self.capacity())
        if from_car is not None:
            energy, source = from_car, "car"
            efficiency = energy / distance * 100 if distance >= 1 else None
        else:
            energy, source = from_soc, "soc" if from_soc is not None else None
            # SoC is reported in whole percent; short trips can't give a usable efficiency.
            efficiency = (
                energy / distance * 100
                if energy is not None and distance >= 5 and used_pct >= 2 else None
            )
        hours = (ended - started) / 3600
        values = {
            "started_at": started,
            "start_estimated": estimated,
            "distance_km": round(distance, 1),
            "energy_kwh": round(energy, 2) if energy is not None else None,
            "energy_soc_kwh": round(from_soc, 2) if from_soc is not None else None,
            "energy_source": source,
            "efficiency": round(efficiency, 1) if efficiency is not None else None,
            "avg_speed_kmh": round(distance / hours, 1) if hours > 1 / 60 else None,
        }
        # The end is the last position reported during or after the trip. The
        # start point doesn't count: no new fix means the end is unknown.
        points = self.db.q(
            "SELECT lat, lon FROM trip_points WHERE trip_id = ? ORDER BY ts", trip_id)
        if r["start_lat"] is not None:
            points = points[1:]
        end = points[-1] if points else None
        if end:
            # A real fix beats one entered by hand.
            values |= {"end_lat": end["lat"], "end_lon": end["lon"], "end_manual": 0}
            if r["end_manual"]:
                values["end_place"] = None
        elif not r["end_manual"]:
            values |= {"end_lat": None, "end_lon": None}
        self.db.update("trips", trip_id, values)
        return dict(r) | values

    def _patch_last_trip(self, o: Obs, prev: Obs | None) -> None:
        """Apply reports that arrive just after a trip closed (final location, SoC, odometer)."""
        last = self.s["last_trip"]
        if not last or not prev:
            return
        if o.t - last["ended_at"] > LATE_PATCH_S:
            # A trip that ended with no position gets the first fix the car
            # reports while still parked there, however late.
            if last.get("no_end") and self._position(o) != (None, None):
                self._trip_point(last["id"], o)
                self.db.update("trips", last["id"], {"end_place": None, "temp_c": None})
                self._finalize_trip(last["id"], last)
                last["no_end"] = False
            return
        changed = (
            (o.lat, o.lon) != (prev.lat, prev.lon)
            or o.trip_auto_km != prev.trip_auto_km
            or o.trip_manual_km != prev.trip_manual_km
            or o.avg_consumption != prev.avg_consumption
            or (o.soc != prev.soc and o.charging_status != "CHARGING")
        )
        if not changed:
            return
        values: dict[str, Any] = {
            "odo_end": o.odometer_km, "ta_end": o.trip_auto_km,
            "cons_end": o.avg_consumption, "tm_end": o.trip_manual_km,
        }
        if o.charging_status != "CHARGING" and not last.get("charged"):
            values["soc_end"] = o.soc
        last["speed_hint"] = o.avg_speed_auto
        self.db.update("trips", last["id"], values)
        self._trip_point(last["id"], o)
        if (o.lat, o.lon) != (prev.lat, prev.lon):
            self.db.update("trips", last["id"], {"end_place": None})
        row = self._finalize_trip(last["id"], last)
        last["no_end"] = bool(row) and row["end_lat"] is None

    # -- charging ----------------------------------------------------------

    def _charge(self, o: Obs, prev: Obs | None) -> None:
        charging = o.charging_status == "CHARGING"
        charge = self.s["charge"]
        last = self.s["last_charge"]

        if last and o.connection == "DISCONNECTED":
            last["unplugged"] = True

        if not charge and charging:
            started = self._when(o.charge_ts, o, prev)
            if last and not last["unplugged"] and started - last["ended_at"] <= CHARGE_RESUME_S:
                self.db.update("charges", last["id"], {"ended_at": None})
                charge = {"id": last["id"], "last_sample": last["last_sample"]}
            else:
                soc_start = o.soc
                if prev and prev.soc is not None and o.soc is not None and prev.soc <= o.soc:
                    soc_start = prev.soc
                lat, lon = self._position(o)
                place = self._place_for(lat, lon)
                charge_id = self.db.insert("charges", {
                    "started_at": started, "soc_start": soc_start, "soc_end": o.soc,
                    "lat": lat, "lon": lon, "odometer_km": o.odometer_km,
                    "place_id": place["id"] if place else None,
                    "place_name": place["name"] if place else None,
                    "price_per_kwh": (
                        place["price_per_kwh"] if place and place["price_per_kwh"] is not None
                        else get_settings(self.db)["default_price_per_kwh"]
                    ),
                })
                charge = {"id": charge_id, "last_sample": None}
                self._event(started, "charge_started", charge_id=charge_id, soc=o.soc)
            self.s["charge"] = charge
            self.s["last_charge"] = None
            if self.s["last_trip"]:
                self.s["last_trip"]["charged"] = True

        if not charge:
            return

        if charging:
            values: dict[str, Any] = {"soc_end": o.soc, "target_soc": o.target_soc}
            if o.charge_type in ("AC", "DC"):
                values["charge_type"] = o.charge_type
            self.db.update("charges", charge["id"], values)
            ts = o.charge_ts or o.soc_ts or o.t
            if charge["last_sample"] is None or ts > charge["last_sample"]:
                self.db.insert("charge_samples", {
                    "charge_id": charge["id"], "ts": ts, "power_w": o.power_w, "soc": o.soc,
                })
                charge["last_sample"] = ts
            self._finalize_charge(charge["id"], closing=False)
            return

        # Not charging any more (or unplugged, or driven away).
        ended = self._when(o.charge_ts, o, prev)
        if o.soc is not None:
            self.db.update("charges", charge["id"], {"soc_end": o.soc})
        self.db.update("charges", charge["id"], {"ended_at": ended})
        self.s["charge"] = None
        row = self._finalize_charge(charge["id"], closing=True)
        if row:
            self.s["last_charge"] = {
                "id": charge["id"], "ended_at": ended,
                "last_sample": charge["last_sample"],
                "unplugged": o.connection == "DISCONNECTED",
            }
            self._event(
                ended, "charge_ended", charge_id=charge["id"],
                energy_kwh=row["energy_kwh"], soc=row["soc_end"],
            )

    def _place_for(self, lat: float | None, lon: float | None) -> dict[str, Any] | None:
        if lat is None or lon is None:
            return None
        best, best_d = None, None
        for p in self.db.q("SELECT * FROM places"):
            d = haversine_m(lat, lon, p["lat"], p["lon"])
            if d <= p["radius_m"] and (best_d is None or d < best_d):
                best, best_d = dict(p), d
        return best

    def _finalize_charge(self, charge_id: int, closing: bool) -> dict[str, Any] | None:
        r = self.db.one("SELECT * FROM charges WHERE id = ?", charge_id)
        if r is None:
            return None
        samples = self.db.q(
            "SELECT ts, power_w FROM charge_samples WHERE charge_id = ? ORDER BY ts", charge_id
        )
        points = [(s["ts"], s["power_w"]) for s in samples]
        if points and points[0][0] > r["started_at"]:
            # Assume the first reading also held from the start until that reading.
            points.insert(0, (r["started_at"], points[0][1]))
        if closing and points and r["ended_at"] and r["ended_at"] > points[-1][0]:
            # ...and that the last one held until charging stopped.
            points.append((r["ended_at"], points[-1][1]))
        metered, covered = integrate_power(points)
        end = r["ended_at"] or (samples[-1]["ts"] if samples else r["started_at"])
        duration = max(end - r["started_at"], 0)
        coverage = min(covered / duration, 1.0) if duration > 0 else 0.0
        gained = (
            max(r["soc_end"] - r["soc_start"], 0)
            if r["soc_end"] is not None and r["soc_start"] is not None else None
        )
        from_soc = gained / 100 * self.capacity() if gained is not None else None

        if closing and (gained or 0) < 1 and metered < 0.5 and duration < 300:
            # A blip, not a session.
            self.db.x("DELETE FROM charge_samples WHERE charge_id = ?", charge_id)
            self.db.x("DELETE FROM charges WHERE id = ?", charge_id)
            return None

        # Integrated power is the better number, but only if we sampled most of the session.
        if metered > 0 and coverage >= 0.8:
            energy, source = metered, "metered"
        else:
            energy, source = from_soc, "soc"
        powers = [s["power_w"] for s in samples if s["power_w"] is not None]
        values = {
            "energy_soc_kwh": round(from_soc, 2) if from_soc is not None else None,
            "energy_metered_kwh": round(metered, 2) if metered > 0 else None,
            "metered_coverage": round(coverage, 2),
            "energy_kwh": round(energy, 2) if energy is not None else None,
            "energy_source": source,
            "max_power_kw": round(max(powers) / 1000, 1) if powers else None,
            "avg_power_kw": (
                round(energy / (duration / 3600), 1) if energy and duration > 60 else None
            ),
        }
        self.db.update("charges", charge_id, values)
        return dict(r) | values


DERIVED_TABLES = ("trips", "trip_points", "charges", "charge_samples", "events")


def rebuild(db: DB, capacity_kwh: Callable[[], float | None]) -> dict[str, int]:
    """Throw away trips and charges and replay the whole observation log.

    Things the user typed in (notes, positions, cost overrides) are carried over to the
    rebuilt row that overlaps the old one most.
    """
    keep_trips = [dict(r) for r in db.q(
        "SELECT * FROM trips WHERE note IS NOT NULL OR start_manual = 1 OR end_manual = 1")]
    keep_charges = [
        dict(r) for r in db.q(
            "SELECT * FROM charges WHERE note IS NOT NULL OR cost_override IS NOT NULL "
            "OR energy_override IS NOT NULL"
        )
    ]
    # Enrichment results are expensive to look up again, so keep them by time.
    places = {
        round(r["started_at"]): dict(r)
        for r in db.q("SELECT started_at, start_place, end_place, temp_c, start_lat, start_lon, end_lat, end_lon FROM trips")
    }
    kept_events = [
        dict(r) for r in db.q(
            "SELECT ts, type, detail FROM events WHERE type NOT IN "
            "('trip_started','trip_ended','charge_started','charge_ended','plugged','unplugged')"
        )
    ]

    with db.tx():
        for table in DERIVED_TABLES:
            db.x(f"DELETE FROM {table}")
        db.delete("detector_state")
        detector = Detector(db, capacity_kwh)
        cols = ", ".join(OBS_FIELDS)
        for row in db.conn.execute(f"SELECT {cols} FROM observations ORDER BY t, id").fetchall():
            detector.feed(Obs(**dict(row)))

        for e in kept_events:
            db.insert("events", e)
        for r in db.q("SELECT id, started_at, start_lat, start_lon, end_lat, end_lon FROM trips"):
            old = places.get(round(r["started_at"]))
            if old:
                # An address is only kept if the position it was looked up for is unchanged.
                same = lambda end: (r[f"{end}_lat"], r[f"{end}_lon"]) == (old[f"{end}_lat"], old[f"{end}_lon"])
                values = {"temp_c": old["temp_c"]} if same("start") and same("end") else {}
                if same("start"):
                    values["start_place"] = old["start_place"]
                if same("end"):
                    values["end_place"] = old["end_place"]
                db.update("trips", r["id"], values)
        for old in keep_trips:
            match = _overlapping(db, "trips", old)
            if match:
                values = {"note": old["note"]} if old["note"] is not None else {}
                new = db.one("SELECT start_lat, end_lat FROM trips WHERE id = ?", match)
                # Positions entered by hand, unless the car has since reported one.
                for end in ("start", "end"):
                    if old[f"{end}_manual"] and new[f"{end}_lat"] is None:
                        values |= {f"{end}_{k}": old[f"{end}_{k}"] for k in ("lat", "lon", "place", "manual")}
                db.update("trips", match, values)
        for old in keep_charges:
            match = _overlapping(db, "charges", old)
            if match:
                db.update("charges", match, {
                    k: old[k] for k in ("note", "cost_override", "energy_override")
                })

    return {
        "trips": db.one("SELECT COUNT(*) c FROM trips")["c"],
        "charges": db.one("SELECT COUNT(*) c FROM charges")["c"],
    }


def _overlapping(db: DB, table: str, old: dict[str, Any]) -> int | None:
    end = old["ended_at"] or old["started_at"]
    row = db.one(
        f"SELECT id FROM {table} WHERE started_at <= ? AND COALESCE(ended_at, started_at) >= ? "
        "ORDER BY MIN(COALESCE(ended_at, started_at), ?) - MAX(started_at, ?) DESC LIMIT 1",
        end, old["started_at"], end, old["started_at"],
    )
    return row["id"] if row else None
