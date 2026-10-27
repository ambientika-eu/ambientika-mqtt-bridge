# Ambientika MQTT Bridge – Home Assistant Add-on

This Home Assistant OS (HAOS) Add-on connects your Ambientika ventilation units to Home Assistant via MQTT with Auto-Discovery support.

## Installation

### Method 1: Add Repository to Home Assistant (Recommended)

1. In Home Assistant, go to **Settings > Apps** and click **Install app** (before HA 2026.2: **Settings > Add-ons > Add-on Store**)
2. Click the **three-dot menu** (top right) > **Repositories**
3. Add this URL:
```
https://github.com/ambientika-eu/ambientika-mqtt-bridge
```
4. Click **Add**, then close the dialog
5. Search for **"Ambientika MQTT Bridge"** in the store
6. Click **Install**

## Prerequisites

- An **MQTT broker**. If you have none, install the official **Mosquitto broker**
  add-on first and start it.
- An **Ambientika account** — the same e-mail address and password you use in the
  Ambientika app. There is no separate account for the bridge.

## Configuration

After installation, configure the add-on via the **Configuration** tab:

| Option | Default | Description |
|---|---|---|
| `ambientika_username` | *(required)* | E-mail address of your Ambientika app account |
| `ambientika_password` | *(required)* | Password of your Ambientika app account |
| `mqtt_host` | `core-mosquitto` | MQTT broker hostname |
| `mqtt_port` | `1883` | MQTT broker port |
| `mqtt_username` | *(empty)* | Broker user — **required** for the Mosquitto add-on, which refuses anonymous connections |
| `mqtt_password` | *(empty)* | Broker password |
| `mqtt_topic_prefix` | `ambientika` | MQTT topic prefix |
| `poll_interval` | `30` | Polling interval in seconds (10-300) |
| `availability_failure_threshold` | `3` | Consecutive failed reads before a unit is shown unavailable |
| `log_level` | `INFO` | Log verbosity |
| `command_coalesce_ms` | `800` | Commands for the same unit within this window are applied in one call (`0` = immediately) |
| `average_window_min` | `30` | Window of the averaged sensors *Humidity (averaged)* and *Temperature (averaged)*, in minutes (1–240; at least twenty poll intervals; details in DOCS.md) |
| `average_window_devices` | *(empty)* | Own window for single units: `unit=minutes` entries, comma-separated, by serial number or by the unit's name as in the app (1–240 minutes), e.g. `A1B2C3D4E5F6=15, Schlafzimmer Eltern=60`. The serial number is the safe way (it is in the add-on log and in the app); a name must match the app's spelling - upper/lower case does not matter, and a hyphen, underscore or space count as the same separator; anything else does matter. Units not listed use `average_window_min` (details in DOCS.md) |
| `humidity_avg_hysteresis_devices` | *(empty)* | Hysteresis of *Humidity (averaged)* for single units, in percentage points: `unit=points` entries like `average_window_devices` (0–10), e.g. `A1B2C3D4E5F6=1`. The shown value only moves once the mean has left it by at least that much - calmer at night with a short window, a shower passes at once. Units not listed: none (details in DOCS.md) |
| `slave_filter_soft_reset` | `false` | Maintenance acknowledgement for Slave filter counters |
| `filter_ack_ttl_days` | `90` | How long an acknowledgement stays valid |

Leaving `mqtt_username` and `mqtt_password` empty is the most common setup
mistake: the log then shows `MQTT connection failed (rc=5)`.

### NeuraCell-X (radon protection and dew-point control)

