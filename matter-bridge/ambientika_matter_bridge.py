#!/usr/bin/env python3
"""
Ambientika Matter Bridge  —  powered by NeuraCell-X(R)
Exposes Ambientika ventilation units as Matter-compatible Air Quality sensors
and Fan devices via the python-matter-server SDK.

NeuraCell-X(R) — the patent-pending AI Neural Control System — provides active radon
protection and dew-point ventilation control. This bridge surfaces the
NeuraCell-X status (radon protection active, radon level, dew-point block,
indoor/outdoor dew point) as Matter sensors so any Matter ecosystem can see it.

Compatible with: Apple Home, Google Home, Amazon Alexa, SmartThings
Requirements: python-matter-server >= 5.0, paho-mqtt

NeuraCell-X(R) and PhaseCell-X(R) are registered trademarks. Patent pending.
GitHub: https://github.com/ambientika-eu/ambientika-mqtt-bridge
"""

import asyncio
import json
import logging
import os
import signal
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import paho.mqtt.client as mqtt

# ---------------------------------------------------------------------------
# Configuration (override via environment variables)
# ---------------------------------------------------------------------------

MQTT_BROKER   = os.getenv("MQTT_BROKER",   "localhost")
MQTT_PORT     = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER     = os.getenv("MQTT_USER",     "")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")
MQTT_PREFIX   = os.getenv("MQTT_PREFIX",   "ambientika")

MATTER_SERVER_HOST = os.getenv("MATTER_SERVER_HOST", "localhost")
MATTER_SERVER_PORT = int(os.getenv("MATTER_SERVER_PORT", "5580"))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("ambientika.matter-bridge")

# ---------------------------------------------------------------------------
# Device state model
# ---------------------------------------------------------------------------

@dataclass
class AmbientikaDevice:
    """Represents one Ambientika ventilation unit."""
    device_id: str
    serial: str = ""
    name:   str = ""
    # Operational state
    mode:        str   = "OFF"   # OFF | HRV | SUPPLY | EXHAUST | NIGHT | AUTO
    fan_speed:   int   = 0       # 0-100 %
    humidity:    float = 0.0     # % RH
    temperature: float = 0.0     # °C
    air_quality: int   = 0       # 0-500 AQI-like (CO2 ppm equivalent)
    filter_alarm: bool = False
    online:      bool  = False
    # Matter node ID assigned by python-matter-server
    matter_node_id: Optional[int] = None


@dataclass
class NeuraCellXState:
    """NeuraCell-X(R) protection status published by the main MQTT bridge."""
    radon_protection: bool = False       # radon alarm -> devices in supply/Intake
    radon: Optional[float] = None        # Bq/m3
    radon_threshold: Optional[int] = None
    dewpoint_block: bool = False         # ventilating would add moisture -> Off
    indoor_dew_point: Optional[float] = None   # °C
    outdoor_dew_point: Optional[float] = None  # °C
    override_active: bool = False        # any protection currently overriding
    matter_node_id: Optional[int] = None


# Global registries
devices: Dict[str, AmbientikaDevice] = {}
neuracell = NeuraCellXState()

# ---------------------------------------------------------------------------
# MQTT helpers
# ---------------------------------------------------------------------------

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        logger.info("MQTT connected")
        topic = f"{MQTT_PREFIX}/+/status"
        client.subscribe(topic)
        logger.info(f"Subscribed to {topic}")
        # NeuraCell-X(R) status published by the main bridge.
        ncx_topic = f"{MQTT_PREFIX}/neuracell/state"
        client.subscribe(ncx_topic)
        logger.info(f"Subscribed to {ncx_topic}  (NeuraCell-X radon + dew-point status)")
    else:
        logger.error(f"MQTT connect failed, rc={rc}")


