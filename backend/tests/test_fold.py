from volvodash.detect import Obs
from volvodash.fold import fold


def level(value, ts):
    return {"batteryChargeLevel": {"value": value, "timestamp": ts}}


def test_older_battery_reading_from_another_endpoint_does_not_win():
    o = fold(Obs(), {"energy": {"batteryChargeLevel": {
        "status": "OK", "value": 46, "updatedAt": "2026-10-30T16:35:00Z"}}}, 1)
    o = fold(o, {"fuel": level(45.0, "2026-10-30T16:27:07.743Z")}, 2)
    assert o.soc == 46
    o = fold(o, {"fuel": level(47.0, "2026-10-30T16:40:00.000Z")}, 3)
    assert o.soc == 47
