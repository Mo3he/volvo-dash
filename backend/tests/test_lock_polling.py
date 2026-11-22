import asyncio
from datetime import datetime, timezone


def test_lock_is_polled_each_minute_after_a_trip_ends(rig, monkeypatch):
    r = rig("live")
    collector = r.collector
    start = r.clock.now()
    collector.detector.s["trip"] = {"id": 1}
    collector.obs.engine = "RUNNING"
    requested = []

    async def fetch(resources, now):
        requested.append(set(resources))
        timestamp = datetime.fromtimestamp(now, timezone.utc).isoformat()
        return {"engine-status": {"engineStatus": {"value": "STOPPED", "timestamp": timestamp}}}

    async def noop(*_):
        return None

    def end_trip(_):
        if collector.detector.s["trip"]:
            collector.detector.s["trip"] = None
            return [{"type": "trip_ended"}]
        return []

    monkeypatch.setattr(collector, "_fetch", fetch)
    monkeypatch.setattr(collector, "_refresh_static", noop)
    monkeypatch.setattr(collector, "_check_sign_in_expiry", noop)
    monkeypatch.setattr(collector.detector, "feed", end_trip)

    async def go():
        await collector.tick()
        assert collector.doors_fast_until == start + 600
        assert collector.due["doors"] == start + 60

        requested.clear()
        r.clock.t = start + 60
        await collector.tick()
        assert "doors" in requested[-1]
        assert collector.due["doors"] == start + 120

        r.clock.t = start + 600
        await collector.tick()
        assert collector.doors_fast_until == 0
        assert r.db.get("doors_fast_until") is None
        assert collector.due["doors"] == start + 600 + 1800

    asyncio.run(go())