def on_message(client, userdata, msg):
    """Handle incoming MQTT messages from the Ambientika MQTT bridge."""
    topic = msg.topic  # e.g. ambientika/DEV001/status  or  ambientika/neuracell/state
    try:
        payload = json.loads(msg.payload.decode())
    except json.JSONDecodeError:
        logger.warning(f"Invalid JSON on {topic}")
        return

    parts = topic.split("/")
    if len(parts) < 3:
        return

    # ---- NeuraCell-X(R) status ----
    if parts[1] == "neuracell" and parts[2] == "state":
        handle_neuracell(payload)
        return

    device_id = parts[1]

    if device_id not in devices:
        devices[device_id] = AmbientikaDevice(device_id=device_id)
        logger.info(f"New device discovered: {device_id}")
        # Commission the new device asynchronously
        asyncio.get_event_loop().call_soon_threadsafe(
            lambda: asyncio.ensure_future(commission_device(devices[device_id]))
        )

    dev = devices[device_id]
    dev.online = True

    # Map Ambientika MQTT payload fields
    if "mode"        in payload: dev.mode        = str(payload["mode"]).upper()
    if "fanSpeed"    in payload: dev.fan_speed   = int(payload["fanSpeed"])
    if "humidity"    in payload: dev.humidity    = float(payload["humidity"])
    if "temperature" in payload: dev.temperature = float(payload["temperature"])
    if "airQuality"  in payload: dev.air_quality = int(payload["airQuality"])
    if "filterAlarm" in payload: dev.filter_alarm = bool(payload["filterAlarm"])
    if "serial"      in payload: dev.serial       = str(payload["serial"])
    if "name"        in payload: dev.name         = str(payload["name"])

    logger.debug(f"{device_id}: mode={dev.mode} fan={dev.fan_speed}% hum={dev.humidity}% temp={dev.temperature}C")

    # Push updated attributes to Matter node
    if dev.matter_node_id is not None:
        asyncio.get_event_loop().call_soon_threadsafe(
            lambda: asyncio.ensure_future(update_matter_node(dev))
        )


# ---------------------------------------------------------------------------
# NeuraCell-X(R) status handling
# ---------------------------------------------------------------------------

def handle_neuracell(payload: dict):
    """Update the NeuraCell-X state from the bridge and reflect it into Matter."""
    def _num(v):
        return None if v is None else float(v)

    if "radon_protection" in payload: neuracell.radon_protection = bool(payload["radon_protection"])
    if "radon"            in payload: neuracell.radon            = _num(payload["radon"])
    if "radon_threshold"  in payload: neuracell.radon_threshold  = payload["radon_threshold"]
    if "dewpoint_block"   in payload: neuracell.dewpoint_block   = bool(payload["dewpoint_block"])
    if "indoor_dew_point" in payload: neuracell.indoor_dew_point = _num(payload["indoor_dew_point"])
    if "outdoor_dew_point" in payload: neuracell.outdoor_dew_point = _num(payload["outdoor_dew_point"])
    if "override_active"  in payload: neuracell.override_active  = bool(payload["override_active"])

    logger.info(
        "NeuraCell-X: radon_protection=%s radon=%s dewpoint_block=%s override=%s",
        neuracell.radon_protection, neuracell.radon,
        neuracell.dewpoint_block, neuracell.override_active,
    )

    # Commission the NeuraCell-X virtual sensor once, then push updates.
    if neuracell.matter_node_id is None:
        try:
            asyncio.get_event_loop().call_soon_threadsafe(
                lambda: asyncio.ensure_future(commission_neuracell())
            )
        except RuntimeError:
            pass
    else:
        try:
            asyncio.get_event_loop().call_soon_threadsafe(
                lambda: asyncio.ensure_future(update_neuracell_node())
            )
        except RuntimeError:
            pass


# ---------------------------------------------------------------------------
# Matter commissioning & attribute updates
# ---------------------------------------------------------------------------

