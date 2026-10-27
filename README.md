# Ambientika MQTT Bridge

**MQTT bridge for Ambientika ventilation units** – connects the Ambientika Cloud API to any local MQTT broker with full Home Assistant Auto-Discovery support.

> Works with: Home Assistant · Apple Home · Google Home · Amazon Alexa · Node-RED · Loxone · ioBroker · Matter · openHAB · Homey · and any MQTT-capable platform

---

<p align="center">
  <img src="neuracell-x-logo.png" alt="NeuraCell-X - AI Neural Control System (patent-pending)" width="480">
</p>

<h3 align="center">Powered by NeuraCell-X&reg; &mdash; the patent-pending AI Neural Control System</h3>

<p align="center">
  <b>Active radon protection</b> &nbsp;&middot;&nbsp; <b>Intelligent dew-point ventilation</b> &nbsp;&middot;&nbsp; <b>Whole-home, fully automatic</b>
</p>

<p align="center">
  <img alt="Radon" src="https://img.shields.io/badge/Radon-active%20protection-38e1c8">
  <img alt="Dew point" src="https://img.shields.io/badge/Taupunkt-dew--point%20control-3ac6e6">
  <img alt="Patent-pending" src="https://img.shields.io/badge/NeuraCell--X-patent%20pending-6aa9ff">
  <img alt="TUV" src="https://img.shields.io/badge/hardware-T%C3%9CV%20gepr%C3%BCft-4caf50">
</p>

> **When radon rises**, every unit shifts to a gentle fresh-air overpressure (Zuluft, Stufe 1) that slows radon ingress. **When the outside air is too humid to ventilate**, the units pause so no moisture is drawn in. **When conditions are safe again**, normal operation is restored &mdash; automatically, with radon protection always taking priority.

---

## Supported Platforms

| Platform | Integration | Folder | Status |
|----------|-------------|--------|--------|
| **Home Assistant** | MQTT Auto-Discovery + Add-on | `ha-addon/` | ✅ Ready |
| **Apple Home** | Homebridge plugin | `homebridge-plugin/` | ✅ Ready |
| **Apple Home (native)** | Matter Bridge | `matter-bridge/` | ✅ Ready |
| **Google Home** | Homebridge plugin / Matter | `homebridge-plugin/` · `matter-bridge/` | ✅ Ready |
| **Amazon Alexa** | Homebridge plugin / Matter | `homebridge-plugin/` · `matter-bridge/` | ✅ Ready |
| **Node-RED** | Example flow | `examples/node-red/` | ✅ Ready |
| **Loxone** | MQTT Virtual I/O guide | `examples/loxone/` | ✅ Ready |
| **ioBroker** | Native adapter | `iobroker-adapter/` | ✅ Ready |
| **SmartThings** | Matter Bridge | `matter-bridge/` | ✅ Ready |
| **NeuraCell-X®** | Radon + dew-point protection (all platforms) | *built-in* | ✅ Ready |
| **openHAB** | MQTT Binding (generic) | See README | 📖 Guide |
| **KNX / BACnet** | Via MQTT-KNX gateway | See README | 📖 Guide |

---

## Quick Start

```bash
git clone https://github.com/ambientika-eu/ambientika-mqtt-bridge.git
cd ambientika-mqtt-bridge
cp .env.example .env
cp config.example.yaml config.yaml
# Edit .env (or config.yaml) with your Ambientika credentials and MQTT broker settings
docker compose up -d
```

