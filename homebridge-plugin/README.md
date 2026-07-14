<p align="center">
  <img src="neuracell-x-logo.png" alt="NeuraCell-X® — AI Neural Control System (patented)" width="440">
</p>

> **Powered by NeuraCell-X® — the patented AI Neural Control System.**
> Active radon protection and intelligent dew-point (Taupunkt) ventilation control.

# homebridge-ambientika

**Apple Home, Google Home & Amazon Alexa integration for Ambientika ventilation units**

This [Homebridge](https://homebridge.io) plugin connects Ambientika ventilation units to Apple HomeKit (and via Homebridge also to Google Home and Amazon Alexa).

Works via the [Ambientika MQTT Bridge](../README.md) – the bridge handles cloud communication, this plugin exposes the devices to HomeKit.

## Features

- Control ventilation mode (Auto, Manual, Night, Standby)
- Adjust fan speed
- Monitor humidity, supply air temperature, air quality
- Filter alarm notification in Home app
- Works with Siri: "Hey Siri, set ventilation to night mode"
- Works with Google Home and Amazon Alexa via Homebridge

## Prerequisites

1. **Homebridge** installed (see [homebridge.io](https://homebridge.io))
2. **Ambientika MQTT Bridge** running and publishing to an MQTT broker
3. **MQTT broker** (e.g. Mosquitto) accessible from your Homebridge host

## Installation

### Via Homebridge UI (recommended)

1. Open the Homebridge UI
2. Go to **Plugins**
3. Search for **homebridge-ambientika**
4. Click **Install**

### Via npm

```bash
npm install -g homebridge-ambientika
```

## Configuration

Add to your Homebridge `config.json`:

```json
{
  "platforms": [
    {
      "platform": "AmbientikaPlugin",
      "name": "Ambientika",
      "mqttHost": "localhost",
      "mqttPort": 1883,
      "mqttUsername": "",
      "mqttPassword": "",
      "topicPrefix": "ambientika"
    }
  ]
}
```

> **MQTT credentials are required for most brokers.** Most production setups (including the official Home Assistant Mosquitto add-on) disable anonymous MQTT access. If `mqttUsername` / `mqttPassword` are empty the plugin will not be able to subscribe to the bridge topics. Use the same MQTT user that the Ambientika MQTT Bridge uses to publish, or create a dedicated read-only user for the plugin.

### Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `mqttHost` | string | `localhost` | MQTT broker hostname or IP |
| `mqttPort` | number | `1883` | MQTT broker port |
| `mqttUsername` | string | `` | MQTT username (**required** for most brokers) |
| `mqttPassword` | string | `` | MQTT password (**required** for most brokers) |
| `topicPrefix` | string | `ambientika` | MQTT topic prefix (must match bridge config) |

## HomeKit Accessories

Each Ambientika device appears as an **Air Purifier** accessory in the Home app with:

- **Active / Inactive** – turns ventilation on (Auto) or off (Standby)
- **Auto / Manual mode** – switches between sensor-driven and manual control
- **Fan speed** – Low / Medium / High (shown as rotation speed %)
- **Humidity sensor** – current room humidity
- **Temperature sensor** – supply air temperature
- **Air quality sensor** – air quality index (maps to HomeKit levels)
- **Filter maintenance** – shows alert when filter needs service

## Siri Commands

- "Hey Siri, turn on ventilation"
- "Hey Siri, set ventilation to automatic"
- "Hey Siri, what is the humidity in the bedroom?"

## Architecture

```
Ambientika Cloud
      |
      v
Ambientika MQTT Bridge  (Python, polls cloud API)
      |
      v
MQTT Broker  (Mosquitto)
      |
      v
homebridge-ambientika  (this plugin, subscribes to MQTT)
      |
      v
Homebridge  ->  Apple HomeKit / Google Home / Amazon Alexa
```

## License

MIT

---

## NeuraCell-X® — patented radon & dew-point protection

![NeuraCell-X®](neuracell-x-logo.png)

This component surfaces the live status of **NeuraCell-X®**, the *patented* AI Neural
Control System built into the Ambientika MQTT Bridge:

- **Radon protection (highest priority):** when the radon meter alarms, every unit
  switches to supply / Intake mode at fan **Stufe 1** — a gentle fresh-air overpressure
  that actively slows radon ingress.
- **Dew-point control (Taupunktsteuerung):** when ventilating would raise indoor
  humidity, the units switch **off**; when conditions are favourable again, ventilation
  is released.
- Radon **overrides** the dew-point block, and every device is restored to the **exact**
  mode it had before once all protections clear.

**Exposed as a `NeuraCell-X` accessory** with two occupancy sensors: *Radon Protection* (detected = active) and *Dew-Point Block* (detected = ventilation blocked).

*NeuraCell-X® and PhaseCell-X® are registered trademarks of Südwind / Ambientika. Patent pending.*
