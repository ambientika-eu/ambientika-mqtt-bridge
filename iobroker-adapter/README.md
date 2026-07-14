<p align="center">
  <img src="neuracell-x-logo.png" alt="NeuraCell-X® — AI Neural Control System (patent-pending)" width="440">
</p>

> **Powered by NeuraCell-X® — the patent-pending AI Neural Control System.**
> Active radon protection and intelligent dew-point (Taupunkt) ventilation control.

# ioBroker Adapter: Ambientika

ioBroker adapter for **Ambientika** ventilation units, connecting them via MQTT
to the ioBroker smart home platform.

> **ioBroker** is the most popular open-source smart home platform in Germany
> and Austria (>200,000 installations).

## Features

- Automatic device discovery via MQTT
- Real-time state updates: mode, fan speed, temperature, humidity, air quality
- Filter alarm notification
- Bidirectional control: set mode and fan speed from ioBroker
- Works with all ioBroker visualizations (VIS, VIS-2, Lovelace, etc.)
- Compatible with ioBroker Alexa, Google Home, Apple HomeKit adapters

## Architecture

```
Ambientika Device
       |
  [Ambientika MQTT Bridge]
       |  MQTT: ambientika/<id>/status
       v
  [MQTT Broker (Mosquitto)]
       |
  [ioBroker ambientika Adapter]  <-- this adapter
       |
  ioBroker object tree:
  ambientika.0
  ├── <deviceId>
  │   ├── mode          (string, writable)
  │   ├── fanSpeed      (number 0-100, writable)
  │   ├── temperature   (number °C)
  │   ├── humidity      (number %)
  │   ├── airQuality    (number ppm)
  │   ├── filterAlarm   (boolean)
  │   ├── online        (boolean)
  │   └── rssi          (number dBm)
```

## Prerequisites

1. ioBroker installed and running
2. [Ambientika MQTT Bridge](https://github.com/ambientika-eu/ambientika-mqtt-bridge) running
3. MQTT broker accessible from ioBroker (e.g. `ioBroker.mqtt` adapter)

## Installation

### Method 1: From GitHub (manual)

```bash
# Navigate to your ioBroker installation
cd /opt/iobroker

# Install adapter
npm install github:ambientika-eu/ambientika-mqtt-bridge#main --prefix node_modules/iobroker.ambientika

# Or clone and install locally
cd /opt/iobroker/node_modules
git clone https://github.com/ambientika-eu/ambientika-mqtt-bridge.git
cp -r ambientika-mqtt-bridge/iobroker-adapter ./iobroker.ambientika
cd iobroker.ambientika
npm install

# Add to ioBroker
iobroker add ambientika
```

### Method 2: ioBroker Admin UI (when published)

1. Open ioBroker Admin
2. Go to **Adapter** tab
3. Search for `ambientika`
4. Click **+** to install
5. Create an instance

## Configuration

After installation, open the adapter instance settings:

| Setting | Default | Description |
|---------|---------|-------------|
| MQTT Host | `localhost` | Hostname/IP of your MQTT broker |
| MQTT Port | `1883` | MQTT broker port |
| MQTT User | *(empty)* | Username (if authentication enabled) |
| MQTT Password | *(empty)* | Password |
| MQTT Prefix | `ambientika` | Topic prefix (must match MQTT bridge setting) |
| TLS | off | Enable for MQTT over TLS (port 8883) |

## States Reference

### Read + Write

| State | Type | Values | Description |
|-------|------|--------|-------------|
| `mode` | string | OFF, HRV, SUPPLY, EXHAUST, NIGHT, AUTO | Operating mode |
| `fanSpeed` | number | 0–100 | Fan speed in % |

### Read Only

| State | Type | Unit | Description |
|-------|------|------|-------------|
| `temperature` | number | °C | Indoor temperature |
| `humidity` | number | % | Relative humidity |
| `airQuality` | number | ppm | CO₂ equivalent |
| `filterAlarm` | boolean | — | Filter change required |
| `online` | boolean | — | Device reachability |
| `rssi` | number | dBm | WiFi signal strength |

## Automation Examples

### Blockly: Auto Night Mode

```
On time 22:00
  → Set ambientika.0.<deviceId>.mode = "NIGHT"
On time 06:00
  → Set ambientika.0.<deviceId>.mode = "HRV"
```

### JavaScript Rule: High Humidity Alert

```javascript
on({id: "ambientika.0.DEV001.humidity", change: "gt"}, (obj) => {
    if (obj.state.val > 70) {
        // Set to exhaust mode to remove humidity
        setState("ambientika.0.DEV001.mode", "EXHAUST");
        // Send notification
        sendTo("telegram.0", "Ambientika: High humidity " + obj.state.val + "% - switching to exhaust mode");
    }
});
```

### Use with Apple HomeKit (ioBroker)

Install the `ioBroker.yahka` adapter to expose Ambientika devices to Apple Home:

1. Install `ioBroker.yahka`
2. Map `ambientika.0.<id>.fanSpeed` → HomeKit Fan Speed
3. Map `ambientika.0.<id>.mode` (OFF/HRV) → HomeKit Fan On/Off

## Visualization

Use **ioBroker VIS-2** or **ioBroker Lovelace** to build a dashboard.
Example widgets:
- Slider for `fanSpeed`
- Dropdown for `mode`
- Gauge for `temperature` and `humidity`
- LED indicator for `filterAlarm`

## Related

- [Main Repository](https://github.com/ambientika-eu/ambientika-mqtt-bridge)
- [Home Assistant Add-on](../ha-addon/)
- [Matter Bridge](../matter-bridge/) – Apple Home, Google Home, Alexa
- [Homebridge Plugin](../homebridge-plugin/)
- [Node-RED Flow](../examples/node-red/)

## License

MIT License

---

## NeuraCell-X® — patent-pending radon & dew-point protection

![NeuraCell-X®](neuracell-x-logo.png)

This component surfaces the live status of **NeuraCell-X®**, the *patent-pending* AI Neural
Control System built into the Ambientika MQTT Bridge:

- **Radon protection (highest priority):** when the radon meter alarms, every unit
  switches to supply / Intake mode at fan **Stufe 1** — a gentle fresh-air overpressure
  that actively slows radon ingress.
- **Dew-point control (Taupunktsteuerung):** when ventilating would raise indoor
  humidity, the units switch **off**; when conditions are favourable again, ventilation
  is released.
- Radon **overrides** the dew-point block, and every device is restored to the **exact**
  mode it had before once all protections clear.

**Exposed under the `ambientika.0.neuracell` channel:** `radon_protection`, `radon`, `radon_threshold`, `dewpoint_block`, `indoor_dew_point`, `outdoor_dew_point`, `override_active`. All read-only.

*NeuraCell-X® and PhaseCell-X® are registered trademarks of Südwind / Ambientika. Patent pending.*