All settings are listed under [Configuration](#configuration).

---

## Architecture
```
Ambientika Device (WiFi)
       |  (HTTPS/WebSocket)
  [Ambientika MQTT Bridge]  ← this project
       |  (MQTT)
  [MQTT Broker]
       |
   ┌───┴────────────────────────────────────┐
   │                                         │
   ▼                                         ▼
[Home Assistant]                    [Matter Bridge]
[Node-RED]                          [Apple Home]
[ioBroker]                          [Google Home]
[Loxone]                            [Amazon Alexa]
[openHAB]                           [SmartThings]
[Homebridge → Apple/Google/Alexa]
```

---

## NeuraCell-X&reg; &mdash; patent-pending radon & dew-point protection

![NeuraCell-X](neuracell-x-logo.png)

**NeuraCell-X&reg;** is the AI Neural Control System built into the bridge. It couples the
Ambientika radon meter and dew-point control (Taupunktsteuerung) with your ventilation units:

- **Radon protection (highest priority):** radon alarm &rarr; all units to Intake (Zuluft / supply air) at fan **Stufe 1** &mdash; a gentle fresh-air overpressure that actively slows radon ingress.
- **Dew-point control:** ventilating would raise indoor humidity &rarr; units switch **off**; conditions favourable again &rarr; ventilation released.
- **Exact restore:** when all protections clear, every unit returns to the exact mode it had before. A unit caught in its 20-minute **timed extraction** (`TimedExpulsion`) returns to the mode that extraction would have returned to (the unit's `last_operating_mode`), since 1.4.37 - not to the extraction itself, which would run again and then leave the unit in the protection mode.
- **Radon first, dew point scoped:** radon protection always covers every unit and overrides the dew-point block. When radon clears while the dew-point block (e.g. limited to the basement units via `dewpoint_block_devices`) is still active, all other units return to their own mode right away. A radon value (`radon_topic`) and an explicit alarm (`radon_alarm_topic`) are combined: either one keeps radon protection on.
- **Robust restore:** the pre-protection modes are saved to `/data/ambientika_neuracell_state.json`, so an add-on restart or update during active protection still returns every unit to its previous mode (a unit that was changed by hand in the meantime keeps that change). Manual commands sent while a unit is protected or offline are applied once protection ends - except a timed extraction (`TimedExpulsion`), which is dropped with a note in the log: it is a 20-minute action, not a mode to return to. Set `radon_alarm_topic: none` if you only use the radon value and want the alarm topic ignored.
- **Ambientika radon meter built in:** a radon meter in MQTT mode 4 publishes `{"mittelwert": …}` on `radon/<meter-id>/state`; the bridge reads it directly (`radon_meter_topic`, default `radon/+/state`) — no Home Assistant automation needed. Its start-up value 0 is ignored, and when the meter goes offline its last value stops counting. With several radon sources (e.g. a second meter on `radon_topic`) the highest current value decides. Radon and dew-point topics accept plain values or JSON (`radon_value_key`, `dewpoint_block_key`) and MQTT wildcards.
- **Ambientika Taupunktsteuerung built in:** the dew-point controller with MQTT firmware publishes its decision as `{"ventilating": true|false, "reason": …}` on `dew-point/<id>/state`; the bridge reads it directly (`dewpoint_controller_topic`, default `dew-point/+/state`, key `dewpoint_controller_key`) — `ventilating: false` blocks the units in `dewpoint_block_devices`, no forwarding to `dewpoint_block_topic` needed (that topic still works for third-party sources, the last message wins; leave it at its default — if it also covers `dew-point/<id>/state`, those messages are read as a block signal instead and the log says so). Its Last Will on `dew-point/<id>/availability/state` and its sensor messages on `dew-point/<id>/sensors` are watched automatically.
- **Radon meter watched:** the meter's Last Will marks it offline at once; a meter that sends nothing (value or availability) for longer than `radon_meter_timeout` (default 30 min, 0 = off) counts as disconnected as well. The meters seen so far are remembered in a retained message (`<prefix>/neuracell/meters`; a meter not heard from for 7 days is no longer watched), so a bridge restart while the meter is already gone still ends in *Radon Meter Connected: off* once the limit runs out — the meter's own `online` is not retained and would otherwise never be missed. A live value counts as a sign of life; a value or `online` the broker delivers from its store (retained, e.g. after a restart) does not, the meter then has to send something within the limit, and a stored value of a meter that counts as lost is not used. The limit pauses while the bridge itself has no broker connection and starts afresh after a reconnect, so a broker outage never marks a meter as disconnected. Each meter is judged on its own and logged on every change; *Radon Meter Connected* is off as soon as one meter is lost.
- **Dew-point controller watched:** a Taupunktsteuerung that hangs stops sending, and its last block would otherwise stay in force unnoticed. The bridge watches it through its availability topic with Last Will (for the Ambientika controller automatically, otherwise `dewpoint_availability_topic`) (payload `online`/`offline`, `true`/`false` or JSON with `state`) or, for controllers without Last Will, a time limit without any message (`dewpoint_signal_timeout`, minutes; not applied while the Last Will reports online); a retained `online` delivered after a restart or reconnect is not taken as alive (and never revives a controller whose Last Will said offline), a live message on the controller's own topics (`dew-point/<id>/state`, `/sensors`) always is; with `dewpoint_source: device` failed cloud reads count like for the units. `dewpoint_lost_action` decides what happens then: `keep` (default, last state stays), `release` (ventilate) or `block` (units off). Radon keeps priority either way. While the controller counts as lost with `release`/`block`, a block value the broker delivers from its store (retained, e.g. after a bridge restart or reconnect) is not applied; once the controller is back (`online` on its availability topic, or a fresh message in time-limit mode) its last block value counts again. A live message is always applied. The time limit pauses while the bridge itself has no broker connection and restarts after a reconnect, so a broker outage never triggers the lost action. Use the controller's exact availability topic, not a wildcard that also matches the block topic.

The live status is published to `ambientika/neuracell/state` and surfaced natively on every platform (in Home Assistant the NeuraCell-X entities follow the bridge's own availability, so they show *unavailable* while the add-on is stopped instead of a stale value):

| Platform | NeuraCell-X&reg; surface |
|---|---|
| **Home Assistant** | Auto-discovered *Radon Protection Active*, *Radon Level*, *Radon Meter Connected*, *Ventilation Blocked (Dew Point)*, *Dew Point Controller Connected*, *Dew Point Indoor / Outdoor* |
| **ioBroker** | `ambientika.0.neuracell.*` states |
| **Apple / Google / Alexa** (Homebridge) | *NeuraCell-X* accessory: Radon Protection + Dew-Point Block occupancy sensors |
| **Matter** (SmartThings, ...) | *NeuraCell-X Radon Protection* contact sensor |
| **Node-RED / Loxone** | `ambientika/neuracell/state` inputs (see the examples) |

Both protections can read their trigger **hardware-free, straight from the Ambientika cloud** &mdash; no
extra sensor wiring, relay or MQTT signal needed. Set `radon_source: "device"` (radon meter) and/or
`dewpoint_source: "device"` (TPS) and give the device's serial number (it appears in the add-on log).

Configure it in the add-on options / `config.yaml`: `radon_source` (`signal` or `device`),
`radon_device_serial`, `radon_device_alarm_field` / `radon_device_alarm_values`, `radon_threshold`,
`radon_protection_fan`, `dewpoint_source` (`signal`, `computed` or `device`), `dewpoint_margin`, and more.

*NeuraCell-X&reg; and PhaseCell-X&reg; are registered trademarks of S&uuml;dwind / Ambientika. Patent pending.*

---

## Integration Guides

### Home Assistant Add-on
See [`ha-addon/README.md`](ha-addon/README.md)

### Apple Home + Google Home + Alexa (Homebridge)
See [`homebridge-plugin/README.md`](homebridge-plugin/README.md)

### Apple Home + Google Home + Alexa + SmartThings (Matter – native, no bridge app needed)
See [`matter-bridge/README.md`](matter-bridge/README.md)

### Home Assistant: ready-made package (evening/night programme, radon priority)
See [`examples/home-assistant-schlafetage/README.md`](examples/home-assistant-schlafetage/README.md)

### Node-RED
See [`examples/node-red/README.md`](examples/node-red/README.md)

### Loxone
See [`examples/loxone/README.md`](examples/loxone/README.md)

### ioBroker
See [`iobroker-adapter/README.md`](iobroker-adapter/README.md)

---

## MQTT Topics

| Topic | Direction | Description |
|-------|-----------|-------------|
| `ambientika/<serial>/state` | Bridge → Broker | Full device state (JSON, retained) |
| `ambientika/<serial>/availability` | Bridge → Broker | `online` / `offline` |
| `ambientika/<serial>/reset_state` | Bridge → Broker | Filter reset: `idle` / `running` / `confirmed` / `acknowledged` / `unconfirmed` |
| `ambientika/<serial>/set/<attribute>` | Broker → Bridge | Set **one** attribute, plain value |
| `ambientika/<serial>/set` | Broker → Bridge | Set **several** attributes at once (JSON object) |
| `ambientika/bridge/availability` | Bridge → Broker | The bridge itself: `online` / `offline` |
| `ambientika/neuracell/state` | Bridge → Broker | NeuraCell-X® radon + dew-point status (JSON) |
| `ambientika/neuracell/meters` | Bridge → Broker | Ambientika radon meters seen so far (JSON, retained; survives a bridge restart) |
| `radon/<id>/state`, `radon/<id>/availability/state` | Meter → Broker | Ambientika radon meter (MQTT mode 4): `{"mittelwert": …}`, Last Will |
| `dew-point/<id>/state`, `dew-point/<id>/sensors`, `dew-point/<id>/availability/state` | Controller → Broker | Ambientika Taupunktsteuerung: `{"ventilating": …, "reason": …}`, sensor values, Last Will |

`<serial>` is the device serial number as reported by the cloud; it is printed in
the log at start-up. `ambientika` is the default prefix (`MQTT_TOPIC_PREFIX`).

### Status Payload Example

```json
{
  "operating_mode": "Smart",
  "operating_mode_raw": "Surveillance",
  "fan_speed": "Medium",
  "humidity_level": "Normal",
  "light_sensor_level": "Medium",
  "temperature": 21,
  "humidity": 52,
  "temperature_avg": 21.3,
  "humidity_avg": 61.8,
  "air_quality": "Good",
  "humidity_alarm": false,
  "filters_status": "Good",
  "filters_status_raw": "Good",
  "filter_ack_since": null,
  "filter_ack_until": null,
  "night_alarm": false,
  "device_role": "SlaveEqualMaster",
  "last_operating_mode": "Smart",
  "zone_index": 1,
  "operating_mode_num": 0,
  "operating_mode_raw_num": 5,
  "last_operating_mode_num": 0,
  "fan_speed_num": 2,
  "humidity_level_num": 1,
  "light_sensor_level_num": 3,
  "air_quality_num": 3,
  "filter_status_num": 0,
  "filter_status_raw_num": 0
}
```

Every categorical value comes with a numeric companion (`*_num`) so long-term
statistics and Grafana work without a translation table of your own.
`filter_ack_since` / `filter_ack_until` carry the maintenance acknowledgement of a
Slave (ISO 8601, see *Filter reset on Master/Slave groups*), `null` without one.

### Commands

Writable attributes and their accepted values:

| Attribute | Values |
|---|---|
| `operating_mode` | `Smart`, `Auto`, `ManualHeatRecovery`, `Night`, `AwayHome`, `Surveillance`, `TimedExpulsion`, `Expulsion`, `Intake`, `MasterSlaveFlow`, `SlaveMasterFlow`, `Off` |
| `fan_speed` | `Low`, `Medium`, `High` |
| `humidity_level` | `Dry`, `Normal`, `Moist` |
| `light_sensor_level` | `NotAvailable`, `Off`, `Low`, `Medium` |
| `reset_filter` | any payload triggers the filter reset |

**One attribute** — the value is the plain payload, not JSON:

```
topic:   ambientika/AMB-2024-001/set/operating_mode
payload: MasterSlaveFlow
```

**Several attributes at once** — a JSON object on the `set` topic, without an
attribute in the topic:

```
topic:   ambientika/AMB-2024-001/set
payload: {"operating_mode": "MasterSlaveFlow", "fan_speed": "High"}
```

The short names `mode`, `fanSpeed`, `humidityLevel` and `lightSensorLevel` are
accepted as well. An invalid value rejects the whole command; an unknown key is
skipped with a warning in the log.

**Commands are coalesced.** Each command has to fill the attributes it does not
set from the device's current status, and the cloud needs a moment to reflect a
change. Commands that arrive for the same device within `COMMAND_COALESCE_MS`
(default `800`) are therefore applied in a single call — so an automation that
sets the operating mode and then the fan speed no longer writes the old mode
back. Set `COMMAND_COALESCE_MS=0` to apply every command on its own, immediately.

> `fan_speed` may be *reported* as `Night` while a unit runs on its night step.
> That value is read-only: it is published as state but rejected on the command
> path, because the API would not accept it back.

---

## Configuration

The bridge reads its configuration from the first of these it finds: the add-on
options (`/data/options.json`, Home Assistant), a `config.yaml` (path in
`CONFIG_YAML`, `/config/config.yaml` in the Docker image - see
[`config.example.yaml`](config.example.yaml)) or environment variables. An
environment variable overrides the same setting from `config.yaml`.

### General

| Variable | `config.yaml` | Default | Description |
|---|---|---|---|
| `AMBIENTIKA_USERNAME` (or `AMBIENTIKA_USER`) | `ambientika.username` | — | Ambientika account e-mail address |
| `AMBIENTIKA_PASSWORD` (or `AMBIENTIKA_PASS`) | `ambientika.password` | — | Ambientika account password |
| `MQTT_HOST` | `mqtt.host` | `localhost` | MQTT broker host |
| `MQTT_PORT` | `mqtt.port` | `1883` | MQTT broker port |
| `MQTT_USERNAME` (or `MQTT_USER`) | `mqtt.username` | *(empty)* | MQTT username (**required** for most brokers) |
| `MQTT_PASSWORD` (or `MQTT_PASS`) | `mqtt.password` | *(empty)* | MQTT password (**required** for most brokers) |
| — | `mqtt.tls` | `false` | TLS to the broker |
| `MQTT_TOPIC_PREFIX` | `bridge.topic_prefix` | `ambientika` | MQTT topic prefix |
| `DISCOVERY_PREFIX` | `bridge.discovery_prefix` | `homeassistant` | Home Assistant discovery prefix |
| — | `bridge.enable_discovery` | `true` | Publish Home Assistant Auto-Discovery |
| `POLL_INTERVAL` | `bridge.poll_interval` | `30` | Device poll interval in seconds |
| `AVAILABILITY_FAILURE_THRESHOLD` | `bridge.availability_failure_threshold` | `3` | Consecutive failed polls before a device is flagged offline |
| `LOG_LEVEL` | `bridge.log_level` | `INFO` | Logging level |
| `COMMAND_COALESCE_MS` | — | `800` | Commands for the same device within this window are applied in one call (`0` = off) |
| `AVERAGE_WINDOW_MIN` | — | `30` | Window of `humidity_avg` / `temperature_avg` in minutes, 1-1440 (add-on option `average_window_min`: 1-240; at least twenty poll intervals either way) |
| `AVERAGE_WINDOW_DEVICES` | `bridge.average_window_devices` | *(empty)* | Own window for single units: `unit=minutes` entries, comma-separated, by serial number or unit name as in the app, 1-240 (e.g. `A1B2C3D4E5F6=15, Schlafzimmer Eltern=60`). The serial number is the safe way; in a name, upper/lower case does not matter and a hyphen, underscore or space count as the same separator (see *Averaged humidity and temperature*) |
| `HUMIDITY_AVG_HYSTERESIS_DEVICES` | `bridge.humidity_avg_hysteresis_devices` | *(empty)* | Hysteresis of `humidity_avg` for single units in percentage points, `unit=points` entries as above, 0-10 (e.g. `A1B2C3D4E5F6=1`); the published value only moves once the mean has left it by at least that much (see *Averaged humidity and temperature*) |
| `SLAVE_FILTER_SOFT_RESET` | — | `0` | Record a bridge-side "serviced" acknowledgement when a Slave's filter counter cannot be cleared remotely (see below) |
| `FILTER_ACK_TTL_DAYS` | — | `90` | How long such an acknowledgement stays valid |
| `FILTER_ACK_PATH` | — | `/data/filter_ack.json` | Where the acknowledgements are stored (needs a persistent volume) |
| `AMBIENTIKA_HOST` | `ambientika.host` | `https://app.ambientika.eu:4521` | Ambientika cloud endpoint (see *Running without our server*) |

### NeuraCell-X: radon protection

In `config.yaml` these live in the `neuracell:` section under the same name as
the add-on option (lower case, e.g. `radon_threshold`); the add-on options are
the same names. Radon alarm: every unit to Intake (supply air) at
`RADON_PROTECTION_FAN` until the value is back below the threshold minus the
hysteresis.

| Variable | Default | Description |
|---|---|---|
| `NEURACELL_ENABLED` | `true` | Radon protection on or off |
| `RADON_SOURCE` | `signal` | `signal`: the radon value and alarm come from the MQTT topics below. `device`: the radon meter's status is read from the Ambientika cloud, no MQTT signal needed |
| `RADON_THRESHOLD` | `300` | Bq/m³ at which radon protection starts (300 is the German reference value). With `RADON_SOURCE=device` also the limit for a numeric alarm field |
| `RADON_HYSTERESIS` | `50` | Bq/m³ the value must fall below the threshold before protection ends |
| `RADON_PROTECTION_FAN` | `Low` | Fan speed during radon protection: `Low`, `Medium` or `High` |
| `RADON_METER_TOPIC` | `radon/+/state` | Native topic of the Ambientika radon meter (MQTT mode 4, `{"mittelwert": …}`), read directly. `off` switches the direct read off (`none` in `config.yaml` / the add-on; as an environment variable `none` counts as unset) |
| `RADON_METER_TIMEOUT` | `30` | Minutes without any message from an Ambientika radon meter before it counts as disconnected (0 = off; the meter reports every 10 minutes, 20 or more is recommended) |
| `RADON_TOPIC` | `ambientika/radon/value` | Topic with a radon value in Bq/m³ from any other source: a plain number or JSON (see `RADON_VALUE_KEY`); MQTT wildcards allowed. With several sources the highest current value decides |
| `RADON_ALARM_TOPIC` | `ambientika/radon/alarm` | Topic with an explicit alarm (`ON`/`OFF`, `true`/`false`). Value and alarm are combined, either one keeps protection on. `off` (`none` in `config.yaml` / the add-on) = ignored, so a stale retained `ON` can never hold protection |
| `RADON_VALUE_KEY` | `mittelwert` | JSON key of the radon value when `RADON_TOPIC` or the meter topic carries JSON |
| `RADON_DEVICE_SERIAL` | *(empty)* | `RADON_SOURCE=device`: serial number of the radon meter (shown in the log). The meter is read only, never exposed as a fan |
| `RADON_DEVICE_ALARM_FIELD` | `air_quality` | `RADON_SOURCE=device`: the status field of the meter that carries its alarm or level. A numeric field is compared with `RADON_THRESHOLD`, a text field with the values below |
| `RADON_DEVICE_ALARM_VALUES` | `Bad,Poor,Very Bad,Alarm,Alert` | `RADON_SOURCE=device`: text values of that field that mean "radon alarm" (comma-separated, upper/lower case does not matter) |

### NeuraCell-X: dew-point control

In `config.yaml` these live in the `dewpoint:` section under the same name as
the add-on option. Ventilating would raise indoor humidity: the units in
`DEWPOINT_BLOCK_DEVICES` switch off until conditions are favourable again.
Radon protection always has priority.

| Variable | Default | Description |
|---|---|---|
| `DEWPOINT_ENABLED` | `true` | Dew-point control on or off |
| `DEWPOINT_SOURCE` | `signal` | `signal`: a block signal from MQTT - the Ambientika Taupunktsteuerung on its own topic (below) or a third-party source on `DEWPOINT_BLOCK_TOPIC`. `computed`: the bridge computes indoor and outdoor dew point from four sensor topics. `device`: the Taupunktsteuerung's status is read from the Ambientika cloud |
| `DEWPOINT_BLOCK_DEVICES` | *(empty)* | Units the block applies to, by name as in the app or serial number, comma-separated (e.g. the basement units). Empty = all units |
| `DEWPOINT_CONTROLLER_TOPIC` | `dew-point/+/state` | Native topic of the Ambientika Taupunktsteuerung (MQTT firmware), read directly; its Last Will on `dew-point/<id>/availability/state` and its sensor messages are watched automatically. `off` switches the direct read off (`none` in `config.yaml` / the add-on) |
| `DEWPOINT_CONTROLLER_KEY` | `ventilating` | JSON key in those messages; `false` means: block ventilation |
| `DEWPOINT_BLOCK_TOPIC` | `ambientika/dewpoint/block` | `signal`: topic of a third-party block signal (`ON`/`true`/`1` = block). The last message wins. Leave it at its default so it does not overlap the controller topic |
| `DEWPOINT_BLOCK_KEY` | *(empty)* | JSON key of the block flag when `DEWPOINT_BLOCK_TOPIC` carries a JSON object |
| `DEWPOINT_AVAILABILITY_TOPIC` | *(empty)* | `signal`: availability topic with Last Will of a third-party controller (`online`/`offline`, `true`/`false` or JSON with `state`). The Ambientika controller needs none |
| `DEWPOINT_SIGNAL_TIMEOUT` | `0` | `signal`: minutes without any message from the controller before it counts as disconnected (0 = off). For controllers without Last Will; not applied while a Last Will reports online |
| `DEWPOINT_LOST_ACTION` | `keep` | While the controller counts as disconnected: `keep` the last block state, `release` ventilation, or `block` the units. Radon keeps priority either way |
| `dewpoint_indoor_temp_topic` *(config.yaml / add-on only)* | `ambientika/dewpoint/indoor_temp` | `computed`: indoor temperature in °C |
| `dewpoint_indoor_humidity_topic` *(config.yaml / add-on only)* | `ambientika/dewpoint/indoor_humidity` | `computed`: indoor relative humidity in % |
| `dewpoint_outdoor_temp_topic` *(config.yaml / add-on only)* | `ambientika/dewpoint/outdoor_temp` | `computed`: outdoor temperature in °C |
| `dewpoint_outdoor_humidity_topic` *(config.yaml / add-on only)* | `ambientika/dewpoint/outdoor_humidity` | `computed`: outdoor relative humidity in % |
| `DEWPOINT_MARGIN` | `1.0` | `computed`: block as soon as the outdoor dew point reaches the indoor dew point minus this margin (°C) - ventilating would then add moisture |
| `DEWPOINT_HYSTERESIS` | `0.5` | `computed`: release only once the outdoor dew point is this much (°C) below that line again |
| `DEWPOINT_DEVICE_SERIAL` | *(empty)* | `DEWPOINT_SOURCE=device`: serial number of the Taupunktsteuerung (shown in the log). It is read only, never exposed as a fan |
| `DEWPOINT_DEVICE_BLOCK_MODES` | `Off` | `DEWPOINT_SOURCE=device`: operating modes of the Taupunktsteuerung that mean "block" (comma-separated) |

> **MQTT credentials are required for most brokers.** The official Home Assistant Mosquitto add-on and most production setups disable anonymous MQTT access. If `MQTT_USER` / `MQTT_PASSWORD` are empty the bridge will fail to connect with `Not authorized`. Create a dedicated MQTT user for the bridge (e.g. via the Mosquitto add-on's `logins` option) and set both variables.

---

## Notes

### Unknown enum values from the cloud API

The cloud API occasionally reports enum values that the pinned `ambientika_py`
release does not know. `ambientika_py` resolves them with plain `Enum[...]`
lookups, so an unknown value raises `KeyError` *inside* `Device.status()` and
aborts the whole poll for that device. The known case is `"fanSpeed": "Night"`,
reported while a unit runs in night mode (see
[#5](https://github.com/ambientika-eu/ambientika-mqtt-bridge/issues/5)):

```
ERROR  Error polling <serial>: 'Night'
```

The bridge registers `FanSpeed.Night` and `FanSpeed.Turbo` at start-up (the API
schema at `https://app.ambientika.eu:4521/swagger/v1/swagger.json` defines
`FanSpeed` as Low, Medium, High, Night, Turbo) and installs a tolerant enum
lookup, so any future unknown value is auto-registered with a warning instead of
breaking the poll. Such compatibility members are **read-only**: they are
published as state but never sent to the API, which would not accept them back.
A command that names one (a Home Assistant scene restoring a snapshotted
`Turbo`, say) is carried out with the nearest sendable value instead, see below;
a name the enum does not know at all is still rejected.

A command that does not name every attribute (e.g. a mode change from the
Home Assistant `select`) fills the missing ones from the current cloud status.
Since 1.4.29 a read-only value found there is never echoed back: the bridge sends
the last value of that attribute the cloud accepted for this unit (seen in a poll
or sent by a command), or - if it never saw one - the nearest sendable value
(Night → Low, Turbo → High, unknown humidity → Normal, unknown dusk level → Off),
and logs what it replaced. An unknown *operating mode* is never guessed; such a
command is skipped with an error. Before 1.4.29 an auto-registered value (internal
number 900+) went straight into `change_mode` and the cloud answered
`HTTP 500 "Value was either too large or too small for an unsigned byte"`.

### Filter reset on Master/Slave groups

The manufacturer resets filters per zone through the **Master** only. Each Slave
keeps its own counter, which nothing clears: a reset addressed to a Slave is
acknowledged with HTTP 200 but never carried out, the app shows only the Master's
filter status, and there is nothing to press at the unit either (the unit's own
filter alarm is active on Masters only). The bridge sends the documented
`device/reset-filter` to the device and to its zone Master, then checks the real
device status and reports what actually happened - for a Slave it says plainly
that its counter stays, instead of promising a change on a later poll.

The only way to clear a Slave's own counter is a reset while the unit is a Master:
set it up in the app as a standalone unit for the moment, reset the filter, then
couple it as a Slave again. For everyday use the maintenance acknowledgement below
is the simpler way.

A counter is only skipped when it is positively `Good`. `Medium` (yellow) is a
real reset case, since filters are usually cleaned before the alarm turns red.

Set `SLAVE_FILTER_SOFT_RESET=1` and mount a persistent `/data` to record a
bridge-side maintenance acknowledgement for such a Slave. `filter_status_num` then
reports the serviced unit as green until `FILTER_ACK_TTL_DAYS` expire, while the
raw device value stays untouched. Both the text and the numeric field come as a
pair - the main field carries the effective value, the raw device value sits next
to it:

| Field | Content |
|---|---|
| `filters_status` | effective (acknowledgement applied) |
| `filters_status_raw` | raw device value |
| `filter_status_num` | effective |
| `filter_status_raw_num` | raw device value |

Warning rules on the worst filter state therefore fire correctly again, without a
serviced Slave hanging on red forever. With the feature off - the default - the
main and raw fields are identical.

The diagnostic sensor *Filter Reset Status* reports `acknowledged` for such a
reset, as opposed to `confirmed` (the counter really cleared) and `unconfirmed`
(neither cleared nor recorded). Since 1.4.32 the acknowledged value is published at
once, not with the next poll, and an acknowledgement that cannot be stored (no
writable `/data`) is reported as a warning and `unconfirmed` instead of
`acknowledged`.

Since 1.4.35 the status sensor keeps saying `acknowledged` for as long as the
acknowledgement is in force - also after a restart or an update (before, every
unit started as `idle`, so nothing showed that one was in force) - and drops
back to `idle` the moment it ends. The state carries the acknowledgement
itself: `filter_ack_since` and `filter_ack_until` (ISO 8601 with offset, `null`
without one), and with `SLAVE_FILTER_SOFT_RESET=1` Home Assistant discovers them
as the diagnostic timestamp sensors *Filter Acknowledged Since* and *Filter
Acknowledgement Valid Until*, so the day the effective status falls back to the
raw value is visible in advance. At start-up the log names every acknowledgement
still in force with its date and the day it runs out.

Since 1.4.30 an acknowledgement ends only when `FILTER_ACK_TTL_DAYS` run out or
when the device itself reports `Good` for at least 10 polls in a row spanning at
least 10 minutes (its counter was cleared after all, e.g. by a reset as a
temporary standalone unit; a rebooting unit or a short cloud
hiccup reporting a default `Good` does not count); both are logged
(`filter acknowledgement for <serial> removed: ...`). A poll with an unrecognised
raw value keeps the acknowledgement and is logged once per new value. Before
1.4.30 a single poll with a raw value that was `Good` or unrecognised removed it
at once and without a log line.

### Mode changes: accepted is not applied

`device/change-mode` returns HTTP 200 once the cloud has accepted the call; it
does not report whether the unit carried it out. Since 1.4.30 the log line reads
`change_mode OK for <serial> (accepted by the cloud)` and the bridge checks the
following polls: `operating mode <mode> confirmed on <serial>` once the unit
reports the sent mode, or a warning if it still reports another mode after
`MODE_VERIFY_WINDOW_S` (180 s). A unit under NeuraCell-X protection is not judged,
and the check never costs a poll - if it ever failed internally the status would
still be published.
For a Slave the warning names the zone Master: a coupled Slave runs with its
Master, so the mode belongs on the Master.

### Operating mode of a Slave

A coupled Slave runs with its zone Master. Its own mode field is not what it is
doing - a Slave can report `Surveillance` and still ventilate in the Master's
reversing rhythm. Like the Ambientika app, which shows only the Master's status for
a zone, the bridge since 1.4.32 publishes the Master's mode for a Slave and keeps
the Slave's own value next to it:

| Field | Content |
|---|---|
| `operating_mode` / `operating_mode_num` | effective mode (the zone Master's for a Slave) |
| `operating_mode_raw` / `operating_mode_raw_num` | the unit's own value |

Masters are read first in each cycle. The unit's own value is shown instead while
NeuraCell-X protection controls the unit, or if its Master has not been read for
more than `max(300 s, 2 x POLL_INTERVAL + 60 s)`. The zone Master is looked up
within the same house, and the roles come from what each unit reports in its own
status (`device_role`: `Master`, `SlaveEqualMaster`, `SlaveOppositeMaster`,
`NotConfigured`), so re-coupling or resetting units in the app is followed
without a restart; a reset unit that still carries its old zone index is not a
Slave. When a Slave's own value differs, the log says so once per change
(`operating mode of <serial>: the unit reports ..., shown as ...`). Selecting a
mode on a Slave still sends the command to that unit; the published mode follows
the Master, so set modes on the Master.

### Plausibility of temperature and humidity

Values outside the sensor range are never published (humidity 1-100 %,
temperature -40 to 85 °C). A single impossible drop such as 6 % humidity between
55 and 70 % is held back too: a humidity below 20 % is published only if dry air
(below 25 %) was already measured among the last three readings (a held-back one
counts) or published within the last hour (at least six poll intervals).
Genuinely dry winter air is therefore confirmed by its own next dry reading - only
the first dry reading after a long humid stretch is delayed by one reading - while
rises, shower peaks and the normal reversing rhythm are never held back. The cloud
hands out the unit's last status packet, so with a poll interval shorter than the
unit's upload rhythm the same packet is read several times; a repeat within
`DUPLICATE_PACKET_MAX_S` (120 s) does not count as a new reading and cannot
confirm a held-back value. Meanwhile the last
plausible value is published, for at most ten minutes; after that the value is
published as unknown, so a failed sensor never looks live. The log notes once when
values are held back and once when a plausible value returns, at most once an hour
for a flapping sensor.

### Averaged humidity and temperature

The unit reverses its airflow about every half minute, so its sensor sees room air
and incoming air in turn and a single humidity reading jumps by up to about 20
percentage points - unusable for a dashboard tile or a threshold automation, which
read a random phase. Since 1.4.33 every unit also publishes `humidity_avg` and
`temperature_avg` (Home Assistant: *Humidity (averaged)*, *Temperature (averaged)*,
`state_class: measurement`): the time-weighted mean of the published readings over
`AVERAGE_WINDOW_MIN` minutes (default 30, at least twenty poll intervals). Each reading
counts for the time since the previous one, at most two poll intervals, so a cached
packet read several times, an extra publish between polls or a missed poll do not
skew it. Held-back readings (see above) do not count. The value is `null` until at
least two readings cover two minutes (or the whole window, if shorter) and, like the
raw value, once no plausible reading has counted for ten minutes. `humidity` and `temperature` stay
unchanged. The mean covers the whole reversing cycle, room air and incoming air: a
steady value for display, history and thresholds, not a pure room-air value.
What to expect: the cloud hands out about one packet per half minute, i.e. about
one reading per reversal, and which phase of the cycle it catches drifts slowly. The
mean is therefore much calmer than the raw value but can still wander by several
points over an hour. Simulated with a 30 s upload rhythm (+-10 %) and half-periods
of 30-45 s, the raw value jumps by ~35 points, the 30-min mean wanders by up to ~10
over hours and the 60-min mean by ~5-10; with an irregular upload rhythm (25-35 s)
the mean stays within 1-2 points. Keep `POLL_INTERVAL` at 30 s or shorter; at 60 s
and above the mean needs many more reads and scatters more.
In a real installation (five units, three days, 30-min window) the mean varied by
about 2.5 points within an hour against about 9 for the raw value, with daily means
equal to the raw ones within 0.1 points; the unit reports humidity in steps of 2-3
points, so a longer window gains little there and mainly adds delay.

Since 1.4.34 single units can have their own window: `AVERAGE_WINDOW_DEVICES`
(add-on option and `config.yaml` key `average_window_devices`) takes `unit=minutes`
entries separated by commas, semicolons or line breaks, by the unit's serial
number or its name as in the app, 1-240 minutes each (plain number with a decimal
point; a trailing `min` is fine; in `config.yaml` quote the value). Example:
`A1B2C3D4E5F6=15, Schlafzimmer Eltern=60` - the bathroom follows a shower quickly,
the bedroom is smoothed more, all other units keep `AVERAGE_WINDOW_MIN`. The
serial number is the safe way: it is unique, it stands in the log (`Device: <name>
(serial: …)`) and in the app, and it cannot be misspelt the way a name can. A name
has to match the app's spelling; upper/lower case does not matter, and a hyphen,
underscore or space count as the same separator (since 1.4.35 - `Gäste-Bad` finds
a unit called `Gäste Bad`, `Gästebad` does not); anything else does matter. The floor of twenty poll intervals applies per unit as well.
A shorter window follows a change faster but shows more of the residual swing
described above (simulated: the 15-min mean swings about twice as much within an
hour as the 30-min mean, the 60-min mean about half as much).
The log shows the windows in force once the units are found (`Averaging per unit:
...`); an entry that matches no unit or cannot be read is named there, with the
list of units and their serial numbers, and ignored. For a name containing a
comma or semicolon use the serial number. Names are matched at start-up.
A real bathroom confirmed the trade-off: with 10 instead of 15 minutes the mean
reached its shower peak about 4 minutes earlier, but the swing in quiet night hours
grew by about a third and the mean began to wander slowly with no real change.

Since 1.4.38 single units can also get a hysteresis on `humidity_avg`:
`HUMIDITY_AVG_HYSTERESIS_DEVICES` (add-on option and `config.yaml` key
`humidity_avg_hysteresis_devices`) takes `unit=points` entries with the same syntax
and name matching as `AVERAGE_WINDOW_DEVICES`, 0-10 percentage points (decimal
point; a trailing `%`, `%rH` or `%rF` is fine). The published `humidity_avg` of such
a unit only moves once the mean has left it by at least that many points, e.g.
`A1B2C3D4E5F6=1`. On the unit's coarse raster a short window can sit on one phase of
the drifting sampling for a while, so in quiet hours the mean wanders by one to two
points with nothing changing in the room; the hysteresis hides that, while a shower
(tens of points) passes at once. The price: the value can sit up to the hysteresis
away from the true mean, and slow real changes arrive in steps. `null` (unknown)
passes at once and the first mean afterwards is published as it is. Raw values,
`temperature_avg` and the averaging itself are unchanged; 0 or not listed = none.
A median instead of the mean was simulated and not taken: on the coarse raster it
flips by a full step whenever one phase fills about half the window. The log shows
the setting once the units are found (`Humidity (averaged) hysteresis per unit: ...`).

### Availability debounce

A single failed poll is usually a transient cloud hiccup - the API sporadically
answers `HTTP 404 Status packet not found!` for a device that is perfectly
reachable. The bridge therefore only publishes `offline` after
`AVAILABILITY_FAILURE_THRESHOLD` consecutive failures (default `3`), which stops
entities from flickering to *unavailable* and back for one poll interval. Set it
to `1` for the previous immediate-offline behaviour.

### `ambientika_py` dependency

The bridge installs [`ambientika_py`](https://github.com/wingertge/ambientika-py) from PyPI (`ambientika_py>=0.0.6`). Release 0.0.6 contains the `LightSensorLevel` enum the bridge requires; the earlier 0.0.5 did not, so previous builds pinned the library to an upstream Git commit as a temporary workaround (see [#3](https://github.com/ambientika-eu/ambientika-mqtt-bridge/issues/3) and [wingertge/ambientika-py#8](https://github.com/wingertge/ambientika-py/issues/8)). That is no longer necessary.

---

## License

MIT License – © Ambientika / SUEDWIND

---

## Links

- 🌐 [ambientika.eu](https://www.ambientika.eu)
- 📦 [GitHub Repository](https://github.com/ambientika-eu/ambientika-mqtt-bridge)

---

## Running without our server

This bridge reads its data through the Ambientika server. There is a second, standalone version that replaces that server with a local service speaking the same protocol, so no measurement and no command leaves the house:

https://github.com/ambientika-eu/ambientika-local-standalone

Initial commissioning still runs once through the Ambientika app; after that the installation stays local. The standalone version is new and not yet proven on every firmware revision, so it ships in observation mode: it reads, decodes and publishes, but writes nothing to the units until you switch that off.
