"""End to end: simulated car -> fake Volvo API -> client -> collector -> detector."""

import pytest

from volvodash.detect import rebuild
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
