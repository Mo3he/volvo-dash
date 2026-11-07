"""End to end: simulated car -> fake Volvo API -> client -> collector -> detector."""

import pytest

from volvodash.detect import Detector, rebuild
from volvodash.sim import CAPACITY_KWH, SPOTS


def merged_truth(drives, gap):
    """Ground-truth drives, with stops shorter than `gap` joined the way the detector joins them."""
    out = []
    for d in drives:
        if out and d["start"] - out[-1]["end"] <= gap:
            out[-1] = {**out[-1], "end": d["end"], "km": out[-1]["km"] + d["km"],
                       "kwh": out[-1]["kwh"] + d["kwh"]}
        else:
            out.append(dict(d))
    return out


@pytest.mark.parametrize("mode", ["ex40", "live", "sparse"])
def test_trips_match_what_the_car_did(rig, mode):
    r = rig(mode)
    r.run(14)
    truth = [d for d in merged_truth(r.car.truth["drives"], 0 if mode == "sparse" else 300)]
    trips = r.trips()
    assert len(truth) > 20
    assert len(trips) == len(truth)
    for trip, real in zip(trips, truth):
        assert trip["distance_km"] == pytest.approx(real["km"], abs=0.25)
        assert abs(trip["ended_at"] - real["end"]) <= 90
        if mode != "sparse":
            assert abs(trip["started_at"] - real["start"]) <= 90
            assert trip["start_estimated"] == 0
        else:
            # Nobody saw the start, so it is a guess from distance and average
            # speed. It must be flagged as one and land in the right region.
            assert trip["start_estimated"] == 1
            assert abs(trip["started_at"] - real["start"]) <= 0.6 * (real["end"] - real["start"]) + 120
    if mode == "ex40":
        # No position while moving: every map point is a place the car parked.
        parked = {(round(lat, 4), round(lon, 4)) for _, lat, lon in SPOTS.values()}
        points = {(round(p["lat"], 4), round(p["lon"], 4))
                  for p in r.db.q("SELECT lat, lon FROM trip_points")}
        assert points <= parked
    # The car's own energy counter is exact per trip, short trips included,
    # until the manual trip meter grows so large that rounding wins.
    from_car = [(t, real) for t, real in zip(trips, truth) if t["energy_source"] == "car"]
    assert len(from_car) >= 10
    for trip, real in from_car:
        assert trip["energy_kwh"] == pytest.approx(real["kwh"], abs=0.1 + 0.001 * trip["tm_end"])
    total = sum(t["distance_km"] for t in trips)
    assert total == pytest.approx(sum(d["km"] for d in truth), rel=0.005)
    # Whole-percent SoC makes single trips noisy, but the totals must agree.
    energy = sum(t["energy_kwh"] for t in trips)
    assert energy == pytest.approx(sum(d["kwh"] for d in truth), rel=0.06)


@pytest.mark.parametrize("mode", ["ex40", "live", "sparse"])
def test_charges_match_what_the_car_did(rig, mode):
    r = rig(mode)
    r.run(14)
    truth = r.car.truth["charges"]
    charges = r.charges()
    assert len(truth) >= 4
    assert {c["type"] for c in truth} == {"AC", "DC"}
    assert len(charges) == len(truth)
    for charge, real in zip(charges, truth):
        assert charge["charge_type"] == real["type"]
        slack = 960 if mode == "sparse" else 120
        assert abs(charge["started_at"] - real["start"]) <= slack
        assert abs(charge["ended_at"] - real["end"]) <= slack
        assert charge["soc_end"] == pytest.approx(real["soc_end"], abs=1)
        assert charge["soc_start"] == pytest.approx(real["soc_start"], abs=1.5)
        added = (real["soc_end"] - real["soc_start"]) / 100 * CAPACITY_KWH
        assert charge["energy_soc_kwh"] == pytest.approx(added, abs=1.6)
        if mode != "sparse" or real["type"] == "AC":
            # Minute-by-minute power, or steady AC power reported every 15 min:
            # adding it up gives what came out of the charger.
            assert charge["energy_source"] == "metered"
            assert charge["energy_kwh"] == pytest.approx(real["wall_kwh"], rel=0.03)
        else:
            # DC power falls steeply; 15-minute samples are too coarse to add up.
            assert charge["energy_source"] == "soc"


