<p align="center">
  <img src="neuracell-x-logo.png" alt="NeuraCell-X (patented)" width="420">
</p>

> **Powered by NeuraCell-X&reg;** &mdash; the patented AI Neural Control System (active radon protection + dew-point ventilation control).

# Ambientika – Node-RED Flow

This folder contains an importable Node-RED flow for controlling Ambientika ventilation units via the [Ambientika MQTT Bridge](../../README.md).

## Prerequisites

1. **Ambientika MQTT Bridge** is running and connected to your MQTT broker  
   → See [main README](../../README.md) for setup instructions
2. **Node-RED** installed (standalone, as Home Assistant add-on, or on Raspberry Pi)
3. **MQTT broker** running (e.g., Mosquitto on localhost:1883)

## Import the Flow

1. Open Node-RED in your browser (default: http://localhost:1880)
2. Click the **≡ menu** (top right) → **Import**
3. Click **"select a file to import"** and choose `ambientika-flow.json`  
   – or paste the JSON content directly
4. Click **Import**
5. Click **Deploy**

## What's Included

The flow contains 4 sections:

### 1. Status Monitoring
- Subscribes to `ambientika/+/state` (all devices)
- Parses the full device state (humidity, temperature, fan speed, mode, alarms)
- Shows a human-readable summary in the debug panel
- Stores state in flow context for use by other nodes

### 2. Device Control
Manual trigger buttons (inject nodes) for common commands:

| Button | Action |
|--------|--------|
| Set Mode: Auto | Automatic mode (sensor-driven) |
| Set Mode: Manual | Manual high-speed ventilation |
| Set Mode: Night | Quiet night mode |
| Set Mode: Off | Standby |
| Fan Speed: Low / Medium / High | Direct fan control |
| Humidity: 60% / 70% | Set humidity setpoint |

### 3. Automation Examples
- **Auto Boost on High Humidity**: Automatically switches to ManualHigh when humidity > 75%, returns to Auto when < 60%
- **Night Mode Schedule**: Switches to Night mode at 22:00, back to Auto at 07:00 (cron-based)

### 4. Filter Alarm Notification
- Watches the `ambientika/+/filter_alarm` topic
- Triggers a notification payload when filter maintenance is required
- Connect the output to a Telegram / Pushover / Email node to receive alerts

## MQTT Topics

| Topic | Direction | Description |
|-------|-----------|-------------|
| `ambientika/{serial}/state` | Subscribe | Full device state (JSON) |
| `ambientika/{serial}/filter_alarm` | Subscribe | Filter alarm status |
| `ambientika/{serial}/set/operating_mode` | Publish | Set mode (Auto, ManualHigh, Night, Standby, …) |
| `ambientika/{serial}/set/fan_speed` | Publish | Set fan speed (Low, Medium, High) |
| `ambientika/{serial}/set/humidity_setpoint` | Publish | Set humidity setpoint (40–90) |

## Configuration

1. **MQTT Broker**: Double-click the `Local MQTT Broker` configuration node and update:
   - Server (default: `localhost`)
   - Port (default: `1883`)
   - Username / Password (if authentication is enabled)

2. **Device Serial**: The flow auto-detects the serial number from the first MQTT message received.  
   Alternatively, hardcode it in any function node: replace `'YOUR_SERIAL_HERE'` with your device serial.

## Adding Push Notifications

To receive filter alarm notifications on your phone:

1. Install the **node-red-contrib-telegrambot** or **node-red-node-pushover** palette
2. Connect the output of the `Build Filter Notification` node to your notification node
3. Configure your bot token / API key

## Screenshot

```
[Inject: Set Mode Auto]  ──┐
[Inject: Set Mode Manual] ─┤
[Inject: Set Mode Night]  ─┤─→ [Build Mode Command] ─→ [MQTT out]
[Inject: Set Mode Off]    ─┘

[MQTT in: state] ─→ [Parse State] ─→ [Debug: Summary]
                                   └→ [Store in Context]

[MQTT in: state] ─→ [Auto Boost Logic] ─→ [MQTT out: Auto Command]

[MQTT in: filter_alarm] ─→ [Filter Alarm?] ─→ [Build Notification] ─→ [Debug]
```



## NeuraCell-X&reg; (radon + dew point)

![NeuraCell-X](neuracell-x-logo.png)

The flow includes a **NeuraCell-X** section that subscribes to `ambientika/neuracell/state`
and exposes the patented radon + dew-point protection status:

- `NeuraCell-X Status` (MQTT in) &rarr; `NeuraCell-X Logic` (function)
- Output 1 &rarr; debug status summary (radon protection, radon level, dew-point block, dew points)
- Output 2 &rarr; a ready-to-use **alert payload** when radon protection engages or ventilation is paused

Connect output 2 to a Telegram / Pushover / e-mail node to be notified the instant radon
protection activates. Fields available in `msg.summary`: `radon_protection`, `radon`,
`dewpoint_block`, `indoor_dew_point`, `outdoor_dew_point`, `override_active`.

| Topic | Direction | Description |
|-------|-----------|-------------|
| `ambientika/neuracell/state` | Subscribe | NeuraCell-X&reg; radon + dew-point status (JSON) |

*NeuraCell-X&reg; is a registered trademark of S&uuml;dwind / Ambientika. Patent pending.*

## License

MIT – see [LICENSE](../../LICENSE) (or root repository)
