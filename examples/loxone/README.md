<p align="center">
  <img src="neuracell-x-logo.png" alt="NeuraCell-X (patented)" width="420">
</p>

> **Powered by NeuraCell-X&reg;** &mdash; the patented AI Neural Control System (active radon protection + dew-point ventilation control).

# Ambientika – Loxone Integration Guide

This guide explains how to integrate Ambientika ventilation units into a **Loxone Miniserver** using the [Ambientika MQTT Bridge](../../README.md) and the Loxone MQTT extension.

## Architecture

```
Ambientika Cloud API
        |
        v
Ambientika MQTT Bridge  (runs on Raspberry Pi / NAS / Home Server)
        |
        v
   MQTT Broker  (e.g. Mosquitto)
        |
        v
  Loxone Miniserver  (MQTT Virtual Inputs / Outputs)
```

## Prerequisites

1. **Ambientika MQTT Bridge** running - see [main README](../../README.md)
2. **Loxone Miniserver Gen 2** (or Miniserver Go) with firmware >= 14.0
3. **MQTT broker** reachable from both the bridge host and the Loxone Miniserver (same LAN)
4. **Loxone Config** software installed on Windows PC

---

## Step 1: Enable MQTT in Loxone Config

1. Open **Loxone Config** and connect to your Miniserver
2. In the left tree: **Miniserver > Extensions > MQTT**
3. If not visible, right-click Extensions > Add Extension > MQTT
4. Configure the MQTT extension:
   - **Broker Address**: IP of your MQTT broker (e.g. 192.168.1.100)
   - **Port**: 1883
   - **Username / Password**: if your broker requires authentication
5. Click **Save** and sync to Miniserver

---

## Step 2: Create Virtual Inputs (Sensor Data)

Virtual Inputs receive data published by the bridge.

### 2.1 - Current Operating Mode

| Field | Value |
|-------|-------|
| Name | Ambientika Mode |
| Type | Virtual Input (Text) |
| MQTT Topic | ambientika/YOUR_SERIAL/operating_mode |

### 2.2 - Fan Speed

| Field | Value |
|-------|-------|
| Name | Ambientika Fan Speed |
| Type | Virtual Input (Text) |
| MQTT Topic | ambientika/YOUR_SERIAL/fan_speed |

### 2.3 - Humidity

| Field | Value |
|-------|-------|
| Name | Ambientika Humidity |
| Type | Virtual Input (Number) |
| MQTT Topic | ambientika/YOUR_SERIAL/humidity |
| Unit | % |

### 2.4 - Supply Air Temperature

| Field | Value |
|-------|-------|
| Name | Ambientika Supply Temp |
| Type | Virtual Input (Number) |
| MQTT Topic | ambientika/YOUR_SERIAL/supply_air_temperature |
| Unit | C |

### 2.5 - Extract Air Temperature

| Field | Value |
|-------|-------|
| Name | Ambientika Extract Temp |
| Type | Virtual Input (Number) |
| MQTT Topic | ambientika/YOUR_SERIAL/extract_air_temperature |
| Unit | C |

### 2.6 - Air Quality (CO2 / VOC Index)

| Field | Value |
|-------|-------|
| Name | Ambientika Air Quality |
| Type | Virtual Input (Number) |
| MQTT Topic | ambientika/YOUR_SERIAL/air_quality |

### 2.7 - Filter Alarm

| Field | Value |
|-------|-------|
| Name | Ambientika Filter Alarm |
| Type | Virtual Input (Digital) |
| MQTT Topic | ambientika/YOUR_SERIAL/filter_alarm |
| ON Payload | true |
| OFF Payload | false |


### 2.8 - NeuraCell-X&reg; Radon Protection (patented)

NeuraCell-X&reg; publishes its status as JSON to `ambientika/neuracell/state`.
Create MQTT subscriptions in the Loxone MQTT extension and extract the JSON fields:

