"""Time sources. Everything asks a Clock for the time so the simulator can run fast."""

import asyncio
import time


class Clock:
    def now(self) -> float:
        return time.time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class SimClock(Clock):
    """Wall clock that starts at `start` and runs `speed` times faster."""

    def __init__(self, start: float, speed: float = 1.0) -> None:
        self._start = start
        self._real_start = time.time()
        self.speed = speed

    def now(self) -> float:
        return self._start + (time.time() - self._real_start) * self.speed

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds / self.speed)


class ManualClock(Clock):
    """Clock that only moves when told to. Used by tests and the demo seeder."""

    def __init__(self, start: float) -> None:
        self.t = start

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds
