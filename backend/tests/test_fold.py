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


def test_steady_power_is_added_up_across_long_reporting_gaps():
    from volvodash.detect import integrate_power

    # The real EX40 overnight: 2.7 kW throughout, reports 3 to 20 minutes apart.
    samples = [(0, 2700.0), (180, 2712.0), (1380, 2688.0), (1560, 2700.0)]
    kwh, covered = integrate_power(samples)
    assert covered == 1560
    assert abs(kwh - 2.7 * 1560 / 3600) < 0.01
    # A big change in power across a long gap is not guessed at.
    kwh, covered = integrate_power([(0, 11000.0), (1800, 2700.0)])
    assert covered == 0