def test_trips_get_places_and_temperature(rig):
    r = rig("live")
    r.run(3)
    import asyncio
    asyncio.run(r.collector.enricher.run(r.clock.now(), limit=50))
    trips = r.trips()
    assert trips and all(t["start_place"] and t["end_place"] for t in trips)
    assert all(t["temp_c"] is not None for t in trips)
    assert trips[0]["start_place"].startswith("Kastanjgatan")


def test_rebuild_reproduces_everything_and_keeps_edits(rig):
    r = rig("live")
    r.run(10)
    trips, charges = r.trips(), r.charges()
    r.db.update("trips", trips[2]["id"], {"note": "school run"})
    r.db.update("charges", charges[0]["id"], {"cost_override": 4.2})

    counts = rebuild(r.db, r.collector.capacity)

    strip = lambda rows: [{k: v for k, v in row.items() if k not in ("id", "note", "cost_override")}
                          for row in rows]
    assert counts["trips"] >= len(trips)
    assert strip(r.trips()) == strip(trips)
    assert strip(r.charges()) == strip(charges)
    assert r.trips()[2]["note"] == "school run"
    assert r.charges()[0]["cost_override"] == 4.2


def test_stays_within_the_daily_call_budget(rig):
    r = rig("live")
    r.run(7)
    worst = r.db.one("SELECT MAX(calls) c FROM api_usage")["c"]
    assert worst < 4000


def test_expired_sign_in_stops_polling_and_says_so(rig):
    r = rig("live")
    r.run(0.5)
    r.car.revoked = True
    r.db.set("volvo_tokens", {**r.client.tokens, "expires_at": 0})
    r.run(0.1)
    assert r.db.get("volvo_auth_error")
    assert r.collector.status["mode"] == "needs_reauth"
    assert r.db.one("SELECT COUNT(*) c FROM events WHERE type = 'needs_reauth'")["c"] == 1
    calls = r.client.calls_today()
    r.run(0.2)
    assert r.client.calls_today() == calls


def test_warns_once_before_a_hard_sign_in_limit(rig):
    r = rig("live")
    r.run(0.2)
    # Normally every refresh pushes the deadline 90 days out.
    assert r.client.sign_in_until() > r.clock.t + 80 * 86400
    warned = lambda: r.db.one("SELECT COUNT(*) c FROM events WHERE type = 'sign_in_expiring'")["c"]

    r.car.sign_in_deadline = r.clock.t + 20 * 86400
    r.run(5)
    assert warned() == 0 and r.client.sign_in_until() == pytest.approx(r.car.sign_in_deadline, abs=5)
    r.run(2)
    assert warned() == 1
    r.run(2)
    assert warned() == 1  # once, not every poll

    # Signing in again lifts the limit, and the warning can fire again next time.
    r.car.sign_in_deadline = None
    r.run(0.1)
    assert r.client.sign_in_until() > r.clock.t + 80 * 86400
    assert r.db.get("volvo_expiry_warned") is None


def test_single_use_refresh_tokens_survive_concurrent_requests(rig):
    # Regression: each poll sends several requests at once. When the access token
    # had expired, every one of them refreshed with the same single-use refresh
    # token, and Volvo's rejection of the duplicates looked like a revoked sign-in.
    r = rig("live")
    r.run(2)
    assert r.db.get("volvo_auth_error") is None
    assert r.client.tokens["refresh_token"].startswith("sim-refresh-")
    assert int(r.client.tokens["refresh_token"].rsplit("-", 1)[1]) > 60  # ~every 30 min