async def commission_device(dev: AmbientikaDevice):
    """
    Commission a new virtual Matter device via python-matter-server REST API.
    The matter-server exposes a WebSocket API; here we use a simplified
    commissioning stub – replace with actual SDK calls for your setup.
    """
    import aiohttp
    url = f"http://{MATTER_SERVER_HOST}:{MATTER_SERVER_PORT}/commission"
    payload = {
        "device_type": "air_purifier",
        "vendor_id": 0xFFF1,   # Test Vendor
        "product_id": 0x8001,
        "node_label": dev.name or f"Ambientika {dev.device_id}",
        "serial_number": dev.serial or dev.device_id,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    dev.matter_node_id = data.get("node_id")
                    logger.info(f"Commissioned {dev.device_id} as Matter node {dev.matter_node_id}")
                else:
                    logger.error(f"Commission failed for {dev.device_id}: HTTP {resp.status}")
    except Exception as exc:
        logger.error(f"Commission error for {dev.device_id}: {exc}")


async def commission_neuracell():
    """Commission the NeuraCell-X(R) radon-protection status as a Matter sensor node."""
    import aiohttp
    url = f"http://{MATTER_SERVER_HOST}:{MATTER_SERVER_PORT}/commission"
    payload = {
        "device_type": "contact_sensor",   # ON = radon protection active
        "vendor_id": 0xFFF1,
        "product_id": 0x8002,
        "node_label": "NeuraCell-X Radon Protection",
        "serial_number": "neuracell-x",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    neuracell.matter_node_id = data.get("node_id")
                    logger.info(f"Commissioned NeuraCell-X as Matter node {neuracell.matter_node_id}")
                    await update_neuracell_node()
                else:
                    logger.error(f"NeuraCell-X commission failed: HTTP {resp.status}")
    except Exception as exc:
        logger.error(f"NeuraCell-X commission error: {exc}")


async def update_matter_node(dev: AmbientikaDevice):
    """Push current Ambientika state to the corresponding Matter node attributes."""
    import aiohttp
    if dev.matter_node_id is None:
        return

    # Map fan_speed (0-100) -> Matter FanControl.PercentSetting (0-100)
    # Map mode -> Matter OnOff cluster
    is_on = dev.mode not in ("OFF", "STANDBY")

    attributes = {
        "node_id": dev.matter_node_id,
        "on_off": is_on,
        "fan_percent": dev.fan_speed,
        "relative_humidity": int(dev.humidity * 100),  # Matter uses 0-10000
        "temperature": int(dev.temperature * 100),       # Matter uses 1/100 °C
        "air_quality": _map_air_quality(dev.air_quality),
    }

    url = f"http://{MATTER_SERVER_HOST}:{MATTER_SERVER_PORT}/set_attributes"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=attributes, timeout=5) as resp:
                if resp.status != 200:
                    logger.warning(f"set_attributes HTTP {resp.status} for node {dev.matter_node_id}")
    except Exception as exc:
        logger.debug(f"set_attributes error: {exc}")


async def update_neuracell_node():
    """Push NeuraCell-X(R) status to its Matter sensor node."""
    import aiohttp
    if neuracell.matter_node_id is None:
        return
    attributes = {
        "node_id": neuracell.matter_node_id,
        # Contact "open" (True) when any radon/dew-point protection is active.
        "contact": bool(neuracell.override_active),
        "radon_protection": bool(neuracell.radon_protection),
        "dewpoint_block": bool(neuracell.dewpoint_block),
        "radon": neuracell.radon,
        "indoor_dew_point": neuracell.indoor_dew_point,
        "outdoor_dew_point": neuracell.outdoor_dew_point,
    }
    url = f"http://{MATTER_SERVER_HOST}:{MATTER_SERVER_PORT}/set_attributes"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=attributes, timeout=5) as resp:
                if resp.status != 200:
                    logger.warning(f"NeuraCell-X set_attributes HTTP {resp.status}")
    except Exception as exc:
        logger.debug(f"NeuraCell-X set_attributes error: {exc}")