The options starting with `radon_` and `dewpoint_` configure the radon and
dew-point protection. They only matter if you have the matching hardware, and
the defaults are safe to leave alone. The Ambientika radon meter
(`radon/<id>/state`) and the Ambientika Taupunktsteuerung (`dew-point/<id>/state`,
`ventilating`) are read directly with the defaults, nothing to configure. The
sensors *Radon Meter Connected* and *Dew Point Controller Connected* show whether
the two devices are still talking to the broker: their Last Will counts at once,
a radon meter silent for longer than `radon_meter_timeout` (30 min) counts as
disconnected as well, also after an add-on restart (the meters seen so far are
remembered on the broker). A third-party dew-point controller needs
`dewpoint_availability_topic` or `dewpoint_signal_timeout` for that (with
`dewpoint_source: device` it follows the cloud reads instead). How the two
protections work together (radon first, exact restore of every unit's mode) is
described in the
[project README](https://github.com/ambientika-eu/ambientika-mqtt-bridge#neuracell-x--patent-pending-radon--dew-point-protection).

**Radon protection** - radon alarm: every unit to Intake (supply air) at
`radon_protection_fan` until the value is back below the threshold minus the
hysteresis.

| Option | Default | Description |
|---|---|---|
| `neuracell_enabled` | `true` | Radon protection on or off |
| `radon_source` | `signal` | `signal`: the radon value and alarm come from the MQTT topics below. `device`: the radon meter's status is read from the Ambientika cloud, no MQTT signal needed |
| `radon_threshold` | `300` | Bq/m³ at which radon protection starts (50–1000; 300 is the German reference value). With `radon_source: device` also the limit for a numeric alarm field |
| `radon_hysteresis` | `50` | Bq/m³ the value must fall below the threshold before protection ends (0–500) |
| `radon_protection_fan` | `Low` | Fan speed during radon protection: `Low`, `Medium` or `High` |
| `radon_meter_topic` | `radon/+/state` | Native topic of the Ambientika radon meter (MQTT mode 4, `{"mittelwert": …}`), read directly. `none` switches the direct read off |
| `radon_meter_timeout` | `30` | Minutes without any message from an Ambientika radon meter before it counts as disconnected (0 = off; the meter reports every 10 minutes, 20 or more is recommended) |
| `radon_topic` | `ambientika/radon/value` | Topic with a radon value in Bq/m³ from any other source: a plain number or JSON (see `radon_value_key`); MQTT wildcards allowed. With several sources the highest current value decides |
| `radon_alarm_topic` | `ambientika/radon/alarm` | Topic with an explicit alarm (`ON`/`OFF`, `true`/`false`). Value and alarm are combined, either one keeps protection on. `none` = ignored, so a stale retained `ON` can never hold protection |
| `radon_value_key` | `mittelwert` | JSON key of the radon value when `radon_topic` or the meter topic carries JSON |
| `radon_device_serial` | *(empty)* | `radon_source: device`: serial number of the radon meter (shown in the add-on log). The meter is read only, never exposed as a fan |
| `radon_device_alarm_field` | `air_quality` | `radon_source: device`: the status field of the meter that carries its alarm or level. A numeric field is compared with `radon_threshold`, a text field with the values below |
| `radon_device_alarm_values` | `Bad,Poor,Very Bad,Alarm,Alert` | `radon_source: device`: text values of that field that mean "radon alarm" (comma-separated, upper/lower case does not matter) |

**Dew-point control** - ventilating would raise indoor humidity: the units in
`dewpoint_block_devices` switch off until conditions are favourable again. Radon
protection always has priority.

| Option | Default | Description |
|---|---|---|
| `dewpoint_enabled` | `true` | Dew-point control on or off |
| `dewpoint_source` | `signal` | `signal`: a block signal from MQTT - the Ambientika Taupunktsteuerung on its own topic (below) or a third-party source on `dewpoint_block_topic`. `computed`: the bridge computes indoor and outdoor dew point from four sensor topics. `device`: the Taupunktsteuerung's status is read from the Ambientika cloud |
| `dewpoint_block_devices` | *(empty)* | Units the block applies to, by name as in the app or serial number, comma-separated (e.g. the basement units). Empty = all units |
| `dewpoint_controller_topic` | `dew-point/+/state` | Native topic of the Ambientika Taupunktsteuerung (MQTT firmware), read directly; its Last Will on `dew-point/<id>/availability/state` and its sensor messages are watched automatically. `none` switches the direct read off |
| `dewpoint_controller_key` | `ventilating` | JSON key in those messages; `false` means: block ventilation |
| `dewpoint_block_topic` | `ambientika/dewpoint/block` | `signal`: topic of a third-party block signal (`ON`/`true`/`1` = block). The last message wins. Leave it at its default so it does not overlap the controller topic |
| `dewpoint_block_key` | *(empty)* | JSON key of the block flag when `dewpoint_block_topic` carries a JSON object |
| `dewpoint_availability_topic` | *(empty)* | `signal`: availability topic with Last Will of a third-party controller (`online`/`offline`, `true`/`false` or JSON with `state`). The Ambientika controller needs none |
| `dewpoint_signal_timeout` | `0` | `signal`: minutes without any message from the controller before it counts as disconnected (0 = off). For controllers without Last Will; not applied while a Last Will reports online |
| `dewpoint_lost_action` | `keep` | While the controller counts as disconnected: `keep` the last block state, `release` ventilation, or `block` the units. Radon keeps priority either way |
| `dewpoint_indoor_temp_topic` | `ambientika/dewpoint/indoor_temp` | `computed`: indoor temperature in °C |
| `dewpoint_indoor_humidity_topic` | `ambientika/dewpoint/indoor_humidity` | `computed`: indoor relative humidity in % |
| `dewpoint_outdoor_temp_topic` | `ambientika/dewpoint/outdoor_temp` | `computed`: outdoor temperature in °C |
| `dewpoint_outdoor_humidity_topic` | `ambientika/dewpoint/outdoor_humidity` | `computed`: outdoor relative humidity in % |
| `dewpoint_margin` | `1.0` | `computed`: block as soon as the outdoor dew point reaches the indoor dew point minus this margin (°C) - ventilating would then add moisture |
| `dewpoint_hysteresis` | `0.5` | `computed`: release only once the outdoor dew point is this much (°C) below that line again |
| `dewpoint_device_serial` | *(empty)* | `dewpoint_source: device`: serial number of the Taupunktsteuerung (shown in the add-on log). It is read only, never exposed as a fan |
| `dewpoint_device_block_modes` | `Off` | `dewpoint_source: device`: operating modes of the Taupunktsteuerung that mean "block" (comma-separated) |

The full user documentation is in [DOCS.md](DOCS.md); Home Assistant shows it in
the add-on's **Documentation** tab.

## Home Assistant Auto-Discovery

The bridge publishes MQTT Auto-Discovery messages, so your units appear
automatically under **Settings > Devices & Services > MQTT > Devices**. One
device per unit, with these entities (names as Home Assistant shows them;
the field in the state payload in brackets):

**Controls** - *Mode* (`operating_mode`), *Fan Speed* (`fan_speed`), *Humidity
Level* (`humidity_level`), *Light Sensor Level* (`light_sensor_level`) and the
button *Filter zuruecksetzen*.

**Sensors** - *Temperature* and *Humidity* (`temperature`, `humidity`: the raw
reading, it swings with the unit's reversing airflow), *Temperature (averaged)*
and *Humidity (averaged)* (`temperature_avg`, `humidity_avg`: the time-weighted
mean, see DOCS.md), *Air Quality*, *Filter Status* (effective) and *Filter Status
raw* (the unit's own counter), *Mode* (effective: a Slave shows its Master's) and
*Mode raw* (the unit's own field), *Fan Speed*, *Humidity Level*, *Light Sensor
Level*, *Device Role*, *Last Mode* (`last_operating_mode`: the concrete function
a unit in `Smart` / `Auto` is running right now), *Zone*. The text values except
*Device Role* also have a numeric companion, *… (num)*, for statistics and Grafana.

**Binary sensors** - *Humidity Alarm*, *Night Alarm*.

**Diagnostic** - *Filter Reset Status* (disabled by default: `idle`, `running`,
`confirmed`, `acknowledged`, `unconfirmed`) and, with `slave_filter_soft_reset`
on, *Filter Acknowledged Since* and *Filter Acknowledgement Valid Until*.

**NeuraCell-X** (one extra device) - *Radon Protection Active*, *Radon Level*,
*Radon Meter Connected*, *Dew Point Controller Connected*, *Ventilation Blocked
(Dew Point)*, *Dew Point Indoor*, *Dew Point Outdoor*.

**Ambientika MQTT Bridge** (one more device) - the connectivity sensor
*Ambientika Bridge*: whether the add-on itself is running and connected.

### Distinguishing the internal state in SMART mode

The *Mode* control shows the *set* macro-mode. In the automatic modes
(`Smart` / `Auto`) that value stays `Smart`, even though the unit internally
switches between concrete functions - heat recovery vs. free cooling
(`MasterSlaveFlow`), night mode, etc. The read-only sensor *Last Mode*
(`last_operating_mode`) shows the concrete function the unit reports, and *Fan
Speed* the real running speed, so together they show what SMART is doing at any
moment.

## Example automations

Entity ids below are examples - take yours from the device page in Home
Assistant.

```yaml
# Boost a bathroom when the averaged humidity stays high (the raw value would
# fire on a random phase of the reversing cycle)
automation:
  - alias: "Ambientika bathroom boost"
    trigger:
      - platform: numeric_state
        entity_id: sensor.bad_humidity_averaged
        above: 70
        for: "00:10:00"
    action:
      - service: select.select_option
        target:
          entity_id: select.bad_fan_speed
        data:
          option: High

# Filter notification: Filter Status (num) is 0 = Good, 1 = Medium (dirty), 2 = Bad
  - alias: "Ambientika filter service"
    trigger:
      - platform: numeric_state
        entity_id: sensor.wohnzimmer_filter_status_num
        above: 0
    action:
      - service: notify.mobile_app
        data:
          title: "Ambientika filter"
          message: "Please clean or replace the filter in the living room unit."
```

## Support

- GitHub: https://github.com/ambientika-eu/ambientika-mqtt-bridge
- Website: https://www.ambientika.eu