def test_raw_log_alone_rebuilds_everything(rig, tmp_path):
    from volvodash.raw import rebuild_all

    r = rig("live")
    r.run(10)
    tables = ("observations", "trips", "charges", "charge_samples", "trip_points")
    before = {t: [tuple(row)[1:] for row in r.db.q(f"SELECT * FROM {t} ORDER BY 2, 3")] for t in tables}
    endpoints = {row["endpoint"] for row in r.db.q("SELECT DISTINCT endpoint FROM raw_responses")}

    # Lose every derived row, then rebuild from nothing but the raw log.
    for t in tables:
        r.db.x(f"DELETE FROM {t}")
    counts = rebuild_all(r.db, r.collector.capacity, tmp_path)

    after = {t: [tuple(row)[1:] for row in r.db.q(f"SELECT * FROM {t} ORDER BY 2, 3")] for t in tables}
    assert counts["observations"] == len(before["observations"]) > 100
    for t in tables:
        assert after[t] == before[t], t
    # Static snapshots and the slow endpoints are in the raw log too.
    assert {"/connected-vehicle/v2/vehicles/{vin}", "/energy/v2/vehicles/{vin}/capabilities",
            "/connected-vehicle/v2/vehicles/{vin}/commands",
            "/connected-vehicle/v2/vehicles/{vin}/diagnostics",
            "/connected-vehicle/v2/vehicles/{vin}/brakes"} <= endpoints
    assert list(tmp_path.glob("volvodash-before-rebuild-*.db"))


def test_raw_log_skips_repeats(rig):
    r = rig("live")
    r.run(3)
    stored = r.db.one("SELECT COUNT(*) c FROM raw_responses")["c"]
    calls = sum(row["calls"] for row in r.db.q("SELECT calls FROM api_usage"))
    assert stored < calls / 2
    size = r.db.one("SELECT SUM(LENGTH(body)) s FROM raw_responses")["s"]
    print(f"\nraw log: {stored} of {calls} answers kept, {size / 3 / 1024:.0f} KiB per day")


def test_raw_log_does_not_repeat_after_restart_or_reordering(rig):
    from volvodash.raw import RawLog

    r = rig("ex40")
    r.run(0.2)
    before = r.db.one("SELECT COUNT(*) c FROM raw_responses")["c"]
    row = r.db.one("SELECT endpoint, body FROM raw_responses WHERE endpoint LIKE '%/odometer' "
                   "ORDER BY id DESC LIMIT 1")
    import json, zlib
    body = json.loads(zlib.decompress(row["body"]))
    reordered = json.dumps(dict(reversed(list(body.items()))), indent=2)
    fresh = RawLog(r.db)  # as after a restart
    fresh.record(r.clock.now(), "GET", row["endpoint"], 200, reordered)
    assert r.db.one("SELECT COUNT(*) c FROM raw_responses")["c"] == before


def test_car_energy_counter_and_when_not_to_trust_it():
    from volvodash.detect import car_energy

    def trip(c0, m0, c1, m1):
        return {"cons_start": c0, "tm_start": m0, "cons_end": c1, "tm_end": m1}

    # Real second trip, 31 Oct 2026: 1.64 kWh where battery percent said 1.63.
    assert car_energy(trip(26.1, 36.0, 24.0, 46.0), 9.9, 81.6) == pytest.approx(1.644)
    # Meter reset during the trip, or it disagrees with the distance driven.
    assert car_energy(trip(26.1, 36.0, 20.0, 4.0), 4.0, 81.6) is None
    assert car_energy(trip(26.1, 36.0, 26.1, 36.0), 9.9, 81.6) is None
    assert car_energy(trip(None, None, 24.0, 46.0), 9.9, 81.6) is None
    # At thousands of km, 0.1 kWh/100 km rounding is worse than one battery percent.
    assert car_energy(trip(18.0, 900.0, 18.0, 910.0), 10.0, 81.6) is None
    assert car_energy(trip(18.0, 600.0, 18.0, 610.0), 10.0, 81.6) == pytest.approx(1.8)


