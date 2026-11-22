"""Regression tests for Home Assistant MQTT discovery payloads."""

import ast
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_PATH = REPO_ROOT / "samsung_mqtt_home_assistant.py"

# Live-verified Home Assistant entity_ids for default --nasa-mqtt-prefix="EHS".
# Changing any of these breaks existing HA dashboards, automations, or Recorder history.
EXPECTED_DEFAULT_ENTITY_IDS = {
    "binary_sensor.ehs_communication",
    "binary_sensor.ehs_defrosting",
    "binary_sensor.ehs_operating",
    "number.ehs_0x42f1_fr_limit",
    "number.ehs_fsv2011_heating_water_law_max",
    "number.ehs_fsv2012_heating_water_law_min",
    "number.ehs_fsv2021_heating_water_out_wl1_temp_max",
    "number.ehs_fsv2022_heating_water_out_wl1_temp_min",
    "number.ehs_fsv2031_heating_water_out_wl2_temp_max",
    "number.ehs_fsv2032_heating_water_out_wl2_temp_min",
    "number.ehs_fsv2051_cooling_water_law_max",
    "number.ehs_fsv2052_cooling_water_law_min",
    "number.ehs_fsv2061_cooling_water_out_wl1_temp_max",
    "number.ehs_fsv2062_cooling_water_out_wl1_temp_min",
    "number.ehs_fsv2071_cooling_water_out_wl2_temp_max",
    "number.ehs_fsv2072_cooling_water_out_wl2_temp_min",
    "number.ehs_fsv3021_dhw_temp_w_o_booster",
    "number.ehs_fsv3022_dhw_stop_temp",
    "number.ehs_fsv3023_dhw_start_temp",
    "number.ehs_fsv3024_dhw_sh_min_heating_duration",
    "number.ehs_fsv3025_dhw_sh_max_dhw_duration",
    "number.ehs_fsv3026_dhw_sh_max_heating_duration",
    "number.ehs_fsv3052_dhw_forced_timer_duration",
    "number.ehs_fsv4012_low_outdoor_temp_for_heating_priority",
    "number.ehs_fsv4013_heating_off_temperature",
    "number.ehs_fsv4042_mixing_valve_dt_heating",
    "number.ehs_fsv4043_mixing_valve_dt_cooling",
    "number.ehs_fsv4044_mixing_valve_factor",
    "number.ehs_fsv4045_mixing_valve_interval_minute",
    "number.ehs_fsv4046_mixing_valve_running_time_x10_seconds",
    "number.ehs_fsv4052_dt_target",
    "number.ehs_fsv4053_inverter_pump_factor",
    "number.ehs_inverter_pump_pwm",
    "number.ehs_temp_dhw_target",
    "number.ehs_water_law_offset",
    "number.ehs_zone1_ambient",
    "number.ehs_zone1_outlet_target",
    "number.ehs_zone1_target",
    "number.ehs_zone2_ambient",
    "number.ehs_zone2_outlet_target",
    "number.ehs_zone2_target",
    "select.ehs_fsv2041_heating_water_law",
    "select.ehs_fsv2081_cooling_water_law",
    "select.ehs_fsv2091_zone_1_ext_thermostat",
    "select.ehs_fsv2092_zone_2_ext_thermostat",
    "select.ehs_fsv2093_temp_control",
    "select.ehs_fsv3011_dhw_control",
    "select.ehs_fsv3031_dhw_booster_control",
    "select.ehs_fsv3071_dhw_v3v_default_direction",
    "select.ehs_fsv4011_dhw_heating_priority",
    "select.ehs_fsv4041_mixing_valve_enabled",
    "select.ehs_fsv4051_inverter_pump_control",
    "select.ehs_fsv4061_zone_control",
    "select.ehs_mode",
    "select.ehs_temperature_reference",
    "sensor.bridge_restart_date",
    "sensor.ehs_0x4090_air_efficiency",
    "sensor.ehs_0x40b2",
    "sensor.ehs_carnot_cop",
    "sensor.ehs_dhw_valve_direction_tank",
    "sensor.ehs_error_code_1",
    "sensor.ehs_input_power",
    "sensor.ehs_lwt_water_out",
    "sensor.ehs_of_carnot_cop",
    "sensor.ehs_operating_cop",
    "sensor.ehs_output_power",
    "sensor.ehs_rwt_water_in",
    "sensor.ehs_temp_dhw_tank",
    "sensor.ehs_temp_eva_in",
    "sensor.ehs_temp_mixing_valve_zone1",
    "sensor.ehs_temp_outer",
    "sensor.ehs_temp_outlet_zone1",
    "sensor.ehs_temp_outlet_zone2",
    "sensor.ehs_temp_water_law_target",
    "sensor.ehs_total_input_power",
    "sensor.ehs_total_output_power",
    "sensor.ehs_water_flow",
    "sensor.ehs_water_pressure",
    "sensor.ehs_water_target",
    "switch.ehs_dhw",
    "switch.ehs_fsv3041_dhw_disinfection",
    "switch.ehs_fsv3051_dhw_forced_timer_off",
    "switch.ehs_fsv5051_fr_control",
    "switch.ehs_fsv_unlock",
    "switch.ehs_silence_mode",
    "switch.ehs_silence_parameter",
    "switch.ehs_test_mode",
    "switch.ehs_test_mode_boiler_suppl",
    "switch.ehs_test_mode_dhw_v3v",
    "switch.ehs_test_mode_heater",
    "switch.ehs_test_mode_water_pump",
    "switch.ehs_test_mode_zone1_valve",
    "switch.ehs_test_mode_zone2_v3v",
    "switch.ehs_test_mode_zone2_valve",
    "switch.ehs_zone1",
    "switch.ehs_zone2",
    "text.ehs_raw_payload",
    "text.ehs_raw_reply",
}


