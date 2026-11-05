"""Publishes the car to an MQTT broker, with Home Assistant discovery.

Topics, with <id> = "volvodash_" + the last six characters of the VIN:
  volvodash/<id>/status     "online" / "offline" (retained, last will)
  volvodash/<id>/state      JSON with every value below (retained)
  volvodash/<id>/location   JSON for the device tracker (retained)
  volvodash/<id>/event      JSON per trip/charging event (not retained)
  volvodash/<id>/command    send "lock", "climatization-start",
                            "climatization-stop" or "refresh"
Home Assistant picks everything up from <discovery prefix>/.../config.
"""

import asyncio
import json
import logging
import ssl
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import aiomqtt

from .collector import Collector
from .db import DB, get_settings
from .periods import after_charge

log = logging.getLogger(__name__)

# Unlock is deliberately not offered: a Home Assistant automation or a
# compromised broker should never be able to open the car.
COMMANDS = ("lock", "climatization-start", "climatization-stop", "refresh")

OPEN_STATES = ("OPEN", "AJAR")
DOORS = ("frontLeftDoor", "frontRightDoor", "rearLeftDoor", "rearRightDoor", "hood", "tailgate")
WINDOWS = ("frontLeftWindow", "frontRightWindow", "rearLeftWindow", "rearRightWindow", "sunroof")

# key, name, unit, device_class, state_class, icon
SENSORS: list[tuple[str, str, str | None, str | None, str | None, str | None]] = [
    ("battery_level", "Battery", "%", "battery", "measurement", None),
    ("range", "Range", "km", "distance", "measurement", "mdi:map-marker-distance"),
    ("charging_status", "Charging status", None, None, None, "mdi:ev-station"),
    ("charging_type", "Charging type", None, None, None, "mdi:ev-plug-type2"),
    ("charging_power", "Charging power", "kW", "power", "measurement", None),
    ("charging_time_left", "Charging time left", "min", "duration", None, "mdi:timer-outline"),
    ("charge_limit", "Charge limit", "%", None, None, "mdi:battery-charging-80"),
    ("session_energy", "Charging session energy", "kWh", "energy", None, None),
    ("odometer", "Odometer", "km", "distance", "total_increasing", "mdi:counter"),
    ("trip_meter", "Trip meter", "km", "distance", None, "mdi:map-marker-path"),
    ("average_consumption", "Average consumption", "kWh/100km", None, "measurement", "mdi:lightning-bolt"),
    ("distance_to_service", "Distance to service", "km", "distance", None, "mdi:wrench-clock"),
    ("activity", "Activity", None, None, None, "mdi:car-info"),
    ("last_reported", "Car last reported", None, "timestamp", None, None),
    ("last_trip_distance", "Last trip distance", "km", "distance", None, "mdi:map-marker-path"),
    ("last_trip_energy", "Last trip energy", "kWh", "energy", None, None),
    ("last_trip_consumption", "Last trip consumption", "kWh/100km", None, None, "mdi:lightning-bolt"),
    ("last_trip_end", "Last trip ended", None, "timestamp", None, None),
    ("last_charge_energy", "Last charge energy", "kWh", "energy", None, None),
    ("last_charge_cost", "Last charge cost", None, None, None, "mdi:cash"),
    ("last_charge_end", "Last charge ended", None, "timestamp", None, None),
    ("since_charge_distance", "Distance since last charge", "km", "distance", None, "mdi:map-marker-distance"),
    ("since_charge_energy", "Energy since last charge", "kWh", "energy", None, None),
    ("since_charge_consumption", "Consumption since last charge", "kWh/100km", None, None, "mdi:lightning-bolt"),
    ("since_charge_battery_used", "Battery used since last charge", "%", None, None, "mdi:battery-minus"),
    ("full_charge_range", "Range on a full charge", "km", "distance", None, "mdi:map-marker-radius"),
    ("api_calls_today", "Volvo API calls today", None, None, "measurement", "mdi:api"),
    ("sign_in_valid_until", "Volvo sign-in valid until", None, "timestamp", None, "mdi:account-clock"),
]

# key, name, device_class
BINARY_SENSORS: list[tuple[str, str, str | None]] = [
    ("plugged_in", "Plugged in", "plug"),
    ("charging", "Charging", "battery_charging"),
    ("unlocked", "Doors unlocked", "lock"),  # HA's lock class: on = unlocked
    ("door_open", "Door open", "door"),
    ("window_open", "Window open", "window"),
    ("driving", "Driving", "moving"),
    ("warning", "Warning", "problem"),
    ("sign_in_needed", "Volvo sign-in needed", "problem"),
]

