import asyncio

import pytest

from volvodash import sim
from volvodash.clock import ManualClock
from volvodash.collector import Collector
from volvodash.db import DB
from volvodash.enrich import Enricher
from volvodash.volvo import VolvoClient

START = 1_790_000_000.0 // 86400 * 86400 + 3 * 3600  # 03:00 UTC on a fixed day


class Rig:
    """The whole backend wired to a simulated car and a clock we control."""

    def __init__(self, mode: str = "live", seed: int = 7) -> None:
        self.clock = ManualClock(START)
        self.db = DB(":memory:")
        self.car = sim.SimCar(START, seed=seed, mode=mode)
        sim.connect(self.db, self.clock)
        self.client = VolvoClient(self.db, self.clock, sim.transport(self.car, self.clock))
        self.collector = Collector(self.db, self.client, self.clock,
                                   Enricher(self.db, sim.SimLookups()))
        self.collector.live = False

    def run(self, days: float) -> None:
        async def go() -> None:
            end = self.clock.t + days * 86400
            while self.clock.t < end:
                self.clock.t += await self.collector.tick()
        # One loop for the whole test, like the server: locks belong to a loop.
        if not hasattr(self, "loop"):
            self.loop = asyncio.new_event_loop()
        self.loop.run_until_complete(go())

    def trips(self):
        return [dict(r) for r in self.db.q(
            "SELECT * FROM trips WHERE ended_at IS NOT NULL ORDER BY started_at")]

    def charges(self):
        return [dict(r) for r in self.db.q(
            "SELECT * FROM charges WHERE ended_at IS NOT NULL ORDER BY started_at")]


@pytest.fixture
def rig():
    return Rig
