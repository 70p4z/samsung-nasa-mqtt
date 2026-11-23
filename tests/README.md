# Running Tests

This test suite verifies Home Assistant MQTT discovery payloads without requiring a live MQTT broker, Docker, or NASA hardware.

## Setup

Create a virtual environment (if you don't already have `.venv`) and install dependencies:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest
```

## Running Tests

Run the full test suite:

```bash
.venv/bin/python -m pytest -v
```

Run only the discovery regression tests:

```bash
.venv/bin/python -m pytest -v tests/test_discovery.py
```

Run a specific test by name:

```bash
.venv/bin/python -m pytest -v -k "multi_pump"
```

## What `tests/test_discovery.py` Covers

- **Retained valid discovery payloads** — all 98 entities publish valid JSON with `retain=True` and required Home Assistant keys (`name`, `device`, `unique_id`, `default_entity_id`, `state_topic`).
- **No duplicate IDs** — ensures every config topic, `unique_id`, and `default_entity_id` is unique.
- **Home Assistant `entity_id` stability** — compares all 98 generated `default_entity_id` values against `EXPECTED_DEFAULT_ENTITY_IDS` so refactoring cannot accidentally rename entities or break HA dashboards/history.
- **Device metadata & energy sensors** — checks the `device` block (`identifiers: ["samsung_ehs"]`, no `samsung_ehs_ehs_` regression) and `state_class: "total_increasing"` on energy counters.
- **Multi-pump `--nasa-mqtt-prefix` support** — tests `EHS`, `EHS2`, `EHS Garage`, `Garage`, and empty prefixes to ensure distinct device/entity identifiers.

## Updating Expected Entities

When you intentionally add or remove an entity in `samsung_mqtt_home_assistant.py`, update `EXPECTED_DEFAULT_ENTITY_IDS` in `tests/test_discovery.py` and re-run `pytest`. Avoid changing existing `default_entity_id` or `unique_id` values unless you plan to migrate them in Home Assistant.