def test_driving_after_each_charge(rig):
    from volvodash.periods import after_charge

    r = rig("ex40")
    r.run(14)
    charges = sorted(r.charges(), key=lambda c: c["started_at"])
    trips = r.trips()
    afters = [after_charge(r.db, c, r.collector.obs.soc) for c in charges]
    # Every trip after the first charge is counted under exactly one charge.
    after_first = [t for t in trips if t["started_at"] >= charges[0]["ended_at"]]
    assert sum(a["trips"] for a in afters) == len(after_first)
    assert sum(a["distance_km"] for a in afters) == pytest.approx(
        sum(t["distance_km"] for t in after_first), abs=0.1 * len(afters))
    assert [a["ongoing"] for a in afters] == [False] * (len(afters) - 1) + [True]
    for c, a, nxt in zip(charges, afters, charges[1:] + [None]):
        assert a["soc_from"] == c["soc_end"]
        assert a["soc_to"] == (nxt["soc_start"] if nxt else r.collector.obs.soc)
        if a["full_range_km"] is not None:
            # The simulator's car does 81.6 kWh / ~17-25 kWh/100 km.
            assert 280 < a["full_range_km"] < 520
    assert any(a["full_range_km"] for a in afters)


def test_parking_without_gps_leaves_the_end_unknown(rig):
    # Underground at work: the car keeps serving its last fix, from home.
    r = rig("ex40")
    r.car.no_gps = {"work"}
    r.run(14)
    drives = r.car.truth["drives"]
    trips = r.trips()
    checked = {"to_work": 0, "from_work": 0, "elsewhere": 0}
    for t in trips:
        i = min(range(len(drives)), key=lambda i: abs(drives[i]["end"] - t["ended_at"]))
        to = drives[i]["to"]
        came_from = drives[j]["to"] if (j := max(
            (k for k in range(i + 1) if drives[k]["start"] <= t["started_at"] + 90), default=0) - 1) >= 0 else "home"
        if to == "work":
            assert t["end_lat"] is None and t["end_place"] is None
            checked["to_work"] += 1
        else:
            assert (round(t["end_lat"], 4), round(t["end_lon"], 4)) == tuple(round(x, 4) for x in SPOTS[to][1:])
            checked["elsewhere"] += 1
        if came_from == "work":
            assert t["start_lat"] is None
            checked["from_work"] += 1
    assert all(checked.values()), checked


def test_position_entered_by_hand_for_a_car_park_without_gps(rig):
    from fastapi import HTTPException

    from volvodash import app as webapp
    from volvodash.detect import Detector, rebuild

    r = rig("ex40")
    r.car.no_gps = {"work"}
    webapp.ctx.db = r.db
    garage = {"lat": 57.7061, "lon": 11.9372, "name": "Work garage"}
    row = lambda i: r.db.one("SELECT * FROM trips WHERE id = ?", i)

    # Live: enter it while the car is still parked there, before it drives off.
    for _ in range(400):
        r.run(0.02)
        last = r.db.one("SELECT * FROM trips ORDER BY started_at DESC LIMIT 1")
        if last and last["ended_at"] and last["end_lat"] is None and not r.collector.detector.s["trip"]:
            break
    else:
        raise AssertionError("never parked at work")
    webapp._set_trip_position(last, "end", garage)
    assert (row(last["id"])["end_lat"], row(last["id"])["end_place"], row(last["id"])["end_manual"]) == (
        57.7061, "Work garage", 1)
    r.run(1)
    nxt = r.db.one("SELECT * FROM trips WHERE started_at > ? ORDER BY started_at LIMIT 1", last["started_at"])
    assert (nxt["start_lat"], nxt["start_place"], nxt["start_manual"]) == (57.7061, "Work garage", 1)
    assert row(last["id"])["end_lat"] == 57.7061  # late reports didn't wipe it

    # After the fact: set an end, and the next trip's start follows.
    r.run(6)
    later = [t for t in r.db.q("SELECT * FROM trips WHERE end_lat IS NULL AND ended_at IS NOT NULL")]
    assert later
    webapp._set_trip_position(later[0], "end", {"lat": 57.7061, "lon": 11.9372})
    after = r.db.one("SELECT * FROM trips WHERE started_at > ? ORDER BY started_at LIMIT 1", later[0]["started_at"])
    assert after["start_manual"] == 1 and after["start_lat"] == 57.7061

    # Both survive a rebuild.
    manual = {(t["started_at"], t["start_manual"], t["end_manual"]) for t in r.db.q(
        "SELECT * FROM trips WHERE start_manual = 1 OR end_manual = 1")}
    rebuild(r.db, r.collector.capacity)
    assert {(t["started_at"], t["start_manual"], t["end_manual"]) for t in r.db.q(
        "SELECT * FROM trips WHERE start_manual = 1 OR end_manual = 1")} == manual

    # Clearing it clears the neighbour too; a position the car reported can't be overwritten.
    t = r.db.one("SELECT * FROM trips WHERE end_manual = 1 ORDER BY started_at DESC LIMIT 1")
    webapp._set_trip_position(t, "end", None)
    assert row(t["id"])["end_lat"] is None
    n = r.db.one("SELECT * FROM trips WHERE started_at > ? ORDER BY started_at LIMIT 1", t["started_at"])
    assert n["start_lat"] is None and n["start_manual"] == 0
    gps = r.db.one("SELECT * FROM trips WHERE end_lat IS NOT NULL AND end_manual = 0 LIMIT 1")
    with pytest.raises(HTTPException):
        webapp._set_trip_position(gps, "end", garage)