| Field | Loxone Virtual Input | JSON field | Notes |
|-------|----------------------|-----------|-------|
| Radon Protection Active | Digital | `radon_protection` | ON = units in fresh-air overpressure (Zuluft, Stufe 1) |
| Dew-Point Block | Digital | `dewpoint_block` | ON = ventilation paused (air too humid) |
| Radon Level | Number (Bq/m3) | `radon` | current radon concentration |
| Dew Point Indoor | Number (C) | `indoor_dew_point` | |
| Dew Point Outdoor | Number (C) | `outdoor_dew_point` | |
| Override Active | Digital | `override_active` | any NeuraCell-X protection active |

In Loxone Config, add an **MQTT Subscription** with topic `ambientika/neuracell/state`
and map each JSON key above to a Virtual Input. Use *Radon Protection Active* to drive a
warning indicator + push notification, and *Dew-Point Block* to show ventilation status.

*NeuraCell-X&reg; is a registered trademark of S&uuml;dwind / Ambientika. Patent pending.*

---

## Step 3: Create Virtual Outputs (Control Commands)

Virtual Outputs send commands from Loxone to the bridge.

### 3.1 - Set Operating Mode

| Field | Value |
|-------|-------|
| Name | Ambientika Set Mode |
| Type | Virtual Output (Text) |
| MQTT Topic | ambientika/YOUR_SERIAL/set/operating_mode |

Connect a **State Machine** or **Text Sender** block to this output.
Valid values: Auto, ManualLow, ManualMedium, ManualHigh, Night, Standby, Away, Boost

### 3.2 - Set Fan Speed

| Field | Value |
|-------|-------|
| Name | Ambientika Set Fan Speed |
| Type | Virtual Output (Text) |
| MQTT Topic | ambientika/YOUR_SERIAL/set/fan_speed |

Valid values: Low, Medium, High

### 3.3 - Set Humidity Setpoint

| Field | Value |
|-------|-------|
| Name | Ambientika Set Humidity |
| Type | Virtual Output (Number) |
| MQTT Topic | ambientika/YOUR_SERIAL/set/humidity_setpoint |

Valid range: 40 - 90 (percent)

---

## Step 4: Logic Examples in Loxone Config

### Night Mode via Presence Detector

    [Presence OFF longer than 30 min] -> [Text Sender: "Night"] -> [Ambientika Set Mode]
    [Presence ON]                     -> [Text Sender: "Auto"]  -> [Ambientika Set Mode]

### Boost on High Humidity (e.g. bathroom sensor)

    [Ambientika Humidity] -> [Threshold: > 75] -> [Text Sender: "ManualHigh"] -> [Ambientika Set Mode]
    [Ambientika Humidity] -> [Threshold: < 60] -> [Text Sender: "Auto"]       -> [Ambientika Set Mode]

### Filter Alarm Push Notification

    [Ambientika Filter Alarm] = ON -> [Push Notification: "Ambientika filter maintenance required"]

### Time-based Schedule

    07:00 -> [Text Sender: "Auto"]  -> [Ambientika Set Mode]
    22:00 -> [Text Sender: "Night"] -> [Ambientika Set Mode]

---

## Step 5: Loxone App / Touch Display

After syncing to the Miniserver, all Virtual Inputs and Outputs appear in the **Loxone App** (iOS, Android) and on **Loxone Touch** wall panels:

- Show current humidity, temperature, air quality as gauges or value displays
- Control mode via a state button (Auto / Night / Off)
- Show filter alarm as a warning indicator with push notification

---

## Replacing YOUR_SERIAL

In all MQTT topics above, replace YOUR_SERIAL with your device serial number.
You can find the serial number:
- In the **Ambientika App** (app.ambientika.eu) under device settings
- In the bridge log output on first startup: Discovered device: SERIAL
- On the device label

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| No data in Virtual Input | Check bridge is running: docker logs ambientika-bridge |
| MQTT connection error in Loxone | Verify broker IP, port, and firewall rules |
| Commands not executed | Check topic spelling, ensure bridge is subscribed |
| Authentication error | Verify MQTT username/password in Loxone MQTT extension config |

---

## Full MQTT Topic Reference

See the [main README](../../README.md#mqtt-topics) for the complete topic list.
