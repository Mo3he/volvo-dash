"""The raw log: every answer Volvo gives, kept whenever it differs from the
previous answer for the same endpoint.

This is the source of truth. Observations, trips and charges are all derived
from it, so anything the app learns to read later can be filled in for the
past by rebuilding.
"""

import hashlib
import json
import zlib
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .db import DB
from .detect import OBS_FIELDS, Obs, rebuild
from .fold import fold

ENERGY_STATE = "/energy/v2/vehicles/{vin}/state"
LOCATION = "/location/v1/vehicles/{vin}/location"
CONNECTED_PREFIX = "/connected-vehicle/v2/vehicles/{vin}/"


class RawLog:
    def __init__(self, db: DB) -> None:
        self.db = db
        self._last: dict[str, str] | None = None

    def record(self, fetched_at: float, method: str, endpoint: str, status: int, body: str) -> None:
        if self._last is None:
            self._last = {
                f"{r['method']} {r['endpoint']}": r["hash"] for r in self.db.q(
                    "SELECT method, endpoint, hash FROM raw_responses WHERE id IN "
                    "(SELECT MAX(id) FROM raw_responses GROUP BY method, endpoint)")
            }
        # Volvo does not always list fields in the same order; compare content.
        try:
            canonical = json.dumps(json.loads(body), sort_keys=True)
        except ValueError:
            canonical = body
        digest = hashlib.sha1(f"{method} {status} {canonical}".encode()).hexdigest()
        key = f"{method} {endpoint}"
        if self._last.get(key) == digest:
            return
        self._last[key] = digest
        self.db.insert("raw_responses", {
            "fetched_at": fetched_at, "method": method, "endpoint": endpoint,
            "status": status, "hash": digest, "body": zlib.compress(body.encode()),
        })


def body_of(row: Any) -> str:
    return zlib.decompress(row["body"]).decode()


def resource_answer(endpoint: str, status: int, body: str) -> tuple[str, dict[str, Any]] | None:
    """What the collector would have made of this response: (resource, fields)."""
    if status != 200:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if endpoint == ENERGY_STATE:
        return "energy", {k: v for k, v in data.items()
                          if isinstance(v, dict) and v.get("status") == "OK"}
    if endpoint == LOCATION:
        return "location", data.get("data") or {}
    if endpoint.startswith(CONNECTED_PREFIX):
        resource = endpoint.removeprefix(CONNECTED_PREFIX)
        fields = data.get("data") or {}
        # The command list is a daily snapshot, not part of a poll.
        if "/" not in resource and resource != "commands" and isinstance(fields, dict):
            return resource, fields
    return None


def polls(db: DB) -> Iterator[tuple[float, dict[str, dict[str, Any]]]]:
    """The raw log grouped back into polls, in order."""
    current_t: float | None = None
    results: dict[str, dict[str, Any]] = {}
    for row in db.conn.execute(
        "SELECT fetched_at, method, endpoint, status, body FROM raw_responses "
        "WHERE method = 'GET' ORDER BY fetched_at, id"
    ):
        if current_t is not None and row["fetched_at"] != current_t:
            yield current_t, results
            results = {}
        current_t = row["fetched_at"]
        answer = resource_answer(row["endpoint"], row["status"], body_of(row))
        if answer:
            results[answer[0]] = answer[1]
    if current_t is not None:
        yield current_t, results


def regenerate_observations(db: DB) -> int:
    """Rewrite the observation log from the raw log, for the period the raw log covers."""
    first = db.one("SELECT MIN(fetched_at) t FROM raw_responses")["t"]
    if first is None:
        return 0
    cols = ", ".join(OBS_FIELDS)
    before = db.one(f"SELECT {cols} FROM observations WHERE t < ? ORDER BY t DESC, id DESC LIMIT 1", first)
    obs = Obs(**dict(before)) if before else Obs()
    written = 0
    db.x("DELETE FROM observations WHERE t >= ?", first)
    for t, results in polls(db):
        new = fold(obs, results, t)
        if not new.same_data(obs):
            db.insert("observations", asdict(new))
            written += 1
        obs = new
    return written


def rebuild_all(db: DB, capacity_kwh: Callable[[], float | None], backups: Path | None) -> dict[str, int]:
    """Re-derive observations, trips and charges. Backs up first."""
    if backups:
        backup(db, backups, "before-rebuild")
    with db.tx():
        observations = regenerate_observations(db)
    counts = rebuild(db, capacity_kwh)
    return counts | {"observations": observations}


def backup(db: DB, directory: Path, label: str, keep: int = 14) -> Path:
    """A consistent copy of the whole database. Keeps the newest `keep` per label."""
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    path = directory / f"volvodash-{label}-{stamp}.db"
    db.conn.execute("VACUUM INTO ?", (str(path),))
    path.chmod(0o600)
    for old in sorted(directory.glob(f"volvodash-{label}-*.db"))[:-keep]:
        old.unlink()
    return path
