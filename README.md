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
# Edit .env with your Ambientika credentials and MQTT broker settings
docker compose up -d
```

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
- **Exact restore:** when all protections clear, every unit returns to the exact mode it had before.

The live status is published to `ambientika/neuracell/state` and surfaced natively on every platform:

| Platform | NeuraCell-X&reg; surface |
|---|---|
| **Home Assistant** | Auto-discovered *Radon Protection Active*, *Radon Level*, *Ventilation Blocked (Dew Point)*, *Dew Point Indoor / Outdoor* |
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
| `ambientika/<deviceId>/status` | Bridge → Broker | Full device state (JSON) |
| `ambientika/<deviceId>/set` | Broker → Bridge | Set mode/fanSpeed (JSON) |
| `ambientika/<deviceId>/availability` | Bridge → Broker | `online` / `offline` |
| `ambientika/neuracell/state` | Bridge → Broker | NeuraCell-X® radon + dew-point status (JSON) |

### Status Payload Example

```json
{
  "deviceId": "DEV001",
  "serial": "AMB-2024-001",
  "name": "Bedroom",
  "mode": "HRV",
  "fanSpeed": 75,
  "temperature": 21.5,
  "humidity": 52,
  "airQuality": 850,
  "filterAlarm": false,
  "online": true,
  "rssi": -58
}
```

### Command Payload Example

```json
{ "mode": "NIGHT" }
{ "fanSpeed": 50 }
{ "mode": "HRV", "fanSpeed": 75 }
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AMBIENTIKA_EMAIL` | — | Ambientika account email |
| `AMBIENTIKA_PASSWORD` | — | Ambientika account password |
| `MQTT_BROKER` | `localhost` | MQTT broker host |
| `MQTT_PORT` | `1883` | MQTT broker port |
| `MQTT_USER` | *(empty)* | MQTT username (**required** for most brokers) |
| `MQTT_PASSWORD` | *(empty)* | MQTT password (**required** for most brokers) |
| `MQTT_PREFIX` | `ambientika` | MQTT topic prefix |
| `POLL_INTERVAL` | `30` | Device poll interval in seconds |
| `AVAILABILITY_FAILURE_THRESHOLD` | `3` | Consecutive failed polls before a device is flagged offline |
| `HA_DISCOVERY` | `true` | Enable Home Assistant Auto-Discovery |
| `LOG_LEVEL` | `INFO` | Logging level |
| `SLAVE_FILTER_SOFT_RESET` | `0` | Record a bridge-side "serviced" acknowledgement when a Slave's filter counter cannot be cleared remotely (see below) |
| `FILTER_ACK_TTL_DAYS` | `90` | How long such an acknowledgement stays valid |
| `FILTER_ACK_PATH` | `/data/filter_ack.json` | Where the acknowledgements are stored (needs a persistent volume) |

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

The bridge registers `FanSpeed.Night` at start-up and installs a tolerant enum
lookup, so any future unknown value is auto-registered with a warning instead of
breaking the poll. Such compatibility members are **read-only**: they are
published as state but rejected on the command path, because the API would not
accept them back.

### Filter reset on Master/Slave groups

The filter reset is applied by the **Master** of a coupled zone. Each Slave keeps
its own counter, which the cloud cannot reach: a reset addressed to a Slave is
acknowledged with HTTP 200 but never carried out. The bridge sends the documented
`device/reset-filter` to the device and to its zone Master, then checks the real
device status and reports what actually happened - for a Slave it says plainly
that the reset has to be done at the unit itself, instead of promising a change on
a later poll.

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
(neither cleared nor recorded).

Truly zeroing a Slave's counter is only possible at the device: configure the unit
in the app temporarily as a standalone device, reset the filter, then set it up as
a Slave again.

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