BUTTONS = [
    ("lock", "Lock"),
    ("climatization-start", "Start climate"),
    ("climatization-stop", "Stop climate"),
    ("refresh", "Check now"),
]


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


def _round(v: float | None, digits: int = 1) -> float | None:
    return round(v, digits) if v is not None else None


def device_id(db: DB) -> str | None:
    vin = db.get("volvo_vin", "")
    return f"volvodash_{vin[-6:].lower()}" if vin else None


def state_payload(db: DB, collector: Collector) -> dict[str, Any]:
    """Everything published on the state topic. All metric; Home Assistant converts."""
    o = collector.obs
    state = {r["key"]: json.loads(r["value"]) for r in db.q("SELECT key, value FROM state")}
    d = collector.detector.s
    charge = db.one("SELECT energy_kwh FROM charges WHERE id = ?", d["charge"]["id"]) if d["charge"] else None
    trip = db.one("SELECT * FROM trips WHERE ended_at IS NOT NULL ORDER BY started_at DESC LIMIT 1")
    last = db.one("SELECT * FROM charges WHERE ended_at IS NOT NULL ORDER BY started_at DESC LIMIT 1")
    last_cost = None
    if last:
        energy = last["energy_override"] if last["energy_override"] is not None else last["energy_kwh"]
        if last["cost_override"] is not None:
            last_cost = last["cost_override"]
        elif energy is not None and last["price_per_kwh"] is not None:
            last_cost = round(energy * last["price_per_kwh"], 2)
    warnings = sorted(
        k for k, v in state.items()
        if k.endswith("Warning") and v not in ("NO_WARNING", "UNSPECIFIED", None)
    )
    stamps = [t for t in (o.soc_ts, o.charge_ts, o.engine_ts, o.availability_ts,
                          o.odometer_ts, o.location_ts) if t]
    # While charging, the drive since the last charge is over: report zero.
    since = None if d["charge"] or not last else after_charge(db, dict(last), o.soc)
    lock = state.get("centralLock")
    return {
        "battery_level": o.soc,
        "range": o.range_km,
        "charging_status": (o.charging_status or "").lower() or None,
        "charging_type": (o.charge_type or "").lower() or None,
        "charging_power": _round(o.power_w / 1000, 2) if o.power_w is not None else None,
        "charging_time_left": o.mins_to_target if d["charge"] else 0,
        "charge_limit": o.target_soc,
        "session_energy": _round(charge["energy_kwh"], 2) if charge else 0,
        "odometer": o.odometer_km,
        "trip_meter": o.trip_auto_km,
        "average_consumption": state.get("averageEnergyConsumption"),
        "distance_to_service": state.get("distanceToService"),
        "activity": collector.status.get("mode"),
        "last_reported": _iso(max(stamps)) if stamps else None,
        "last_trip_distance": trip["distance_km"] if trip else None,
        "last_trip_energy": trip["energy_kwh"] if trip else None,
        "last_trip_consumption": trip["efficiency"] if trip else None,
        "last_trip_end": _iso(trip["ended_at"]) if trip else None,
        "last_charge_energy": _round(last["energy_kwh"], 2) if last else None,
        "last_charge_cost": last_cost,
        "last_charge_end": _iso(last["ended_at"]) if last else None,
        "since_charge_distance": since["distance_km"] if since else 0,
        "since_charge_energy": since["energy_kwh"] if since else 0,
        "since_charge_consumption": since["efficiency"] if since else None,
        "since_charge_battery_used": since["soc_used"] if since else 0,
        "full_charge_range": since["full_range_km"] if since else None,
        "api_calls_today": collector.client.calls_today(),
        "plugged_in": o.connection == "CONNECTED",
        "charging": o.charging_status == "CHARGING",
        "unlocked": None if lock is None else lock != "LOCKED",
        "door_open": any(state.get(k) in OPEN_STATES for k in DOORS),
        "window_open": any(state.get(k) in OPEN_STATES for k in WINDOWS),
        "driving": bool(d["trip"]),
        "warning": bool(warnings),
        "warnings": warnings,
        "sign_in_needed": bool(db.get("volvo_auth_error")),
        "sign_in_valid_until": _iso(until) if (until := collector.client.sign_in_until()) else None,
    }