def _collect_discovery_configs(prefix: str = "EHS") -> list[tuple[str, dict, bool]]:
    """Execute mqtt_setup() with mocked hardware/MQTT and capture /config publishes."""
    published: list[tuple[str, dict, bool]] = []
    fake_mqtt = MagicMock()

    def capture_publish(topic: str, payload: str | None = None, retain: bool = False, **kwargs):
        if topic.endswith("/config"):
            published.append((topic, json.loads(payload), retain))

    fake_mqtt.publish.side_effect = capture_publish

    source = APP_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(APP_PATH))

    with (
        patch.object(sys, "argv", [APP_PATH.name, "--nasa-mqtt-prefix", prefix]),
        patch("packetgateway.PacketGateway", return_value=MagicMock()),
        patch("time.sleep"),
        patch("threading.Thread"),
    ):
        namespace = {"__name__": "__test__", "__file__": str(APP_PATH)}
        exec(compile(tree, str(APP_PATH), "exec"), namespace)
        namespace["mqtt_client"] = fake_mqtt
        namespace["mqtt_published_vars"] = {}
        namespace["mqtt_setup"]()

    return published


@pytest.fixture(scope="module")
def default_discoveries() -> list[tuple[str, dict, bool]]:
    """Discovery messages published with the default 'EHS' prefix."""
    return _collect_discovery_configs("EHS")


def test_all_discovery_entities_published_and_retained(default_discoveries):
    """All 98 entities must publish valid, retained HA discovery payloads."""
    assert len(default_discoveries) == len(EXPECTED_DEFAULT_ENTITY_IDS)

    required_keys = {"name", "device", "unique_id", "default_entity_id", "state_topic"}
    for topic, payload, retain in default_discoveries:
        assert retain is True, f"Discovery config should be retained: {topic}"
        missing = required_keys - payload.keys()
        assert not missing, f"{topic} missing required fields: {missing}"
        domain = topic.split("/")[1]
        assert payload["default_entity_id"].startswith(f"{domain}."), (
            f"{topic}: default_entity_id {payload['default_entity_id']} does not match domain {domain}"
        )


def test_no_duplicate_identifiers(default_discoveries):
    """Config topics, unique_ids, and default_entity_ids must all be unique."""
    topics = [t for t, _, _ in default_discoveries]
    unique_ids = [p["unique_id"] for _, p, _ in default_discoveries]
    entity_ids = [p["default_entity_id"] for _, p, _ in default_discoveries]

    assert len(topics) == len(set(topics)), "Duplicate discovery topics found"
    assert len(unique_ids) == len(set(unique_ids)), "Duplicate unique_ids found"
    assert len(entity_ids) == len(set(entity_ids)), "Duplicate default_entity_ids found"


