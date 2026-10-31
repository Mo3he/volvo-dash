"""Optional lookups that make the log nicer to read: street addresses for where
trips start and end (OpenStreetMap Nominatim) and the outside temperature
(Open-Meteo). Both are free, need no key, and receive the car's coordinates,
which is why they sit behind one setting.
"""

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from .db import DB, get_settings
from .detect import haversine_m

log = logging.getLogger(__name__)
USER_AGENT = "volvo-dash/0.1 (self-hosted)"


class Lookups:
    def __init__(self) -> None:
        self.http = httpx.AsyncClient(timeout=15, headers={"User-Agent": USER_AGENT})

    async def place(self, lat: float, lon: float) -> str | None:
        r = await self.http.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 17},
        )
        r.raise_for_status()
        a = r.json().get("address") or {}
        street = a.get("road") or a.get("pedestrian") or a.get("neighbourhood")
        town = a.get("city") or a.get("town") or a.get("village") or a.get("municipality")
        parts = [p for p in (street, town) if p]
        return ", ".join(parts) or r.json().get("display_name")

    async def temperature(self, lat: float, lon: float, ts: float) -> float | None:
        day = datetime.fromtimestamp(ts, timezone.utc)
        r = await self.http.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": round(lat, 2), "longitude": round(lon, 2),
                "hourly": "temperature_2m", "timezone": "UTC",
                "start_date": day.strftime("%Y-%m-%d"), "end_date": day.strftime("%Y-%m-%d"),
            },
        )
        r.raise_for_status()
        temps = r.json().get("hourly", {}).get("temperature_2m") or []
        return temps[day.hour] if len(temps) > day.hour else None


class Enricher:
    def __init__(self, db: DB, lookups: Any) -> None:
        self.db = db
        self.lookups = lookups

    async def _place(self, lat: float, lon: float) -> str | None:
        # Saved places win, and ~100 m cells keep repeat visits to one lookup.
        for p in self.db.q("SELECT * FROM places"):
            if haversine_m(lat, lon, p["lat"], p["lon"]) <= p["radius_m"]:
                return p["name"]
        key = f"{lat:.3f},{lon:.3f}"
        hit = self.db.one("SELECT name FROM geocache WHERE key = ?", key)
        if hit:
            return hit["name"]
        name = await self.lookups.place(lat, lon)
        self.db.x("INSERT OR REPLACE INTO geocache (key, name) VALUES (?, ?)", key, name or "")
        return name

    async def run(self, now: float, limit: int = 2) -> int:
        """Fill in a few missing addresses and temperatures. Returns how many rows it touched."""
        if not get_settings(self.db)["lookups_enabled"]:
            return 0
        done = 0
        try:
            for r in self.db.q(
                "SELECT * FROM trips WHERE ended_at IS NOT NULL AND ("
                " (start_place IS NULL AND start_lat IS NOT NULL) OR"
                " (end_place IS NULL AND end_lat IS NOT NULL) OR"
                " (temp_c IS NULL AND end_lat IS NOT NULL AND ended_at > ?))"
                " ORDER BY started_at DESC LIMIT ?",
                # Open-Meteo's forecast endpoint only reaches about three months back.
                now - 80 * 86400, limit,
            ):
                values: dict[str, Any] = {}
                if r["start_place"] is None and r["start_lat"] is not None:
                    values["start_place"] = await self._place(r["start_lat"], r["start_lon"]) or ""
                if r["end_place"] is None and r["end_lat"] is not None:
                    values["end_place"] = await self._place(r["end_lat"], r["end_lon"]) or ""
                if r["temp_c"] is None and r["end_lat"] is not None:
                    values["temp_c"] = await self.lookups.temperature(
                        r["end_lat"], r["end_lon"], (r["started_at"] + r["ended_at"]) / 2
                    )
                self.db.update("trips", r["id"], values)
                done += 1
            for r in self.db.q(
                "SELECT * FROM charges WHERE place_name IS NULL AND lat IS NOT NULL"
                " ORDER BY started_at DESC LIMIT ?", limit,
            ):
                name = await self._place(r["lat"], r["lon"]) or ""
                self.db.update("charges", r["id"], {"place_name": name})
                done += 1
        except (httpx.HTTPError, ValueError, KeyError) as e:
            log.warning("Lookup failed: %s", e)
        return done


def describe(event: dict[str, Any], settings: dict[str, Any]) -> tuple[str, str]:
    """Title and one-line message for a notification."""
    name = settings["vehicle_name"] or "Volvo"
    t = event["type"]
    if t == "trip_ended":
        dist = event.get("distance_km") or 0
        if settings["units"] == "mi":
            text = f"{dist / 1.609344:.1f} mi"
        else:
            text = f"{dist:.1f} km"
        if event.get("energy_kwh"):
            text += f", {event['energy_kwh']:.1f} kWh"
        return f"{name}: trip ended", text
    if t == "trip_started":
        return f"{name}: trip started", ""
    if t == "charge_started":
        return f"{name}: charging started", f"at {event.get('soc', '?'):.0f}%"
    if t == "charge_ended":
        text = f"{event.get('soc') or 0:.0f}%"
        if event.get("energy_kwh"):
            text += f", {event['energy_kwh']:.1f} kWh added"
        return f"{name}: charging ended", text
    if t == "needs_reauth":
        return f"{name}: sign in to Volvo again", "Volvo Dash has stopped logging until you reconnect in Settings."
    return f"{name}: {t.replace('_', ' ')}", ""


async def send_webhook(url: str, title: str, message: str) -> None:
    text = f"{title}. {message}" if message else title
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            if "discord.com/api/webhooks" in url:
                await http.post(url, json={"content": text})
            elif "hooks.slack.com" in url:
                await http.post(url, json={"text": text})
            else:
                # Plain text with a Title header is what ntfy expects; most others accept it.
                await http.post(url, content=(message or title).encode(), headers={"Title": title})
    except httpx.HTTPError as e:
        log.warning("Webhook failed: %s", e)