def _map_air_quality(aqi: int) -> int:
    """Map Ambientika air quality (ppm CO2 equivalent) to Matter AirQuality enum."""
    # Matter AirQuality: 0=Unknown 1=Good 2=Fair 3=Moderate 4=Poor 5=VeryPoor 6=Hazardous
    if aqi <= 0:    return 0
    if aqi <= 800:  return 1  # Good
    if aqi <= 1000: return 2  # Fair
    if aqi <= 1200: return 3  # Moderate
    if aqi <= 1500: return 4  # Poor
    if aqi <= 2000: return 5  # Very Poor
    return 6                  # Hazardous


# ---------------------------------------------------------------------------
# Command forwarding: Matter -> Ambientika
# ---------------------------------------------------------------------------

async def handle_matter_command(node_id: int, command: str, params: dict):
    """
    Receive commands from Matter ecosystem (e.g. "Set fan to 75%")
    and forward them to the Ambientika device via MQTT.
    """
    # Find device by Matter node_id
    dev = next((d for d in devices.values() if d.matter_node_id == node_id), None)
    if dev is None:
        logger.warning(f"No device mapped to Matter node {node_id}")
        return

    topic = f"{MQTT_PREFIX}/{dev.device_id}/set"
    payload: Dict[str, Any] = {}

    if command == "on_off":
        payload["mode"] = "HRV" if params.get("on") else "OFF"
    elif command == "fan_speed":
        payload["fanSpeed"] = int(params.get("percent", 0))
    elif command == "set_mode":
        mode_map = {"hrv": "HRV", "supply": "SUPPLY", "exhaust": "EXHAUST",
                    "night": "NIGHT", "auto": "AUTO", "off": "OFF"}
        payload["mode"] = mode_map.get(params.get("mode", "").lower(), "HRV")

    if payload:
        mqtt_client.publish(topic, json.dumps(payload))
        logger.info(f"Sent to {topic}: {payload}")


# ---------------------------------------------------------------------------
# Matter server command listener (WebSocket)
# ---------------------------------------------------------------------------

async def listen_matter_commands():
    """
    Listen for inbound commands from python-matter-server via WebSocket.
    The matter-server emits events when a Matter controller sends commands.
    """
    import aiohttp
    ws_url = f"ws://{MATTER_SERVER_HOST}:{MATTER_SERVER_PORT}/ws"
    logger.info(f"Connecting to matter-server WebSocket at {ws_url}")

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(ws_url) as ws:
                    logger.info("Matter WebSocket connected")
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                                await handle_matter_command(
                                    data.get("node_id"),
                                    data.get("command"),
                                    data.get("params", {})
                                )
                            except Exception as exc:
                                logger.error(f"WS message error: {exc}")
                        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                            break
        except Exception as exc:
            logger.warning(f"Matter WS disconnected: {exc}. Retry in 5s...")
            await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

mqtt_client: mqtt.Client = None


async def main():
    global mqtt_client

    logger.info("=== Ambientika Matter Bridge starting  —  powered by NeuraCell-X(R) (patent-pending) ===")
    logger.info(f"MQTT broker : {MQTT_BROKER}:{MQTT_PORT}")
    logger.info(f"Matter server: {MATTER_SERVER_HOST}:{MATTER_SERVER_PORT}")

    # Set up MQTT client
    mqtt_client = mqtt.Client(client_id="ambientika-matter-bridge")
    if MQTT_USER:
        mqtt_client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message

    mqtt_client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    mqtt_client.loop_start()

    # Start Matter command listener
    asyncio.ensure_future(listen_matter_commands())

    # Graceful shutdown
    loop = asyncio.get_running_loop()
    stop = loop.create_future()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set_result, None)

    logger.info("Matter Bridge running. Press Ctrl+C to stop.")
    await stop

    logger.info("Shutting down...")
    mqtt_client.loop_stop()
    mqtt_client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