def test_trip_waits_for_its_distance_where_there_is_no_signal(rig):
    # Underground with no signal: the car says "stopped" but its odometer only
    # catches up when it sets off again. The trip must not be thrown away.
    r = rig("ex40")
    r.car.no_gps = {"work"}
    r.car.no_signal = {"work"}
    r.run(14)
    trips = r.trips()
    to_work = [d for d in r.car.truth["drives"] if d["to"] == "work"]
    assert to_work
    for d in to_work:
        t = min(trips, key=lambda t: abs(t["ended_at"] - d["end"]))
        assert abs(t["ended_at"] - d["end"]) < 300
        assert t["distance_km"] == pytest.approx(d["km"], abs=1.5)


def test_end_and_distance_entered_by_hand_survive_and_undo(rig):
    r = rig("ex40")
    r.run(2)
    det = r.collector.detector
    t = r.trips()[1]
    car_end, car_km = t["ended_at"], t["distance_km"]
    r.db.update("trips", t["id"], {"ended_override": car_end - 600, "ended_car": car_end,
                                   "distance_override": 12.3})
    det.refinalize(t["id"])
    got = r.db.one("SELECT * FROM trips WHERE id = ?", t["id"])
    assert got["ended_at"] == car_end - 600 and got["distance_km"] == 12.3
    assert got["avg_speed_kmh"] == pytest.approx(12.3 / ((car_end - 600 - got["started_at"]) / 3600), abs=0.1)

    rebuild(r.db, r.collector.capacity)
    got = r.db.one("SELECT * FROM trips WHERE started_at = ?", got["started_at"])
    assert got["ended_at"] == car_end - 600 and got["distance_km"] == 12.3

    r.db.update("trips", got["id"], {"ended_override": None, "distance_override": None})
    det = Detector(r.db, r.collector.capacity)  # rebuild replaced the detector's state
    det.refinalize(got["id"])
    got = r.db.one("SELECT * FROM trips WHERE id = ?", got["id"])
    assert got["ended_at"] == car_end and got["distance_km"] == car_km