def location_payload(collector: Collector) -> dict[str, Any] | None:
    o = collector.obs
    if o.lat is None or o.lon is None:
        return None
    return {"latitude": o.lat, "longitude": o.lon, "gps_accuracy": 20,
            "reported_at": _iso(o.location_ts)}


def discovery(db: DB, prefix: str, discovery_prefix: str) -> dict[str, dict[str, Any]]:
    """Home Assistant discovery configs, keyed by topic."""
    dev = device_id(db)
    if not dev:
        return {}
    vehicle = db.get("volvo_vehicle") or {}
    name = get_settings(db)["vehicle_name"] or (vehicle.get("descriptions") or {}).get("model") or "Volvo"
    base = f"{prefix}/{dev}"
    device = {
        "identifiers": [dev],
        "name": name,
        "manufacturer": "Volvo",
        "model": " ".join(str(x) for x in ((vehicle.get("descriptions") or {}).get("model"),
                                           vehicle.get("modelYear")) if x),
        "sw_version": "Volvo Dash",
    }
    common = {"availability_topic": f"{base}/status", "device": device}
    out: dict[str, dict[str, Any]] = {}

    for key, label, unit, dclass, sclass, icon in SENSORS:
        cfg: dict[str, Any] = {
            **common, "name": label, "unique_id": f"{dev}_{key}",
            "object_id": f"{dev}_{key}", "state_topic": f"{base}/state",
            "value_template": f"{{{{ value_json.{key} }}}}",
        }
        if unit:
            cfg["unit_of_measurement"] = unit
        if dclass:
            cfg["device_class"] = dclass
        if sclass:
            cfg["state_class"] = sclass
        if icon:
            cfg["icon"] = icon
        if key == "last_charge_cost":
            cfg["unit_of_measurement"] = get_settings(db)["currency"]
        if key in ("api_calls_today", "activity", "last_reported", "sign_in_valid_until"):
            cfg["entity_category"] = "diagnostic"
        out[f"{discovery_prefix}/sensor/{dev}/{key}/config"] = cfg

    for key, label, dclass in BINARY_SENSORS:
        cfg = {
            **common, "name": label, "unique_id": f"{dev}_{key}",
            "object_id": f"{dev}_{key}", "state_topic": f"{base}/state",
            "value_template": f"{{{{ 'ON' if value_json.{key} else 'OFF' }}}}",
        }
        if dclass:
            cfg["device_class"] = dclass
        if key == "warning":
            cfg["json_attributes_topic"] = f"{base}/state"
            cfg["json_attributes_template"] = "{{ {'warnings': value_json.warnings} | tojson }}"
        if key == "sign_in_needed":
            cfg["entity_category"] = "diagnostic"
        out[f"{discovery_prefix}/binary_sensor/{dev}/{key}/config"] = cfg

    out[f"{discovery_prefix}/device_tracker/{dev}/location/config"] = {
        **common, "name": "Location", "unique_id": f"{dev}_location",
        "object_id": f"{dev}_location", "json_attributes_topic": f"{base}/location",
        "source_type": "gps", "icon": "mdi:car",
    }

    commands = set(db.get("volvo_commands", []))
    for key, label in BUTTONS:
        needed = {"lock": "LOCK", "climatization-start": "CLIMATIZATION_START",
                  "climatization-stop": "CLIMATIZATION_STOP"}.get(key)
        if needed and needed not in commands:
            continue
        out[f"{discovery_prefix}/button/{dev}/{key.replace('-', '_')}/config"] = {
            **common, "name": label, "unique_id": f"{dev}_{key.replace('-', '_')}",
            "object_id": f"{dev}_{key.replace('-', '_')}",
            "command_topic": f"{base}/command", "payload_press": key,
        }
    return out


