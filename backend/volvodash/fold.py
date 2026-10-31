"""Turns Volvo API answers into the readings detection works on.

Shared by the live collector and by rebuilding from the raw log, so both
always produce exactly the same observations.
"""

from dataclasses import replace
from typing import Any

from .detect import Obs
from .volvo import parse_ts

MILE = 1.609344


def km(field: dict[str, Any]) -> float | None:
    value = field.get("value")
    if not isinstance(value, (int, float)):
        return None
    unit = (field.get("unit") or "km").lower()
    return value * MILE if unit.startswith("mi") or unit == "mph" else float(value)


def num(field: dict[str, Any]) -> float | None:
    value = field.get("value")
    return float(value) if isinstance(value, (int, float)) else None


def field_value(key: str, f: dict[str, Any]) -> Any:
    value = f.get("value")
    if key == "availabilityStatus" and value == "UNAVAILABLE":
        return f.get("unavailableReason") or value
    return value


def fold(prev: Obs, results: dict[str, dict[str, Any]], now: float) -> Obs:
    """A new observation: `prev` carried forward, updated with this poll's answers."""
    o = replace(prev, t=now)
    for res, fields in results.items():
        if res == "location":
            coords = (fields.get("geometry") or {}).get("coordinates") or []
            if len(coords) >= 2:
                o.lon, o.lat = float(coords[0]), float(coords[1])
                o.location_ts = parse_ts((fields.get("properties") or {}).get("timestamp"))
            continue
        for key, f in fields.items():
            if not isinstance(f, dict):
                continue
            ts = parse_ts(f.get("timestamp") or f.get("updatedAt"))
            value = field_value(key, f)
            if key == "batteryChargeLevel":
                # Both the energy and the fuel endpoint report it, updated at
                # different moments; an older reading must never replace a newer one.
                if ts is None or o.soc_ts is None or ts >= o.soc_ts:
                    o.soc, o.soc_ts = num(f), ts
            elif key == "electricRange":
                o.range_km = km(f)
            elif key == "chargingStatus":
                o.charging_status, o.charge_ts = value, ts
            elif key == "chargerConnectionStatus":
                o.connection = value
            elif key == "chargingType":
                o.charge_type = value
            elif key == "targetBatteryChargeLevel":
                o.target_soc = num(f)
            elif key == "estimatedChargingTimeToTargetBatteryChargeLevel":
                o.mins_to_target = num(f)
            elif key == "engineStatus":
                o.engine, o.engine_ts = value, ts
            elif key == "availabilityStatus":
                o.availability, o.availability_ts = value, ts
            elif key == "odometer":
                o.odometer_km, o.odometer_ts = km(f), ts
            elif key == "tripMeterAutomatic":
                o.trip_auto_km, o.stats_ts = km(f), ts
            elif key == "averageSpeedAutomatic":
                o.avg_speed_auto = km(f)
            elif key == "averageEnergyConsumption":
                o.avg_consumption = num(f)
            elif key == "tripMeterManual":
                o.trip_manual_km = km(f)
        if res == "energy":
            # Not every car reports power; absent means unknown, not unchanged.
            o.power_w = num(fields["chargingPower"]) if "chargingPower" in fields else None
    return o