def test_drive_kept_when_the_car_resends_its_old_odometer_at_switch_off():
    # Seen 6 Nov: a 2.5 km drive home, and at switch-off the car sent the
    # unchanged odometer and trip meter with a fresh time. It is not a 0 km trip.
    from volvodash.db import DB
    from volvodash.detect import Obs

    db = DB(":memory:")
    det = Detector(db, lambda: CAPACITY_KWH)
    t0 = 1_800_000_000.0
    a, b = (57.70, 11.90), (57.71, 11.94)  # about 2.6 km apart

    def obs(m, engine, odo, odo_m, at, loc_m, **kw):
        return Obs(t=t0 + m * 60, soc=kw.pop("soc", 67.0), soc_ts=t0 + m * 60,
                   connection=kw.pop("connection", "DISCONNECTED"),
                   charging_status=kw.pop("charging", "IDLE"), charge_ts=t0,
                   engine=engine, engine_ts=t0 + (m - 0.5) * 60,
                   availability="CAR_IN_USE" if engine == "RUNNING" else "AVAILABLE",
                   availability_ts=t0 + (m - 0.5) * 60,
                   odometer_km=odo, odometer_ts=t0 + odo_m * 60,
                   trip_auto_km=17.7, stats_ts=t0 + odo_m * 60, trip_manual_km=odo + 0.4,
                   lat=at[0], lon=at[1], location_ts=t0 + loc_m * 60, **kw)

    det.feed(obs(-20, "STOPPED", 122.0, -30, a, -30))
    det.feed(obs(-10, "STOPPED", 122.0, -30, a, -30))
    det.feed(obs(1, "RUNNING", 122.0, 0.5, a, -30))
    det.feed(obs(5, "RUNNING", 122.0, 0.5, a, -30, soc=66.0))
    det.feed(obs(9, "STOPPED", 122.0, 8.5, b, 8.5, soc=66.0))   # stale figure, fresh time
    det.feed(obs(11, "STOPPED", 122.0, 8.5, b, 8.5, soc=66.0, connection="CONNECTED",
                 charging="CHARGING"))
    det.feed(obs(60, "STOPPED", 122.0, 8.5, b, 8.5, soc=70.0, connection="CONNECTED",
                 charging="CHARGING"))

    trips = db.q("SELECT * FROM trips")
    assert len(trips) == 1
    assert trips[0]["ended_at"] is not None and trips[0]["distance_km"] is None
    assert (trips[0]["end_lat"], trips[0]["end_lon"]) == b

    # Should the car resend the same figure later, the trip still waits...
    det.feed(obs(120, "STOPPED", 122.0, 119, b, 8.5, soc=72.0, connection="CONNECTED",
                 charging="CHARGING"))
    assert db.one("SELECT distance_km FROM trips")["distance_km"] is None
    # ...and takes the distance once the odometer catches up.
    det.feed(obs(180, "STOPPED", 125.0, 179, b, 8.5, soc=74.0, connection="CONNECTED",
                 charging="CHARGING"))
    assert db.one("SELECT distance_km FROM trips")["distance_km"] == pytest.approx(3.0, abs=0.1)


def test_preconditioning_on_a_full_battery_is_not_a_charge():
    # Seen 7 Nov: charged to 100 % overnight, then the car warmed itself up from
    # the plug in the morning. That showed as a second, 0 % "charge" and the
    # day's driving was counted against it.
    from volvodash.db import DB
    from volvodash.detect import Obs
    from volvodash.periods import after_charge

    db = DB(":memory:")
    det = Detector(db, lambda: CAPACITY_KWH)
    t0 = 1_800_000_000.0

    def obs(m, soc, charging, connection="CONNECTED", engine="STOPPED", odo=100.0, power=None):
        t = t0 + m * 60
        return Obs(t=t, soc=soc, soc_ts=t, charging_status=charging, charge_ts=t,
                   connection=connection, charge_type="AC", power_w=power, target_soc=100,
                   engine=engine, engine_ts=t, availability="AVAILABLE", availability_ts=t,
                   odometer_km=odo, odometer_ts=t)

    for m, soc in ((0, 60.0), (60, 80.0), (120, 100.0)):
        det.feed(obs(m, soc, "CHARGING", power=11000))
    det.feed(obs(121, 100.0, "IDLE"))
    for m in (500, 503, 507):  # preconditioning
        det.feed(obs(m, 100.0, "CHARGING", power=3000))
    det.feed(obs(508, 100.0, "IDLE"))
    det.feed(obs(540, 100.0, "IDLE", connection="DISCONNECTED"))
    det.feed(obs(541, 100.0, "IDLE", connection="DISCONNECTED", engine="RUNNING"))
    det.feed(obs(560, 97.0, "IDLE", connection="DISCONNECTED", engine="STOPPED", odo=112.0))

    charges = db.q("SELECT * FROM charges")
    assert len(charges) == 1 and charges[0]["soc_end"] == 100.0
    assert not db.q("SELECT * FROM events WHERE type = 'charge_started' AND ts > ?", t0 + 3600)
    after = after_charge(db, dict(charges[0]), 97.0)
    assert after["trips"] == 1 and after["distance_km"] == pytest.approx(12, abs=0.5)