class MqttBridge:
    def __init__(self, db: DB, collector: Collector,
                 run_command: Callable[[str], Awaitable[Any]]) -> None:
        self.db = db
        self.collector = collector
        self.run_command = run_command
        self.status: dict[str, Any] = {"state": "off", "error": None, "since": None}
        self._changed = asyncio.Event()

    def reconfigure(self) -> None:
        self._changed.set()

    def _config(self) -> dict[str, Any]:
        s = get_settings(self.db)
        return {k: s[k] for k in s if k.startswith("mqtt_")}

    async def run(self) -> None:
        backoff = 5
        while True:
            cfg = self._config()
            self._changed.clear()
            dev = device_id(self.db)
            if not cfg["mqtt_enabled"] or not cfg["mqtt_host"] or not dev:
                self.status = {"state": "off" if not cfg["mqtt_enabled"] else "waiting",
                               "error": None if cfg["mqtt_enabled"] is False else
                               ("Set a broker address." if not cfg["mqtt_host"] else
                                "Waiting for a car to be connected."), "since": None}
                await self._changed.wait()
                continue
            try:
                await self._session(cfg, dev)
                backoff = 5
            except aiomqtt.MqttError as e:
                self.status = {"state": "error", "error": str(e), "since": datetime.now(timezone.utc).isoformat()}
                self.collector.broadcast()
                log.warning("MQTT: %s; retrying in %ss", e, backoff)
                try:
                    await asyncio.wait_for(self._changed.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 300)

    async def _session(self, cfg: dict[str, Any], dev: str) -> None:
        prefix = cfg["mqtt_topic_prefix"].strip("/") or "volvodash"
        base = f"{prefix}/{dev}"
        tls = ssl.create_default_context() if cfg["mqtt_tls"] else None
        async with aiomqtt.Client(
            hostname=cfg["mqtt_host"], port=int(cfg["mqtt_port"] or (8883 if tls else 1883)),
            username=cfg["mqtt_username"] or None, password=cfg["mqtt_password"] or None,
            identifier=f"{dev}-{abs(hash(base)) % 10000}", tls_context=tls,
            will=aiomqtt.Will(f"{base}/status", "offline", qos=1, retain=True),
        ) as client:
            self.status = {"state": "connected", "error": None,
                           "since": datetime.now(timezone.utc).isoformat()}
            self.collector.broadcast()
            log.info("MQTT connected to %s", cfg["mqtt_host"])
            for topic, payload in discovery(self.db, prefix, cfg["mqtt_discovery_prefix"]).items():
                await client.publish(topic, json.dumps(payload), qos=1, retain=True)
            await client.publish(f"{base}/status", "online", qos=1, retain=True)
            await client.subscribe(f"{base}/command", qos=1)
            # Home Assistant republishes "online" here when it restarts; resend then.
            await client.subscribe(f"{cfg['mqtt_discovery_prefix']}/status")

            updates: asyncio.Queue[str] = asyncio.Queue(maxsize=50)
            self.collector.listeners.add(updates)
            last: dict[str, str] = {}

            async def publish_state(force: bool = False) -> None:
                for topic, payload in ((f"{base}/state", state_payload(self.db, self.collector)),
                                       (f"{base}/location", location_payload(self.collector))):
                    if payload is None:
                        continue
                    text = json.dumps(payload)
                    if force or last.get(topic) != text:
                        await client.publish(topic, text, qos=1, retain=True)
                        last[topic] = text

            async def publish_events() -> None:
                seen = self.db.one("SELECT MAX(id) m FROM events")["m"] or 0
                while True:
                    await updates.get()
                    await publish_state()
                    for e in self.db.q("SELECT * FROM events WHERE id > ? ORDER BY id", seen):
                        seen = e["id"]
                        await client.publish(f"{base}/event", json.dumps(
                            {"type": e["type"], "at": _iso(e["ts"]), **json.loads(e["detail"] or "{}")}), qos=1)

            async def listen() -> None:
                async for message in client.messages:
                    topic = str(message.topic)
                    payload = message.payload.decode() if isinstance(message.payload, bytes) else str(message.payload)
                    if topic.endswith("/status") and payload == "online":
                        for t, p in discovery(self.db, prefix, cfg["mqtt_discovery_prefix"]).items():
                            await client.publish(t, json.dumps(p), qos=1, retain=True)
                        await publish_state(force=True)
                    elif topic == f"{base}/command":
                        await self._command(payload.strip())

            try:
                await publish_state(force=True)
                tasks = [asyncio.ensure_future(c) for c in (publish_events(), listen(), self._changed.wait())]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
                for t in done:
                    if t.exception():
                        raise t.exception()  # type: ignore[misc]
                # Settings changed: say goodbye cleanly before reconnecting.
                await client.publish(f"{base}/status", "offline", qos=1, retain=True)
            finally:
                self.collector.listeners.discard(updates)

    async def _command(self, name: str) -> None:
        if name not in COMMANDS:
            log.warning("MQTT: ignoring unknown command %r", name)
            return
        try:
            await self.run_command(name)
        except Exception as e:  # a failed command must not drop the connection
            log.warning("MQTT command %s failed: %s", name, e)
