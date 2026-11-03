"""What happened between one charge and the next: distance, energy, battery used.

Worked out from trips and charges when asked, so it is always in step with
edits and rebuilds.
"""

from typing import Any

from .db import DB

# Below this much battery used, a full-charge range estimate is mostly rounding.
MIN_PCT_FOR_RANGE = 5


def after_charge(db: DB, charge: dict[str, Any], current_soc: float | None) -> dict[str, Any] | None:
    """Driving from the end of `charge` until the next charge starts (or now)."""
    if charge.get("ended_at") is None:
        return None
    nxt = db.one(
        "SELECT started_at, soc_start FROM charges WHERE started_at > ? ORDER BY started_at LIMIT 1",
        charge["started_at"],
    )
    until = nxt["started_at"] if nxt else None
    trips = db.q(
        "SELECT distance_km, energy_kwh FROM trips WHERE ended_at IS NOT NULL "
        "AND started_at >= ? AND (? IS NULL OR started_at < ?)",
        charge["ended_at"], until, until,
    )
    km = sum(t["distance_km"] or 0 for t in trips)
    measured = [t for t in trips if t["energy_kwh"] is not None]
    energy = sum(t["energy_kwh"] for t in measured)
    measured_km = sum(t["distance_km"] or 0 for t in measured)

    soc_to = nxt["soc_start"] if nxt else current_soc
    used = (
        max(charge["soc_end"] - soc_to, 0)
        if charge.get("soc_end") is not None and soc_to is not None else None
    )
    return {
        "ongoing": nxt is None,
        "until": until,
        "trips": len(trips),
        "distance_km": round(km, 1),
        "energy_kwh": round(energy, 2) if measured else None,
        "efficiency": round(energy / measured_km * 100, 1) if measured_km >= 1 else None,
        "soc_from": charge.get("soc_end"),
        "soc_to": soc_to,
        "soc_used": used,
        # Battery used includes standby and climate while parked, so this is real-world range.
        "full_range_km": round(km / used * 100) if used and used >= MIN_PCT_FOR_RANGE and km else None,
    }
