# Ambientika MQTT Bridge – Home Assistant Add-on

This folder contains the Home Assistant OS (HAOS) Add-on for the Ambientika MQTT Bridge.

With this add-on, you can install the Ambientika MQTT Bridge directly from the Home Assistant Add-on Store with a single click – no Docker, no terminal, no Raspberry Pi setup required.

## Installation

### Method 1: Add Repository to Home Assistant (Recommended)

1. In Home Assistant, go to **Settings > Add-ons > Add-on Store**
2. Click the **three-dot menu** (top right) > **Repositories**
3. Add this URL:
   ```
      https://github.com/ambientika-eu/ambientika-mqtt-bridge
   ```
4. Click **Add**, then close the dialog
5. Search for **"Ambientika MQTT Bridge"** in the store
6. Click **Install**

### Method 2: Manual Installation

1. Copy the `ha-addon` folder contents to your HA config directory:
   ```
   /config/addons/ambientika_mqtt_bridge/
   ```
2. Restart Home Assistant
3. The add-on appears under **Settings > Add-ons > Local Add-ons**

---

## Configuration

After installation, configure the add-on via the **Configuration** tab:

| Option | Description | Default |
|--------|-------------|---------|
| `ambientika_username` | Your Ambientika account email | *(required)* |
| `ambientika_password` | Your Ambientika account password | *(required)* |
| `mqtt_host` | MQTT broker hostname | `core-mosquitto` |
| `mqtt_port` | MQTT broker port | `1883` |
| `mqtt_username` | MQTT username (**required** for most brokers) | `` |
| `mqtt_password` | MQTT password (**required** for most brokers) | `` |
| `mqtt_topic_prefix` | MQTT topic prefix | `ambientika` |
| `poll_interval` | Polling interval in seconds | `30` |
| `log_level` | Log verbosity | `INFO` |
| `neuracell_enabled` | Enable NeuraCell-X&reg; radon protection + dew-point control | `true` |
| `radon_source` | Where the radon alarm comes from: `signal` (MQTT) or `device` (read the radon meter directly from the Ambientika cloud &mdash; no hardware) | `signal` |
| `radon_device_serial` | Radon meter serial number (only for `radon_source: device`; see the add-on log) | `` |
| `radon_device_alarm_field` | Which status field of the radon meter carries the alarm (`device` source) | `air_quality` |
| `radon_device_alarm_values` | Values of that field that mean "radon alarm" (comma-separated) | `Bad,Poor,Very Bad,Alarm,Alert` |
| `radon_threshold` | Radon alarm threshold in Bq/m³ (numeric source) | `300` |
| `radon_protection_fan` | Fan level during radon protection (`Low` / `Medium` / `High`) | `Low` |
| `dewpoint_source` | Dew-point trigger: `signal` (MQTT), `computed` (from sensors) or `device` (read the TPS from the cloud) | `signal` |

> **MQTT credentials are required for most brokers.** The official Home Assistant Mosquitto add-on and most production setups disable anonymous MQTT access. If `mqtt_username` / `mqtt_password` are empty the bridge cannot connect (`Not authorized`). Create a dedicated MQTT user for the bridge (e.g. via the Mosquitto add-on's `logins` option) and set both fields. Only leave them empty if you have explicitly configured your broker to allow anonymous access.
>
> **Note:** If you use the official **Mosquitto broker** add-on, the default `core-mosquitto` hostname works out of the box.

---

## NeuraCell-X&reg; — radon protection & dew-point control

**NeuraCell-X&reg;** (patent pending) couples the Ambientika radon meter and dew-point control (TPS) with your ventilation units:

- **Radon protection (highest priority):** on a radon alarm, **all** units go to **Intake (supply air / Zuluft) at fan Stufe 1 (Low)** — a gentle fresh-air overpressure that slows radon ingress.
- **Dew-point control:** when ventilating would raise indoor humidity, the units switch **off**; when conditions are favourable again, ventilation is released.
- **Radon has priority:** while a radon alarm is active it overrides the dew-point block. When all protections clear, every unit returns to the exact mode it had before.

### Hardware-free source (recommended)

If your radon meter and/or TPS hang in the same Ambientika account, the bridge can read them **directly from the Ambientika cloud** — no relay, no ESP, no extra sensors:

```yaml
neuracell_enabled: true
radon_source: "device"
radon_device_serial: "<radon meter serial>"       # see the add-on log: Device: <name> (serial: <serial>)
radon_device_alarm_field: "air_quality"           # a numeric field would use radon_threshold instead
radon_device_alarm_values: "Bad,Poor,Very Bad,Alarm,Alert"
```

On the first run the log shows `Radon source device: <name> (serial ...)` and, on each change, `NeuraCell-X: radon meter ... -> radon protection ON/OFF`. If your meter reports its alarm as a different value, adjust `radon_device_alarm_values` (the actual value appears in the log). If it reports a numeric radon level instead, point `radon_device_alarm_field` at that numeric field and it will use `radon_threshold` / `radon_hysteresis`.

The default `radon_source: "signal"` keeps the classic MQTT input, so nothing changes for existing setups. The TPS / dew-point side has the same hardware-free option (`dewpoint_source: "device"`) — see [`examples/home-assistant-tps`](../examples/home-assistant-tps/README.md).

---

## Home Assistant Auto-Discovery

The bridge publishes MQTT Auto-Discovery messages, so your Ambientika devices appear automatically in Home Assistant under:

**Settings > Devices & Services > MQTT > Devices**

The following entities are created per device:

### Sensors
- Humidity (%)
- Supply Air Temperature (°C)
- Extract Air Temperature (°C)
- Outdoor Temperature (°C)
- Air Quality Index
- Fan Speed (numeric)
- Heat Recovery Efficiency (%)
- Power Consumption (W)

### Binary Sensors
- Filter Alarm
- Defrost Active

### Select / Control Entities
- Operating Mode (Auto, ManualLow, ManualMedium, ManualHigh, Night, Standby, Away, Boost)
- Fan Speed (Low, Medium, High)
- Humidity Setpoint (40–90%)

---

## Example Automations

### Boost when humidity spikes

```yaml
automation:
  - alias: "Ambientika Humidity Boost"
    trigger:
      - platform: numeric_state
        entity_id: sensor.ambientika_humidity
        above: 75
    action:
      - service: select.select_option
        target:
          entity_id: select.ambientika_operating_mode
        data:
          option: ManualHigh
```

### Night mode on schedule

```yaml
automation:
  - alias: "Ambientika Night Mode"
    trigger:
      - platform: time
        at: "22:00:00"
    action:
      - service: select.select_option
        target:
          entity_id: select.ambientika_operating_mode
        data:
          option: Night
  - alias: "Ambientika Day Mode"
    trigger:
      - platform: time
        at: "07:00:00"
    action:
      - service: select.select_option
        target:
          entity_id: select.ambientika_operating_mode
        data:
          option: Auto
```

### Filter alarm notification

```yaml
automation:
  - alias: "Ambientika Filter Alarm"
    trigger:
      - platform: state
        entity_id: binary_sensor.ambientika_filter_alarm
        to: "on"
    action:
      - service: notify.mobile_app
        data:
          title: "Ambientika Filter Service"
          message: "Please replace or clean the ventilation filter."
```

---

## MQTT Topics

See the [main README](../README.md#mqtt-topics) for the complete topic list.

---

## Support

- GitHub Issues: https://github.com/ambientika-eu/ambientika-mqtt-bridge/issues
- Ambientika website: https://www.ambientika.eu
