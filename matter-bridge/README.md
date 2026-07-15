<p align="center">
  <img src="neuracell-x-logo.png" alt="NeuraCell-X® — AI Neural Control System (patent-pending)" width="440">
</p>

> **Powered by NeuraCell-X® — the patent-pending AI Neural Control System.**
> Active radon protection and intelligent dew-point (Taupunkt) ventilation control.

# Ambientika Matter Bridge

Connects **Ambientika ventilation units** to the **Matter** smart home standard,
enabling native control via **Apple Home**, **Google Home**, **Amazon Alexa**,
**Samsung SmartThings** and any other Matter-compatible ecosystem.

> This bridge runs as a local Python process on your network.
> No cloud dependency, full privacy.

## Architecture

```
Ambientika Device (WiFi)
       |
  [Ambientika MQTT Bridge]   <-- converts device REST/WebSocket to MQTT
       |  (MQTT topics: ambientika/+/status)
       v
  [Mosquitto MQTT Broker]
       |
  [Ambientika Matter Bridge] <-- THIS component
       |  (commissions virtual Matter nodes)
       v
  [python-matter-server]     <-- Matter controller (by Home Assistant Labs)
       |  (Matter protocol over Thread/WiFi/Ethernet)
       v
  Apple Home / Google Home / Alexa / SmartThings
```

## What Gets Exposed to Matter

Each Ambientika unit is exposed as a **Matter Air Purifier** device with:

| Cluster | Attribute | Ambientika Source |
|---------|-----------|-------------------|
| On/Off | OnOff | mode != OFF |
| Fan Control | PercentSetting | fanSpeed (0-100) |
| Relative Humidity | MeasuredValue | humidity |
| Temperature | MeasuredValue | temperature |
| Air Quality | AirQuality enum | airQuality (CO2 ppm) |

Commands from Apple Home / Google Home are forwarded back via MQTT.

## Prerequisites

- Python 3.11+ (or Docker)
- Running [Ambientika MQTT Bridge](https://github.com/ambientika-eu/ambientika-mqtt-bridge)
- MQTT broker (e.g. Mosquitto)
- [python-matter-server](https://github.com/home-assistant-libs/python-matter-server) running

## Quick Start with Docker Compose

This is the recommended method. The compose stack starts MQTT broker,
matter-server and the Ambientika bridge together.

```bash
# Clone the repo
git clone https://github.com/ambientika-eu/ambientika-mqtt-bridge.git
cd ambientika-mqtt-bridge/matter-bridge

# (Optional) Create .env for credentials
cat > .env <<EOF
MQTT_USER=myuser
MQTT_PASSWORD=secret
MQTT_PREFIX=ambientika
LOG_LEVEL=INFO
EOF

# Start everything
docker compose up -d

# Watch logs
docker compose logs -f ambientika-matter-bridge
```

## Manual Installation (without Docker)

```bash
cd matter-bridge
pip install -r requirements.txt

# Start python-matter-server separately:
# https://github.com/home-assistant-libs/python-matter-server#installation

# Run the bridge
export MQTT_BROKER=localhost
export MQTT_PORT=1883
export MATTER_SERVER_HOST=localhost
export MATTER_SERVER_PORT=5580
python ambientika_matter_bridge.py
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MQTT_BROKER` | `localhost` | MQTT broker hostname/IP |
| `MQTT_PORT` | `1883` | MQTT broker port |
| `MQTT_USER` | *(empty)* | MQTT username (optional) |
| `MQTT_PASSWORD` | *(empty)* | MQTT password (optional) |
| `MQTT_PREFIX` | `ambientika` | MQTT topic prefix |
| `MATTER_SERVER_HOST` | `localhost` | python-matter-server host |
| `MATTER_SERVER_PORT` | `5580` | python-matter-server port |
| `LOG_LEVEL` | `INFO` | Logging level (DEBUG/INFO/WARNING) |

## Commissioning into Apple Home

When the bridge starts for the first time and discovers Ambientika devices:

1. Each unit is automatically commissioned as a virtual Matter node
2. Open the **Home** app on iPhone/iPad
3. Tap **+** â **Add Accessory** â **More Options**
4. The Ambientika unit appears as **Air Purifier**
5. Add it to a room â done!

You can then control fan speed, check humidity/temperature and set automations
directly in Apple Home.

## Google Home

1. Open **Google Home** app
2. Tap **+** â **Set up device** â **Matter**
3. The Ambientika unit will be discovered automatically
4. Assign to a room

## Amazon Alexa

1. Open **Amazon Alexa** app
2. Go to **Devices** â **+** â **Add Device** â **Other**
3. Enable Matter pairing mode
4. Follow the on-screen instructions

## Troubleshooting

**Device not discovered:**
- Check `docker compose logs ambientika-matter-bridge` for errors
- Verify the Ambientika MQTT Bridge is publishing to `ambientika/+/status`
- Ensure python-matter-server is running: `curl http://localhost:5580/`

**Apple Home shows "Not Responding":**
- Restart the bridge: `docker compose restart ambientika-matter-bridge`
- Check that your phone/hub is on the same network segment

**Commands not working:**
- Enable DEBUG logging: set `LOG_LEVEL=DEBUG` in `.env`
- Check the WebSocket connection to matter-server in logs

## Related

- [Main Repository](https://github.com/ambientika-eu/ambientika-mqtt-bridge)
- [Home Assistant Add-on](../ha-addon/)
- [Homebridge Plugin](../homebridge-plugin/) (alternative for Apple Home)
- [Node-RED Flow](../examples/node-red/)
- [Loxone Guide](../examples/loxone/)

## License

MIT License â see [LICENSE](../LICENSE)

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

**Exposed to Matter** as a contact sensor **NeuraCell-X Radon Protection** (contact *open* = a protection is active), with radon level and dew-point attributes.

*NeuraCell-X® and PhaseCell-X® are registered trademarks of Südwind / Ambientika. Patent pending.*
