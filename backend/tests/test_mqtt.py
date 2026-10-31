"""What goes to Home Assistant, built from a simulated car."""

import json

from volvodash.mqtt import COMMANDS, discovery, location_payload, state_payload


def test_discovery_and_state_line_up(rig):
    r = rig("live")
    r.run(2)
    configs = discovery(r.db, "volvodash", "homeassistant")
    state = state_payload(r.db, r.collector)
    json.dumps(state)  # must be publishable as-is

    dev = "volvodash_d00001"
    assert f"homeassistant/device_tracker/{dev}/location/config" in configs
    for topic, cfg in configs.items():
        assert cfg["unique_id"].startswith(dev)
        assert cfg["availability_topic"] == f"volvodash/{dev}/status"
        if "value_template" in cfg:
            # Every entity reads a key that is actually in the state payload.
            key = cfg["value_template"].split("value_json.")[1].split()[0]
            assert key in state, topic
        if "command_topic" in cfg:
            assert cfg["payload_press"] in COMMANDS
    assert "unlock" not in COMMANDS
    assert 0 <= state["battery_level"] <= 100
    assert state["odometer"] > 1000
    assert location_payload(r.collector)["latitude"]