def test_default_entity_ids_match_live_baseline(default_discoveries):
    """Every default_entity_id must match the live-verified HA baseline to preserve history."""
    actual_entity_ids = {payload["default_entity_id"] for _, payload, _ in default_discoveries}
    missing = EXPECTED_DEFAULT_ENTITY_IDS - actual_entity_ids
    unexpected = actual_entity_ids - EXPECTED_DEFAULT_ENTITY_IDS

    assert not missing and not unexpected, (
        f"Entity ID drift detected!\nMissing: {sorted(missing)}\nUnexpected: {sorted(unexpected)}"
    )

    # Verify key special/manual entities explicitly
    by_topic = {topic: payload for topic, payload, _ in default_discoveries}
    expected_manual = {
        "homeassistant/binary_sensor/samsung_ehs_communication/config": (
            "EHS Communication",
            "binary_sensor.ehs_communication",
            "samsung_ehs_communication",
        ),
        "homeassistant/sensor/samsung_ehs_cop/config": (
            "EHS Operating COP",
            "sensor.ehs_operating_cop",
            "samsung_ehs_cop",
        ),
        "homeassistant/sensor/samsung_ehs_carnot_cop/config": (
            "EHS Carnot CoP",
            "sensor.ehs_carnot_cop",
            "samsung_ehs_carnot_cop",
        ),
        "homeassistant/sensor/samsung_ehs_carnot_pct_cop/config": (
            "EHS % of Carnot CoP",
            "sensor.ehs_of_carnot_cop",
            "samsung_ehs_carnot_pct_cop",
        ),
        "homeassistant/sensor/samsung_ehs_mqtt_bridge/config": (
            "Bridge restart date",
            "sensor.bridge_restart_date",
            "samsung_ehs_mqtt_bridge",
        ),
        "homeassistant/text/samsung_ehs_raw_payload/config": (
            "EHS Raw Payload",
            "text.ehs_raw_payload",
            "samsung_ehs_raw_payload",
        ),
        "homeassistant/text/samsung_ehs_raw_reply/config": (
            "EHS Raw Reply",
            "text.ehs_raw_reply",
            "samsung_ehs_raw_reply",
        ),
        "homeassistant/switch/samsung_ehs_fsv_unlock/config": (
            "EHS FSV Unlock",
            "switch.ehs_fsv_unlock",
            "samsung_ehs_fsv_unlock",
        ),
    }

    for topic, (exp_name, exp_entity_id, exp_unique_id) in expected_manual.items():
        assert topic in by_topic, f"Missing manual entity discovery: {topic}"
        payload = by_topic[topic]
        assert payload["name"] == exp_name
        assert payload["default_entity_id"] == exp_entity_id
        assert payload["unique_id"] == exp_unique_id


def test_device_metadata_and_energy_sensors(default_discoveries):
    """Device identifiers must stay clean (no samsung_ehs_ehs_) and energy sensors keep state_class."""
    by_topic = {topic: payload for topic, payload, _ in default_discoveries}

    for topic, payload, _ in default_discoveries:
        assert payload["device"] == {
            "identifiers": ["samsung_ehs"],
            "name": "EHS",
            "manufacturer": "Samsung",
            "model": "EHS",
        }, f"Unexpected device metadata on {topic}"
        assert payload["unique_id"].startswith("samsung_ehs_")
        assert not payload["unique_id"].startswith("samsung_ehs_ehs_")

    for energy_topic in (
        "homeassistant/sensor/samsung_ehs_total_output_power/config",
        "homeassistant/sensor/samsung_ehs_total_input_power/config",
    ):
        payload = by_topic[energy_topic]
        assert payload["device_class"] == "energy"
        assert payload["state_class"] == "total_increasing"
        assert payload["unit_of_measurement"] == "Wh"


@pytest.mark.parametrize(
    ("prefix", "expected_device_id", "expected_device_name", "expected_water_entity_id", "expected_cop_entity_id"),
    [
        ("EHS", "samsung_ehs", "EHS", "sensor.ehs_rwt_water_in", "sensor.ehs_carnot_cop"),
        ("EHS2", "samsung_ehs2", "EHS2", "sensor.ehs2_rwt_water_in", "sensor.ehs2_carnot_cop"),
        ("EHS Garage", "samsung_ehs_garage", "EHS Garage", "sensor.ehs_garage_rwt_water_in", "sensor.ehs_garage_carnot_cop"),
        ("Garage", "samsung_ehs_garage", "Garage", "sensor.garage_rwt_water_in", "sensor.garage_carnot_cop"),
        ("", "samsung_ehs", "EHS", "sensor.rwt_water_in", "sensor.carnot_cop"),
    ],
)
def test_multi_pump_prefixes(
    prefix,
    expected_device_id,
    expected_device_name,
    expected_water_entity_id,
    expected_cop_entity_id,
):
    """--nasa-mqtt-prefix must produce clean, distinct IDs for multiple heat pumps."""
    discoveries = _collect_discovery_configs(prefix)
    by_topic = {topic: payload for topic, payload, _ in discoveries}

    water = by_topic["homeassistant/sensor/samsung_ehs_temp_water_in/config"]
    assert water["device"]["identifiers"] == [expected_device_id]
    assert water["device"]["name"] == expected_device_name
    assert water["unique_id"] == f"{expected_device_id}_temp_water_in"
    assert water["default_entity_id"] == expected_water_entity_id

    cop = by_topic["homeassistant/sensor/samsung_ehs_carnot_cop/config"]
    assert cop["unique_id"] == f"{expected_device_id}_carnot_cop"
    assert cop["default_entity_id"] == expected_cop_entity_id

    bridge = by_topic["homeassistant/sensor/samsung_ehs_mqtt_bridge/config"]
    assert bridge["name"] == "Bridge restart date"
    assert bridge["default_entity_id"] == "sensor.bridge_restart_date"
    assert bridge["unique_id"] == f"{expected_device_id}_mqtt_bridge"
