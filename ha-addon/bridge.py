#!/usr/bin/env python3
"""
Ambientika MQTT Bridge

Connects the Ambientika Cloud API to any MQTT broker.
Supports Home Assistant Auto-Discovery as well as ioBroker, openHAB,
Loxone and Node-RED (any MQTT-capable system).

Built on top of the community library 'ambientika_py' by wingertge.

========================================================================
NeuraCell-X(R) intelligent control
========================================================================
Two coupled protections with a strict priority order:

  1. RADON PROTECTION (highest priority)
     Radon alarm  ->  all devices to INTAKE (Zuluft / supply air) at fan
     LOW (Stufe 1). Creates a gentle fresh-air overpressure that slows
     radon ingress.

  2. DEW-POINT CONTROL (Taupunktsteuerung)
     "Not ideal to ventilate" (ventilating would raise indoor humidity)
     ->  all devices OFF, so no moist air is drawn in.
     "Ideal conditions"  ->  ventilation released, devices restored.

Priority: radon overrides dew point. While a radon alarm is active the
dew-point block is ignored (radon protection needs the fans running in
supply mode). When all protections clear, every device is returned to
the exact mode it had BEFORE any protection kicked in.

Both signals are consumed from MQTT, so this works with the Ambientika
radon meter and dew-point control as well as with third-party sensors
(RadonEye, AirQ, any temperature/humidity sensors).
"""

import asyncio
import json
import logging
import math
import os
import signal
import sys
import time
from typing import Any, Optional

import paho.mqtt.client as mqtt
import yaml
import aiohttp

try:
    from ambientika_py import (
        authenticate,
        Ambientika,
        Device,
        OperatingMode,
        FanSpeed,
        HumidityLevel,
        LightSensorLevel,
    )
    from returns.result import Success, Failure
except ImportError as exc:
    print(
        "ERROR: required dependency missing. "
        "Run: pip install -r requirements.txt  ({})".format(exc)
    )
    sys.exit(1)

log = logging.getLogger("ambientika_bridge")

# Version shown in the startup banner. Read from the environment so this file
# stays byte-identical across all copies/repos (single source of truth); each
# Home Assistant add-on injects its own config.yaml version via BRIDGE_VERSION
# at build time (see Dockerfile). Unset -> the banner omits the version.
_BRIDGE_VERSION = os.environ.get("BRIDGE_VERSION", "").strip()

# Seconds to wait after a filter-reset call before re-reading the status to
# verify the counter actually changed (the cloud acknowledges the call either
# way). Overridable in tests.
FILTER_RESET_VERIFY_DELAY = 12.0

# Filter reset (device + zone Master).
# The official cloud API documents exactly one filter reset:
# GET /Device/reset-filter?deviceSerialNumber=... ("Sends the reset filter
# command") - the very call the bridge already made. change-mode carries no
# reset field and reset-device only clears the device Role, so no other reset
# exists. The command is fire-and-forget (bare HTTP 200). Per the Advanced+ RS485
# protocol the filter reset is applied by the MASTER of a coupled group, so a
# command sent to a Slave is acknowledged but not carried out. On a reset press
# we therefore send the documented reset-filter to the target device AND to the
# Master of its zone, and after each we re-read the real status; the first that
# clears the counter wins. Safety: only the documented reset-filter GET is ever
# sent - never change-mode, reset-device or DELETE - so nothing but the filter
# alarm can ever be touched.
# The cloud reset is fire-and-forget (decompiled): it is written to the device's
# live socket and returns HTTP 200 without any device ack - exactly like the app,
# which shows success without checking. Whether the counter then clears is a
# device/app-side matter the bridge cannot force. So we send once, quietly, and
# read the status once (below) only to update the hidden reset-status sensor.

# Re-authenticate proactively at this interval, and immediately if the cloud
# starts answering 401 (JWT expired) - otherwise every device would stay offline
# until the add-on is restarted. Re-login refreshes the token in the shared api
# object, which every device call reads, so nothing has to be rebuilt.
REAUTH_INTERVAL = float(os.environ.get("REAUTH_INTERVAL", "21600") or 21600)

# Persisted set of serials whose filter reset is still unconfirmed, so an add-on
# restart/update resumes the retry instead of losing it.
PENDING_RESET_FILE = os.environ.get("PENDING_RESET_FILE", "/data/ambientika_pending_resets.json")

# ---------------------------------------------------------------------------
# ambientika_py enum compatibility  (issue #5)
# ---------------------------------------------------------------------------
# The Ambientika cloud API returns enum values that the pinned ambientika_py
# release does not know yet. ambientika_py resolves them with plain Enum[...]
# lookups, so an unknown value raises KeyError *inside* Device.status() and
# aborts the whole poll for that device.
#
# Known case: while a unit runs in night mode the API reports
# "fanSpeed": "Night", which is missing from FanSpeed (Low/Medium/High). The
# resulting KeyError surfaces in the log as
#     ERROR  Error polling <serial>: 'Night'
# and the unit keeps its last retained MQTT values - in Home Assistant it then
# looks live while it is in fact no longer being updated.
#
# We register the known value explicitly and install a tolerant member map as a
# safety net, so any future unknown value is auto-registered with a warning
# instead of killing the poll. Compatibility members are read-only: they are
# published as state but rejected as commands, because the API would not accept
# them back.

COMPAT_ENUM_MEMBERS: dict = {}   # enum class -> set of names added at runtime
_COMPAT_AUTO_BASE = 900          # value range for auto-registered members

# Values seen in the wild that are missing from the pinned ambientika_py.
KNOWN_MISSING_ENUM_MEMBERS = {
    "FanSpeed": (("Night", 3),),
}


def _register_enum_member(enum_cls, name: str, value: int):
    """Add a member to an existing IntEnum at runtime."""
    member = int.__new__(enum_cls, value)
    member._name_ = name
    member._value_ = value
    type.__setattr__(enum_cls, name, member)
    dict.__setitem__(enum_cls._member_map_, name, member)
    enum_cls._value2member_map_.setdefault(value, member)
    if name not in enum_cls._member_names_:
        enum_cls._member_names_.append(name)
    COMPAT_ENUM_MEMBERS.setdefault(enum_cls, set()).add(name)
    return member


class _TolerantMemberMap(dict):
    """Member map that registers unknown names instead of raising KeyError."""

    def __init__(self, enum_cls, data):
        super().__init__(data)
        self._enum_cls = enum_cls

    def __missing__(self, key):
        cls = self._enum_cls
        value = _COMPAT_AUTO_BASE + len(COMPAT_ENUM_MEMBERS.get(cls, ()))
        log.warning(
            "Unknown %s value %r reported by the Ambientika API - registering it "
            "as a compatibility member so the status poll keeps running. Please "
            "report this value so it can be added properly (see issue #5).",
            cls.__name__, key,
        )
        return _register_enum_member(cls, key, value)


def strict_enum_lookup(enum_cls, name: str):
    """Look up a member WITHOUT the tolerant fallback.

    Used on the command path: a bad MQTT payload must not silently create a new
    enum member, and compatibility members must not be sent back to the API.
    """
    member = dict.get(enum_cls._member_map_, name)
    if member is None or name in COMPAT_ENUM_MEMBERS.get(enum_cls, ()):
        return None
    return member


def install_enum_compat() -> None:
    """Register known missing members and make enum lookups fault tolerant."""
    for enum_cls in (OperatingMode, FanSpeed, HumidityLevel, LightSensorLevel):
        for name, value in KNOWN_MISSING_ENUM_MEMBERS.get(enum_cls.__name__, ()):
            if name not in enum_cls._member_map_:
                _register_enum_member(enum_cls, name, value)
        if not isinstance(enum_cls._member_map_, _TolerantMemberMap):
            type.__setattr__(
                enum_cls, "_member_map_",
                _TolerantMemberMap(enum_cls, enum_cls._member_map_),
            )


install_enum_compat()

# ---------------------------------------------------------------------------
# Protection constants
# ---------------------------------------------------------------------------

# "Zuluftmodus" (supply air / fresh-air overpressure) == OperatingMode.Intake.
RADON_PROTECTION_MODE = OperatingMode.Intake
# "Stufe 1" == FanSpeed.Low.
RADON_PROTECTION_FAN_DEFAULT = FanSpeed.Low
# Dew-point "do not ventilate" == switch the unit off.
DEWPOINT_BLOCK_MODE = OperatingMode.Off


def _truthy(value: str) -> bool:
    """Interpret a free-form MQTT payload as a boolean."""
    return value.strip().lower() in ("on", "true", "1", "yes", "alarm", "active", "block", "blocked")


def _to_float(raw: str) -> Optional[float]:
    try:
        return float(str(raw).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


def dew_point_c(temp_c: float, rh_pct: float) -> float:
    """Dew point in °C from temperature (°C) and relative humidity (%) - Magnus formula."""
    a, b = 17.625, 243.04
    rh = min(max(rh_pct, 0.01), 100.0)
    gamma = math.log(rh / 100.0) + (a * temp_c) / (b + temp_c)
    return (b * gamma) / (a - gamma)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _env(*names: str, default: str = "") -> str:
    """Return the first non-empty env var among 'names'.

    Treats the literal strings 'null' and 'None' as empty, because
    HA's bashio::config returns the string "null" for optional fields
    that are not set in options.json.
    """
    for n in names:
        v = os.environ.get(n)
        if v and v.lower() not in ("null", "none"):
            return v
    return default


class BridgeConfig:
    def __init__(self) -> None:
        self.username = ""
        self.password = ""
        self.host = "https://app.ambientika.eu:4521"

        self.mqtt_host = "localhost"
        self.mqtt_port = 1883
        self.mqtt_user = ""
        self.mqtt_pass = ""
        self.mqtt_client_id = "ambientika-bridge"
        self.mqtt_tls = False

        self.topic_prefix = "ambientika"
        self.discovery_prefix = "homeassistant"
        self.enable_discovery = True
        self.poll_interval = 30
        # Consecutive failed polls before a device is flagged offline. 1 = old
        # behaviour (offline on every single miss).
        self.availability_failure_threshold = 3
        self.log_level = "INFO"

        # --- NeuraCell-X radon protection ---
        self.neuracell_enabled = True
        self.radon_topic = "ambientika/radon/value"       # numeric Bq/m3
        self.radon_alarm_topic = "ambientika/radon/alarm"  # explicit ON/OFF
        self.radon_threshold = 300                          # Bq/m3 (DE reference value)
        self.radon_hysteresis = 50                          # Bq/m3
        self.radon_protection_fan = "Low"
        # radon "source": "signal" = the MQTT topics above; "device" = read the
        # radon meter's status directly from the Ambientika cloud (no hardware).
        self.radon_source = "signal"
        self.radon_device_serial = ""
        # Which status field of the radon meter carries its alarm/level, and
        # which of its values mean "radon alarm". Numeric values are compared to
        # radon_threshold; strings are matched against radon_device_alarm_values.
        self.radon_device_alarm_field = "air_quality"
        self.radon_device_alarm_values = "Bad,Poor,Very Bad,Alarm,Alert"

        # --- Dew-point control (Taupunktsteuerung) ---
        self.dewpoint_enabled = True
        # "signal": consume an external ON/OFF block signal from MQTT.
        # "computed": compute dew points internally from four sensor topics.
        self.dewpoint_source = "signal"
        # signal source:
        self.dewpoint_block_topic = "ambientika/dewpoint/block"  # truthy = block ventilation
        # device source: read a TPS device's status from the Ambientika cloud
        # (no extra hardware). Requires the TPS serial; block when its operating
        # mode is one of dewpoint_device_block_modes (default: Off).
        self.dewpoint_device_serial = ""
        self.dewpoint_device_block_modes = "Off"
        # computed source:
        self.dewpoint_indoor_temp_topic = "ambientika/dewpoint/indoor_temp"
        self.dewpoint_indoor_humidity_topic = "ambientika/dewpoint/indoor_humidity"
        self.dewpoint_outdoor_temp_topic = "ambientika/dewpoint/outdoor_temp"
        self.dewpoint_outdoor_humidity_topic = "ambientika/dewpoint/outdoor_humidity"
        # Block ventilation when outdoor_dp >= indoor_dp - margin (would add moisture).
        self.dewpoint_margin = 1.0        # °C
        self.dewpoint_hysteresis = 0.5    # °C
        # Restrict the dew-point block to specific units (by device name or
        # serial number, comma-separated). Empty = all devices (default,
        # backward compatible). Example: "SMART,OFFICE".
        self.dewpoint_block_devices = ""

    # ----- helpers -----
    @property
    def radon_protection_fan_speed(self) -> FanSpeed:
        try:
            return FanSpeed[self.radon_protection_fan]
        except KeyError:
            log.warning("Invalid radon_protection_fan %r, using Low.", self.radon_protection_fan)
            return RADON_PROTECTION_FAN_DEFAULT

    @property
    def radon_device_alarm_value_set(self) -> set:
        """String status values of the radon meter that mean "radon alarm".

        Used only with radon_source='device' when the alarm field is a string
        (e.g. air_quality). Numeric fields are compared to radon_threshold
        instead. Accepts commas or semicolons; matching is case-insensitive.
        """
        raw = str(self.radon_device_alarm_values or "").replace(";", ",")
        return {t.strip().lower() for t in raw.split(",") if t.strip()}

    @property
    def dewpoint_block_device_tokens(self) -> set:
        """Normalised set of device selectors for the dew-point block.

        Empty set means the block applies to every device (legacy behaviour).
        Accepts commas or semicolons as separators; matching is
        case-insensitive against device name or serial number.
        """
        raw = str(self.dewpoint_block_devices or "").replace(";", ",")
        return {t.strip().lower() for t in raw.split(",") if t.strip()}

    @property
    def dewpoint_device_block_mode_set(self) -> set:
        """OperatingModes that mean the source TPS device is blocking ventilation.

        Empty/invalid -> defaults to {Off}. Used only with dewpoint_source='device'.
        """
        raw = str(self.dewpoint_device_block_modes or "").replace(";", ",")
        modes = set()
        for name in (t.strip() for t in raw.split(",") if t.strip()):
            try:
                modes.add(OperatingMode[name])
            except KeyError:
                log.warning("Invalid dewpoint_device_block_mode %r (ignored).", name)
        return modes or {OperatingMode.Off}

    def _apply_extras(self, get, cast_bool=bool) -> None:
        """Populate NeuraCell-X + dew-point fields using a getter get(key, default)."""
        # radon
        self.neuracell_enabled = cast_bool(get("neuracell_enabled", self.neuracell_enabled))
        self.radon_topic = get("radon_topic", self.radon_topic) or self.radon_topic
        self.radon_alarm_topic = get("radon_alarm_topic", self.radon_alarm_topic) or self.radon_alarm_topic
        try:
            self.radon_threshold = int(get("radon_threshold", self.radon_threshold))
        except (TypeError, ValueError):
            pass
        try:
            self.radon_hysteresis = int(get("radon_hysteresis", self.radon_hysteresis))
        except (TypeError, ValueError):
            pass
        self.radon_protection_fan = get("radon_protection_fan", self.radon_protection_fan) or self.radon_protection_fan
        self.radon_source = get("radon_source", self.radon_source) or self.radon_source
        self.radon_device_serial = get("radon_device_serial", self.radon_device_serial) or self.radon_device_serial
        self.radon_device_alarm_field = get("radon_device_alarm_field", self.radon_device_alarm_field) or self.radon_device_alarm_field
        # Empty string is a valid value here (means "no string values"), accept verbatim.
        rdav = get("radon_device_alarm_values", self.radon_device_alarm_values)
        self.radon_device_alarm_values = self.radon_device_alarm_values if rdav is None else str(rdav)
        if self.radon_source not in ("signal", "device"):
            log.warning("Invalid radon_source %r, using 'signal'.", self.radon_source)
            self.radon_source = "signal"
        # dew point
        self.dewpoint_enabled = cast_bool(get("dewpoint_enabled", self.dewpoint_enabled))
        self.dewpoint_source = get("dewpoint_source", self.dewpoint_source) or self.dewpoint_source
        self.dewpoint_block_topic = get("dewpoint_block_topic", self.dewpoint_block_topic) or self.dewpoint_block_topic
        self.dewpoint_indoor_temp_topic = get("dewpoint_indoor_temp_topic", self.dewpoint_indoor_temp_topic) or self.dewpoint_indoor_temp_topic
        self.dewpoint_indoor_humidity_topic = get("dewpoint_indoor_humidity_topic", self.dewpoint_indoor_humidity_topic) or self.dewpoint_indoor_humidity_topic
        self.dewpoint_outdoor_temp_topic = get("dewpoint_outdoor_temp_topic", self.dewpoint_outdoor_temp_topic) or self.dewpoint_outdoor_temp_topic
        self.dewpoint_outdoor_humidity_topic = get("dewpoint_outdoor_humidity_topic", self.dewpoint_outdoor_humidity_topic) or self.dewpoint_outdoor_humidity_topic
        # Empty string is a valid value here (means "all devices"), so accept it verbatim.
        dbd = get("dewpoint_block_devices", self.dewpoint_block_devices)
        self.dewpoint_block_devices = "" if dbd is None else str(dbd)
        self.dewpoint_device_serial = get("dewpoint_device_serial", self.dewpoint_device_serial) or self.dewpoint_device_serial
        self.dewpoint_device_block_modes = get("dewpoint_device_block_modes", self.dewpoint_device_block_modes) or self.dewpoint_device_block_modes
        try:
            self.dewpoint_margin = float(get("dewpoint_margin", self.dewpoint_margin))
        except (TypeError, ValueError):
            pass
        try:
            self.dewpoint_hysteresis = float(get("dewpoint_hysteresis", self.dewpoint_hysteresis))
        except (TypeError, ValueError):
            pass
        if self.dewpoint_source not in ("signal", "computed", "device"):
            log.warning("Invalid dewpoint_source %r, using 'signal'.", self.dewpoint_source)
            self.dewpoint_source = "signal"

    def apply_env_overrides(self) -> None:
        """Override any field with matching env vars (HA add-on uses these)."""
        for attr, names in (
            ("username", ("AMBIENTIKA_USERNAME", "AMBIENTIKA_USER")),
            ("password", ("AMBIENTIKA_PASSWORD", "AMBIENTIKA_PASS")),
            ("host", ("AMBIENTIKA_HOST",)),
            ("mqtt_host", ("MQTT_HOST",)),
            ("mqtt_user", ("MQTT_USERNAME", "MQTT_USER")),
            ("mqtt_pass", ("MQTT_PASSWORD", "MQTT_PASS")),
            ("topic_prefix", ("MQTT_TOPIC_PREFIX", "TOPIC_PREFIX")),
            ("discovery_prefix", ("DISCOVERY_PREFIX",)),
            ("log_level", ("LOG_LEVEL",)),
            ("radon_topic", ("RADON_TOPIC",)),
            ("radon_alarm_topic", ("RADON_ALARM_TOPIC",)),
            ("radon_protection_fan", ("RADON_PROTECTION_FAN",)),
            ("radon_source", ("RADON_SOURCE",)),
            ("radon_device_serial", ("RADON_DEVICE_SERIAL",)),
            ("radon_device_alarm_field", ("RADON_DEVICE_ALARM_FIELD",)),
            ("radon_device_alarm_values", ("RADON_DEVICE_ALARM_VALUES",)),
            ("dewpoint_source", ("DEWPOINT_SOURCE",)),
            ("dewpoint_block_topic", ("DEWPOINT_BLOCK_TOPIC",)),
            ("dewpoint_block_devices", ("DEWPOINT_BLOCK_DEVICES",)),
            ("dewpoint_device_serial", ("DEWPOINT_DEVICE_SERIAL",)),
            ("dewpoint_device_block_modes", ("DEWPOINT_DEVICE_BLOCK_MODES",)),
        ):
            v = _env(*names)
            if v:
                setattr(self, attr, v)

        mp = _env("MQTT_PORT")
        if mp:
            try:
                self.mqtt_port = int(mp)
            except ValueError:
                pass
        pi = _env("POLL_INTERVAL")
        if pi:
            try:
                self.poll_interval = int(pi)
            except ValueError:
                pass
        aft = _env("AVAILABILITY_FAILURE_THRESHOLD")
        if aft:
            try:
                self.availability_failure_threshold = int(aft)
            except ValueError:
                pass
        rth = _env("RADON_THRESHOLD")
        if rth:
            try:
                self.radon_threshold = int(rth)
            except ValueError:
                pass
        rhy = _env("RADON_HYSTERESIS")
        if rhy:
            try:
                self.radon_hysteresis = int(rhy)
            except ValueError:
                pass
        dm = _env("DEWPOINT_MARGIN")
        if dm:
            try:
                self.dewpoint_margin = float(dm)
            except ValueError:
                pass
        dh = _env("DEWPOINT_HYSTERESIS")
        if dh:
            try:
                self.dewpoint_hysteresis = float(dh)
            except ValueError:
                pass
        ne = _env("NEURACELL_ENABLED")
        if ne:
            self.neuracell_enabled = ne.lower() not in ("false", "0", "no", "off")
        de = _env("DEWPOINT_ENABLED")
        if de:
            self.dewpoint_enabled = de.lower() not in ("false", "0", "no", "off")

    @classmethod
    def from_yaml(cls, path: str) -> "BridgeConfig":
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
        cfg = cls()

        amb = raw.get("ambientika", {}) or {}
        cfg.username = amb.get("username", "") or ""
        cfg.password = amb.get("password", "") or ""
        cfg.host = amb.get("host", cfg.host)

        mq = raw.get("mqtt", {}) or {}
        cfg.mqtt_host = mq.get("host", cfg.mqtt_host)
        cfg.mqtt_port = int(mq.get("port", cfg.mqtt_port))
        cfg.mqtt_user = mq.get("username", "") or ""
        cfg.mqtt_pass = mq.get("password", "") or ""
        cfg.mqtt_tls = bool(mq.get("tls", False))

        br = raw.get("bridge", {}) or {}
        cfg.topic_prefix = br.get("topic_prefix", cfg.topic_prefix)
        cfg.discovery_prefix = br.get("discovery_prefix", cfg.discovery_prefix)
        cfg.enable_discovery = bool(br.get("enable_discovery", True))
        cfg.poll_interval = int(br.get("poll_interval", cfg.poll_interval))
        cfg.availability_failure_threshold = int(br.get(
            "availability_failure_threshold", cfg.availability_failure_threshold))
        cfg.log_level = br.get("log_level", cfg.log_level)

        extras = {}
        extras.update(raw.get("neuracell", {}) or {})
        extras.update(raw.get("dewpoint", {}) or {})
        cfg._apply_extras(extras.get)

        cfg.apply_env_overrides()
        return cfg

    @classmethod
    def from_ha_options(cls, path: str) -> "BridgeConfig":
        """Read /data/options.json written by Home Assistant Supervisor."""
        with open(path) as f:
            raw = json.load(f)
        cfg = cls()
        cfg.username = raw.get("ambientika_username", raw.get("ambientika_user", "")) or ""
        cfg.password = raw.get("ambientika_password", raw.get("ambientika_pass", "")) or ""
        cfg.host = raw.get("ambientika_host", cfg.host)
        cfg.mqtt_host = raw.get("mqtt_host", cfg.mqtt_host)
        cfg.mqtt_port = int(raw.get("mqtt_port", cfg.mqtt_port))
        cfg.mqtt_user = raw.get("mqtt_username", raw.get("mqtt_user", "")) or ""
        cfg.mqtt_pass = raw.get("mqtt_password", raw.get("mqtt_pass", "")) or ""
        cfg.mqtt_tls = bool(raw.get("mqtt_tls", False))
        cfg.topic_prefix = raw.get("mqtt_topic_prefix", raw.get("topic_prefix", cfg.topic_prefix))
        cfg.discovery_prefix = raw.get("discovery_prefix", cfg.discovery_prefix)
        cfg.enable_discovery = bool(raw.get("enable_discovery", True))
        cfg.poll_interval = int(raw.get("poll_interval", cfg.poll_interval))
        cfg.availability_failure_threshold = int(raw.get(
            "availability_failure_threshold", cfg.availability_failure_threshold))
        cfg.log_level = raw.get("log_level", cfg.log_level)
        cfg._apply_extras(raw.get)
        cfg.apply_env_overrides()
        return cfg

    @classmethod
    def from_env(cls) -> "BridgeConfig":
        cfg = cls()
        cfg.apply_env_overrides()
        return cfg


# ---------------------------------------------------------------------------
# Topic helpers
# ---------------------------------------------------------------------------

def state_topic(prefix: str, serial: str) -> str:
    return f"{prefix}/{serial}/state"

def avail_topic(prefix: str, serial: str) -> str:
    return f"{prefix}/{serial}/availability"

def cmd_topic(prefix: str, serial: str, attr: str) -> str:
    return f"{prefix}/{serial}/set/{attr}"

def neuracell_state_topic(prefix: str) -> str:
    return f"{prefix}/neuracell/state"

def reset_state_topic(prefix: str, serial: str) -> str:
    return f"{prefix}/{serial}/reset_state"

def bridge_avail_topic(prefix: str) -> str:
    return f"{prefix}/bridge/availability"

def build_bridge_discovery(cfg: BridgeConfig):
    """Discovery for a single connectivity sensor showing the bridge itself is up."""
    base = cfg.discovery_prefix
    prefix = cfg.topic_prefix
    return [(
        f"{base}/binary_sensor/{prefix}_bridge/config",
        {
            "name": "Ambientika Bridge",
            "unique_id": f"ambientika_{prefix}_bridge_online",
            "state_topic": bridge_avail_topic(prefix),
            "payload_on": "online",
            "payload_off": "offline",
            "device_class": "connectivity",
            "device": {
                "identifiers": [f"{prefix}_bridge"],
                "name": "Ambientika MQTT Bridge",
                "manufacturer": "Ambientika / SUEDWIND",
                "model": "MQTT Bridge",
            },
        },
    )]

# ---------------------------------------------------------------------------
# Home Assistant Auto-Discovery
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Numerische Begleitwerte (eine Zahl je kategorialem Textwert) - direkt im
# state-Topic, damit Zeitreihen/Grafana ohne eigene Uebersetzungstabelle
# auskommen. Kalibriert auf die realen Geraetestrings, konsistent mit der
# Local-App-Historie.
# ---------------------------------------------------------------------------
_AIR_QUALITY_NUM = {                       # hoeher = bessere Luft (0..4)
    # Real device air-quality scale (server AirQuality enum) has exactly five
    # steps and NO "VeryBad": VeryGood(best)..Bad(worst). Mapped higher = better,
    # so it lines up 1:1 with the app/local history and long-term charts.
    "verygood": 4, "good": 3, "medium": 2, "poor": 1, "bad": 0,
}
_FILTER_STATUS_NUM = {                      # hoeher = dringlicher (0 gruen..2 rot)
    "good": 0, "green": 0, "gruen": 0, "ok": 0, "clean": 0,
    "medium": 1, "yellow": 1, "gelb": 1, "moderate": 1, "warn": 1,
    "bad": 2, "red": 2, "rot": 2, "dirty": 2, "alarm": 2,
}


def _norm_str(s):
    if s is None:
        return None
    return str(s).strip().lower().replace(" ", "").replace("_", "").replace("-", "")


def air_quality_to_num(value):
    """air_quality-String -> 0..4 (hoeher = besser). Unbekannt/Zahl -> None."""
    return _AIR_QUALITY_NUM.get(_norm_str(value))


def filter_status_to_num(value):
    """filters_status-String -> 0/1/2 (hoeher = dringlicher). Unbekannt -> None."""
    return _FILTER_STATUS_NUM.get(_norm_str(value))


def _enum_num(v):
    """IntEnum-Wert -> int, sonst None."""
    try:
        return int(v.value)
    except Exception:
        return None


def _fan_speed_num(v):
    """FanSpeed Low/Medium/High -> 1/2/3 (0 des Enums + 1)."""
    n = _enum_num(v)
    return n + 1 if n is not None else None


def build_discovery_configs(cfg: BridgeConfig, serial: str, device_name: str):
    device_info = {
        "identifiers": [serial],
        "name": device_name,
        "manufacturer": "Ambientika / SUEDWIND",
        "model": "Smart Ventilation Unit",
    }
    base = cfg.discovery_prefix
    prefix = cfg.topic_prefix
    avail = avail_topic(prefix, serial)
    state = state_topic(prefix, serial)

    entities = []

    sensor_defs = [
        ("temperature", "Temperature", "°C", "temperature", None),
        ("humidity", "Humidity", "%", "humidity", None),
        ("air_quality", "Air Quality", None, None, "mdi:air-filter"),
        ("filters_status", "Filter Status", None, None, "mdi:air-filter"),
        ("filters_status_raw", "Filter Status raw", None, None, "mdi:air-filter"),
        ("operating_mode", "Mode", None, None, "mdi:fan"),
        ("fan_speed", "Fan Speed", None, None, "mdi:speedometer"),
        ("humidity_level", "Humidity Level", None, None, "mdi:water-percent"),
        ("light_sensor_level", "Light Sensor Level", None, None, "mdi:brightness-5"),
        ("device_role", "Device Role", None, None, "mdi:information"),
        ("last_operating_mode", "Last Mode", None, None, "mdi:fan-clock"),
        ("zone_index", "Zone", None, None, "mdi:home-group"),
    ]
    for key, name, unit, dc, icon in sensor_defs:
        p = {
            "name": name,
            "unique_id": f"ambientika_{serial}_{key}",
            "state_topic": state,
            "value_template": f"{{{{ value_json.{key} }}}}",
            "availability_topic": avail,
            "device": device_info,
        }
        if unit:
            p["unit_of_measurement"] = unit
        if dc:
            p["device_class"] = dc
        if icon:
            p["icon"] = icon
        entities.append((f"{base}/sensor/{serial}_{key}/config", p))

    # Numerische Begleitsensoren: state_class=measurement => HA fuehrt die
    # Langzeitstatistik selbst (Grafana ohne eigene Uebersetzungstabelle).
    numeric_defs = [
        ("air_quality_num", "Air Quality (num)"),
        ("filter_status_num", "Filter Status (num)"),
        ("filter_status_raw_num", "Filter Status raw (num)"),
        ("operating_mode_num", "Mode (num)"),
        ("last_operating_mode_num", "Last Mode (num)"),
        ("fan_speed_num", "Fan Speed (num)"),
        ("humidity_level_num", "Humidity Level (num)"),
        ("light_sensor_level_num", "Light Sensor Level (num)"),
    ]
    for key, name in numeric_defs:
        p = {
            "name": name,
            "unique_id": f"ambientika_{serial}_{key}",
            "state_topic": state,
            "value_template": f"{{{{ value_json.{key} }}}}",
            "availability_topic": avail,
            "device": device_info,
            "state_class": "measurement",
        }
        entities.append((f"{base}/sensor/{serial}_{key}/config", p))

    bin_defs = [
        ("humidity_alarm", "Humidity Alarm", "moisture"),
        ("night_alarm", "Night Alarm", "problem"),
    ]
    for key, name, dc in bin_defs:
        p = {
            "name": name,
            "unique_id": f"ambientika_{serial}_{key}",
            "state_topic": state,
            "value_template": f"{{{{ value_json.{key} }}}}",
            "payload_on": "True",
            "payload_off": "False",
            "availability_topic": avail,
            "device": device_info,
            "device_class": dc,
        }
        entities.append((f"{base}/binary_sensor/{serial}_{key}/config", p))

    select_defs = [
        ("operating_mode", "Mode", [m.name for m in OperatingMode], "mdi:fan"),
        ("fan_speed", "Fan Speed", [s.name for s in FanSpeed], "mdi:speedometer"),
        ("humidity_level", "Humidity Level", [h.name for h in HumidityLevel], "mdi:water-percent"),
        ("light_sensor_level", "Light Sensor Level", [l.name for l in LightSensorLevel], "mdi:brightness-5"),
    ]
    for key, name, opts, icon in select_defs:
        p = {
            "name": name,
            "unique_id": f"ambientika_{serial}_{key}_select",
            "state_topic": state,
            "value_template": f"{{{{ value_json.{key} }}}}",
            "command_topic": cmd_topic(prefix, serial, key),
            "options": opts,
            "availability_topic": avail,
            "device": device_info,
            "icon": icon,
        }
        entities.append((f"{base}/select/{serial}_{key}/config", p))

    # Button: Filter-Reset. Setzt den Filteralarm/-zaehler ueber den
    # Cloud-Endpunkt zurueck - auch schon bei "gelb" (verschmutzt), also bevor
    # der App-eigene Reset-Button (erst bei "rot") erscheint.
    entities.append((
        f"{base}/button/{serial}_reset_filter/config",
        {
            "name": "Filter zuruecksetzen",
            "unique_id": f"ambientika_{serial}_reset_filter",
            "command_topic": cmd_topic(prefix, serial, "reset_filter"),
            "payload_press": "PRESS",
            "availability_topic": avail,
            "device": device_info,
            "icon": "mdi:air-filter",
        },
    ))

    # Filter-reset status sensor: honest running/confirmed/unconfirmed indicator.
    # Disabled by default and marked diagnostic, so it stays invisible to normal
    # users (a cloud reset is fire-and-forget, like the app) while power users can
    # switch it on to see whether a reset actually took.
    entities.append((
        f"{base}/sensor/{serial}_reset_state/config",
        {
            "name": "Filter Reset Status",
            "unique_id": f"ambientika_{serial}_reset_state",
            "state_topic": reset_state_topic(prefix, serial),
            "availability_topic": avail,
            "device": device_info,
            "icon": "mdi:air-filter",
            "enabled_by_default": False,
            "entity_category": "diagnostic",
        },
    ))

    return entities


def build_neuracell_discovery(cfg: BridgeConfig):
    """Discovery configs for the NeuraCell-X protection status."""
    base = cfg.discovery_prefix
    state = neuracell_state_topic(cfg.topic_prefix)
    device_info = {
        "identifiers": ["neuracell_x"],
        "name": "NeuraCell-X",
        "manufacturer": "Ambientika / SUEDWIND",
        "model": "NeuraCell-X AI Neural Control System",
    }
    entities = [
        (f"{base}/binary_sensor/neuracell_radon_protection/config", {
            "name": "Radon Protection Active",
            "unique_id": "neuracell_radon_protection_active",
            "state_topic": state,
            "value_template": "{{ value_json.radon_protection }}",
            "payload_on": "True", "payload_off": "False",
            "device_class": "safety", "icon": "mdi:shield-home",
            "device": device_info,
        }),
        (f"{base}/sensor/neuracell_radon_level/config", {
            "name": "Radon Level",
            "unique_id": "neuracell_radon_level",
            "state_topic": state,
            "value_template": "{{ value_json.radon }}",
            "unit_of_measurement": "Bq/m³", "icon": "mdi:radioactive",
            "device": device_info,
        }),
        (f"{base}/binary_sensor/neuracell_dewpoint_block/config", {
            "name": "Ventilation Blocked (Dew Point)",
            "unique_id": "neuracell_dewpoint_block",
            "state_topic": state,
            "value_template": "{{ value_json.dewpoint_block }}",
            "payload_on": "True", "payload_off": "False",
            "device_class": "moisture", "icon": "mdi:water-off",
            "device": device_info,
        }),
        (f"{base}/sensor/neuracell_dewpoint_indoor/config", {
            "name": "Dew Point Indoor",
            "unique_id": "neuracell_dewpoint_indoor",
            "state_topic": state,
            "value_template": "{{ value_json.indoor_dew_point }}",
            "unit_of_measurement": "°C", "icon": "mdi:thermometer-water",
            "device": device_info,
        }),
        (f"{base}/sensor/neuracell_dewpoint_outdoor/config", {
            "name": "Dew Point Outdoor",
            "unique_id": "neuracell_dewpoint_outdoor",
            "state_topic": state,
            "value_template": "{{ value_json.outdoor_dew_point }}",
            "unit_of_measurement": "°C", "icon": "mdi:thermometer-water",
            "device": device_info,
        }),
    ]
    return entities


# ---------------------------------------------------------------------------
# NeuraCell-X controller  (radon + dew point with priority arbitration)
# ---------------------------------------------------------------------------

class NeuraCellXController:
    """Radon protection (priority) + dew-point control, with baseline restore.

    Desired override per device:
        radon_active     -> Intake / protection fan          (priority 1)
        dewpoint_block   -> Off (keep fan/humidity)          (priority 2)
        neither          -> restore pre-protection baseline
    """

    def __init__(self, bridge: "AmbientikaBridge", cfg: BridgeConfig) -> None:
        self.bridge = bridge
        self.cfg = cfg

        self.radon_active = False
        self.dewpoint_block = False
        self.last_radon: Optional[float] = None

        self.indoor_dew_point: Optional[float] = None
        self.outdoor_dew_point: Optional[float] = None
        self._dp_inputs: dict = {}  # indoor_t, indoor_rh, outdoor_t, outdoor_rh

        self._saved_modes: dict = {}  # serial -> {operating_mode, fan_speed, humidity_level}
        self._pending_manual: dict = {}  # serial -> partial baseline from manual cmds while offline
        self._lock = asyncio.Lock()

    # ----- convenience -----
    @property
    def override_active(self) -> bool:
        return self.radon_active or self.dewpoint_block

    def _dewpoint_targets(self, serial: str, device: Any) -> bool:
        """Whether a dew-point block applies to this device.

        Empty selector list -> every device (legacy behaviour). Otherwise the
        device matches if its serial number or its name is in the list
        (case-insensitive).
        """
        tokens = self.cfg.dewpoint_block_device_tokens
        if not tokens:
            return True
        name = (getattr(device, "name", "") or "").strip().lower()
        return serial.strip().lower() in tokens or name in tokens

    def _device_under_control(self, serial: str, device: Any) -> bool:
        """Whether the currently active protection actually controls this device.

        Radon protection always applies to every unit (safety overpressure).
        A dew-point block may be limited to selected units.
        """
        if self.radon_active:
            return True
        if self.dewpoint_block:
            return self._dewpoint_targets(serial, device)
        return False

    # ----- radon signals -----
    async def on_radon_value(self, raw: str) -> None:
        if not self.cfg.neuracell_enabled:
            return
        value = _to_float(raw)
        if value is None:
            log.warning("NeuraCell-X: could not parse radon value %r", raw)
            return
        self.last_radon = value
        changed = False
        if not self.radon_active and value >= self.cfg.radon_threshold:
            log.warning("NeuraCell-X: radon %.0f >= %d Bq/m3 -> radon protection ON.",
                        value, self.cfg.radon_threshold)
            self.radon_active = True
            changed = True
        elif self.radon_active and value <= (self.cfg.radon_threshold - self.cfg.radon_hysteresis):
            log.warning("NeuraCell-X: radon %.0f Bq/m3 back to safe -> radon protection OFF.", value)
            self.radon_active = False
            changed = True
        if changed:
            await self.reconcile(force=True)
        else:
            self.bridge.publish_neuracell_state()

    async def on_radon_alarm(self, raw: str) -> None:
        if not self.cfg.neuracell_enabled:
            return
        on = _truthy(raw)
        if on != self.radon_active:
            self.radon_active = on
            log.warning("NeuraCell-X: explicit radon alarm %s.", "ON" if on else "OFF")
            await self.reconcile(force=True)

    async def poll_radon_device(self, device: Any) -> None:
        """Derive the radon alarm from a radon meter's cloud status (source='device').

        Reads one status field of the radon meter via the Ambientika API and
        decides whether radon protection should be active. Numeric fields go
        through the same threshold/hysteresis path as radon_topic; boolean
        fields are used directly; string/enum fields (e.g. air_quality) count as
        an alarm when their value is in cfg.radon_device_alarm_value_set. No hardware.
        """
        if not self.cfg.neuracell_enabled:
            return
        status = await self.bridge.read_status(device)
        if status is None:
            log.warning("NeuraCell-X: radon source device %s unreachable this poll; keeping last state.",
                        getattr(device, "serial_number", "?"))
            return
        field = self.cfg.radon_device_alarm_field
        raw = status.get(field)
        if raw is None:
            log.warning("NeuraCell-X: radon source device %s has no status field %r; keeping last state. "
                        "Available fields: %s",
                        getattr(device, "serial_number", "?"), field,
                        ", ".join(sorted(status.keys())))
            return
        # bool must be checked before int (bool is a subclass of int).
        if isinstance(raw, bool):
            on = raw
        elif isinstance(raw, (int, float)):
            # Numeric field -> reuse the threshold/hysteresis path (does reconcile).
            await self.on_radon_value(str(raw))
            return
        else:
            value = getattr(raw, "name", raw)   # enum -> its name, else the value itself
            on = str(value).strip().lower() in self.cfg.radon_device_alarm_value_set
        log.debug("NeuraCell-X: radon meter %s %s=%r -> %s",
                  getattr(device, "serial_number", "?"), field, raw, "ALARM" if on else "clear")
        if on != self.radon_active:
            self.radon_active = on
            log.warning("NeuraCell-X: radon meter %s -> radon protection %s.",
                        getattr(device, "serial_number", "?"), "ON" if on else "OFF")
            await self.reconcile(force=True)
        else:
            self.bridge.publish_neuracell_state()

    # ----- dew-point signals -----
    async def on_dewpoint_block(self, raw: str) -> None:
        """External ON/OFF block signal (source='signal')."""
        if not self.cfg.dewpoint_enabled:
            return
        block = _truthy(raw)
        await self._set_dewpoint_block(block)

    async def on_dewpoint_sensor(self, which: str, raw: str) -> None:
        """One of the four sensor inputs (source='computed')."""
        if not self.cfg.dewpoint_enabled:
            return
        val = _to_float(raw)
        if val is None:
            log.warning("NeuraCell-X: could not parse dew-point sensor %s=%r", which, raw)
            return
        self._dp_inputs[which] = val
        needed = ("indoor_t", "indoor_rh", "outdoor_t", "outdoor_rh")
        if not all(k in self._dp_inputs for k in needed):
            return
        self.indoor_dew_point = dew_point_c(self._dp_inputs["indoor_t"], self._dp_inputs["indoor_rh"])
        self.outdoor_dew_point = dew_point_c(self._dp_inputs["outdoor_t"], self._dp_inputs["outdoor_rh"])
        # Ventilating brings outdoor air in. If the outdoor dew point is at/above
        # (indoor dew point - margin), ventilating would add moisture -> block.
        margin = self.cfg.dewpoint_margin
        hyst = self.cfg.dewpoint_hysteresis
        if not self.dewpoint_block and self.outdoor_dew_point >= (self.indoor_dew_point - margin):
            await self._set_dewpoint_block(True)
        elif self.dewpoint_block and self.outdoor_dew_point <= (self.indoor_dew_point - margin - hyst):
            await self._set_dewpoint_block(False)
        else:
            self.bridge.publish_neuracell_state()

    async def _set_dewpoint_block(self, block: bool) -> None:
        if block == self.dewpoint_block:
            self.bridge.publish_neuracell_state()
            return
        self.dewpoint_block = block
        log.warning("NeuraCell-X: dew-point ventilation %s.",
                    "BLOCKED (fans off)" if block else "released")
        await self.reconcile(force=True)

    async def poll_dewpoint_device(self, device: Any) -> None:
        """Derive the dew-point block from a TPS device's cloud status (source='device').

        Reads the TPS's operating mode via the Ambientika API and blocks when it
        is one of cfg.dewpoint_device_block_mode_set (default: Off). No hardware.
        """
        if not self.cfg.dewpoint_enabled:
            return
        status = await self.bridge.read_status(device)
        if status is None:
            log.warning("NeuraCell-X: dew-point source device %s unreachable this poll; keeping last state.",
                        getattr(device, "serial_number", "?"))
            return
        mode = status["operating_mode"]
        block = mode in self.cfg.dewpoint_device_block_mode_set
        log.debug("NeuraCell-X: TPS %s operating_mode=%s -> %s",
                  getattr(device, "serial_number", "?"), mode.name, "block" if block else "clear")
        await self._set_dewpoint_block(block)

    # ----- desired state -----
    def _desired(self, status: dict):
        """Return (operating_mode, fan_speed, humidity_level) or None to restore."""
        if self.radon_active:
            return (RADON_PROTECTION_MODE, self.cfg.radon_protection_fan_speed, status["humidity_level"])
        if self.dewpoint_block:
            return (DEWPOINT_BLOCK_MODE, status["fan_speed"], status["humidity_level"])
        return None

    # ----- reconciliation -----
    async def reconcile(self, force: bool = False) -> None:
        """Bring every device in line with the current protection state.

        Serialised with a lock so signal-triggered and poll-triggered
        reconciles can never interleave and corrupt the saved baseline.
        """
        async with self._lock:
            if self.override_active:
                for serial, device in list(self.bridge.devices.items()):
                    # Only touch devices the active protection actually controls.
                    # Radon protects every unit; a dew-point block can be limited
                    # to selected units (cfg.dewpoint_block_devices).
                    if not self._device_under_control(serial, device):
                        continue
                    status = await self.bridge.read_status(device)
                    if status is None:
                        log.warning("NeuraCell-X: %s unreachable; will retry on next poll.", serial)
                        continue
                    desired = self._desired(status)
                    if desired is None:
                        continue
                    # Capture the pre-protection baseline exactly once per device,
                    # honouring any manual change made while it was overridden.
                    # Only devices we actually control get a baseline, so a
                    # targeted dew-point block never disturbs the other units.
                    if serial not in self._saved_modes:
                        base = {
                            "operating_mode": status["operating_mode"],
                            "fan_speed": status["fan_speed"],
                            "humidity_level": status["humidity_level"],
                        }
                        base.update(self._pending_manual.pop(serial, {}))
                        self._saved_modes[serial] = base
                    mode, fan, hum = desired
                    if (force or status["operating_mode"] != mode
                            or status["fan_speed"] != fan
                            or status["humidity_level"] != hum):
                        await self.bridge.set_device_mode(device, mode, fan, hum)
            elif self._saved_modes:
                # All protections cleared: restore each device, retry-safe.
                # Keep a device's baseline until its restore actually succeeds,
                # so an offline device is not left stuck in protection mode.
                for serial in list(self._saved_modes.keys()):
                    device = self.bridge.devices.get(serial)
                    if device is None:
                        del self._saved_modes[serial]
                        continue
                    saved = self._saved_modes[serial]
                    ok = await self.bridge.set_device_mode(
                        device, saved["operating_mode"], saved["fan_speed"], saved["humidity_level"]
                    )
                    if ok:
                        del self._saved_modes[serial]
                if not self._saved_modes:
                    log.warning("NeuraCell-X: all protections cleared -> devices restored.")
            self.bridge.publish_neuracell_state()

    async def enforce(self) -> None:
        """Called every poll: re-assert an active override, or finish a pending restore."""
        if self.override_active or self._saved_modes:
            await self.reconcile(force=False)


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------

class AmbientikaBridge:
    def __init__(self, cfg: BridgeConfig) -> None:
        self.cfg = cfg
        self.client: Optional[mqtt.Client] = None
        self.api: Optional[Ambientika] = None
        self.devices: dict = {}
        self.dewpoint_device: Optional[Device] = None
        self.radon_device: Optional[Device] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._reset_tasks: set = set()
        self._reset_inflight: set = set()
        self._pending_resets: set = set()
        self._reauth_ts: float = 0.0
        self._stop_event: Optional[asyncio.Event] = None
        self.neuracell = NeuraCellXController(self, cfg)
        # serial -> number of consecutive failed polls (availability debounce)
        self._poll_failures: dict = {}
        # serial -> last light_sensor_level seen while the unit was running (or
        # last set by the user). A powered-off unit reports a default level, so
        # we keep this to avoid overwriting the user's dusk-sensor choice on an
        # off->on toggle.
        self._last_light: dict = {}

    # ----- availability debounce -----
    def _note_poll_failure(self, serial: str, reason: str) -> None:
        """Count a failed poll and flag the device offline only after N in a row.

        A single missed cycle is normally a transient cloud hiccup - the API
        sporadically answers HTTP 404 "Status packet not found!" for a device
        that is perfectly reachable. Publishing "offline" on the first miss
        makes the entity flicker to unavailable and back for one poll interval.
        """
        n = self._poll_failures.get(serial, 0) + 1
        self._poll_failures[serial] = n
        threshold = max(1, self.cfg.availability_failure_threshold)
        if n < threshold:
            log.info("Poll %d/%d failed for %s (%s) - keeping it available.",
                     n, threshold, serial, reason)
            return
        if n == threshold:
            log.warning("Poll failed %d times in a row for %s (%s) - marking offline.",
                        n, serial, reason)
        if self.client is not None:
            self.client.publish(avail_topic(self.cfg.topic_prefix, serial),
                                "offline", qos=0, retain=True)

    def _note_poll_success(self, serial: str) -> None:
        if self._poll_failures.pop(serial, 0):
            log.info("Device %s is answering again.", serial)

    # ----- device helpers -----
    async def _reset_request(self, device, method, path, body):
        """Send one raw reset request; return (status, allow_header, text).

        Raw on purpose so we see the real status code and a short body snippet.
        GET sends the dict as query parameters. Only device/reset-filter (the one
        documented filter reset) is ever passed in; DELETE is never sent.
        """
        api = getattr(device, "api", None)
        if api is None:
            return (None, None, None)
        url = f"{api.host}/{path}"
        # Send exactly what the app sends: bearer token + a JSON content-type
        # header (from the app source, HttpService.ts / HouseService.ts). Note:
        # decompiling the server showed this header has no effect on the GET - the
        # reset lands or not purely on whether the device socket is live - but we
        # keep it so our request stays byte-identical to the app's.
        headers = {"Authorization": f"Bearer {api.token}",
                   "Content-Type": "application/json"}
        m = method.upper()
        try:
            async with aiohttp.ClientSession() as sess:
                if m == "GET":
                    cm = sess.get(url, headers=headers, params=body)
                elif m == "POST":
                    cm = sess.post(url, headers=headers, json=body)
                else:
                    return (None, None, None)
                async with cm as r:
                    try:
                        text = (await r.text())[:120]
                    except Exception:
                        text = ""
                    return (r.status, r.headers.get("Allow"), text)
        except Exception as e:
            log.warning("filter reset request %s %s raised for %s: %s",
                        m, path, device.serial_number, e)
            return (None, None, None)

    def _zone_master(self, device):
        """Return the Master device of this device's zone, or None.

        A coupled Ambientika group has one Master and one or more Slaves sharing a
        zone_index; per the RS485 protocol only the Master applies the filter
        reset. Returns None if the device is itself the Master or no Master with
        the same zone_index is found.
        """
        if str(getattr(device, "role", "") or "").lower() == "master":
            return None
        zone = getattr(device, "zone_index", None)
        if zone is None:
            return None
        for other in self.devices.values():
            if (other.serial_number != device.serial_number
                    and getattr(other, "zone_index", None) == zone
                    and str(getattr(other, "role", "") or "").lower() == "master"):
                return other
        return None

    def _reset_candidates(self, device):
        """Ordered (serial, label) list to send the documented reset-filter to:
        the target device first, then the Master of its zone (a Slave's reset is
        acknowledged but applied by the Master). Both are the same safe,
        documented GET device/reset-filter; nothing else is ever contacted.
        """
        cands = [(device.serial_number, "device")]
        master = self._zone_master(device)
        if master is not None:
            cands.append((master.serial_number, "zone master %s" % master.serial_number))
        return cands

    # ----- filter reset orchestration: dedupe, status sensor, persistence -----
    def _publish_reset_state(self, serial: str, state: str) -> None:
        """Publish the per-device reset status.

        idle / running / confirmed / acknowledged / unconfirmed. "acknowledged"
        means: the counter could not be cleared remotely (a Slave), but the
        maintenance was recorded bridge-side, so the effective filter status is
        green while the raw device value stays red.
        """
        if self.client is None:
            return
        try:
            self.client.publish(reset_state_topic(self.cfg.topic_prefix, serial),
                                state, qos=0, retain=True)
        except Exception as e:
            log.warning("could not publish reset_state for %s: %s", serial, e)

    def _load_pending(self) -> None:
        try:
            with open(PENDING_RESET_FILE, encoding="utf-8") as f:
                data = json.load(f)
            self._pending_resets = {str(s) for s in data} if isinstance(data, list) else set()
        except FileNotFoundError:
            self._pending_resets = set()
        except Exception as e:
            log.warning("could not read pending resets from %s: %s", PENDING_RESET_FILE, e)
            self._pending_resets = set()

    def _save_pending(self) -> None:
        try:
            with open(PENDING_RESET_FILE, "w", encoding="utf-8") as f:
                json.dump(sorted(self._pending_resets), f)
        except Exception as e:
            log.warning("could not persist pending resets to %s: %s", PENDING_RESET_FILE, e)

    def _start_reset(self, device) -> None:
        """Run one filter reset per device: tracked, reported and persisted.

        Deduplicates (one running reset per serial), drives the reset-status
        sensor, persists the serial so a restart resumes it, and never lets the
        background task's exception vanish.
        """
        serial = device.serial_number
        if serial in self._reset_inflight:
            log.info("filter reset for %s already running - ignoring extra press", serial)
            return
        self._reset_inflight.add(serial)
        self._pending_resets.add(serial)
        self._save_pending()
        self._publish_reset_state(serial, "running")

        task = asyncio.create_task(self._reset_filter(device))
        self._reset_tasks.add(task)

        def _done(t: "asyncio.Task") -> None:
            self._reset_tasks.discard(t)
            self._reset_inflight.discard(serial)
            state = "unconfirmed"
            try:
                res = t.result()
                state = res if isinstance(res, str) else ("confirmed" if res else "unconfirmed")
            except asyncio.CancelledError:
                log.info("filter reset for %s cancelled", serial)
            except Exception as e:
                log.exception("filter reset task for %s crashed: %s", serial, e)
            self._publish_reset_state(serial, state)
            self._pending_resets.discard(serial)
            self._save_pending()

        task.add_done_callback(_done)

    async def _reauth(self, reason: str) -> bool:
        """Refresh the Ambientika token in place and re-point every device to it.

        All device calls go through device.api.get/post (device.api is the
        AmbientikaApi HTTP client, i.e. Ambientika._api), so a fresh login +
        re-point to the new client refreshes status,
        reset and change-mode without rebuilding any device - the poll loop and
        NeuraCell state stay intact. Never raises.
        """
        self._reauth_ts = time.monotonic()
        try:
            res = await authenticate(self.cfg.username, self.cfg.password, self.cfg.host)
            if isinstance(res, Failure):
                log.warning("re-auth (%s) failed: %s", reason, res)
                return False
            self.api = res.unwrap()
            for d in list(self.devices.values()):
                try:
                    d.api = self.api._api
                except Exception:
                    pass
            for d in (self.dewpoint_device, self.radon_device):
                if d is not None:
                    try:
                        d.api = self.api._api
                    except Exception:
                        pass
            log.info("re-auth (%s) OK - token refreshed for %d device(s).",
                     reason, len(self.devices))
            return True
        except Exception as e:
            log.warning("re-auth (%s) raised: %s", reason, e)
            return False


    async def _reset_filter(self, device) -> str:
        """Fire the documented filter reset and verify against the real status.

        Sent to the device and its zone Master (only the documented reset-filter
        GET is ever sent - never change-mode, reset-device or DELETE). Then we
        check the real device status and tell the truth about it:

          * Master / standalone: a reset can actually take, so we re-check a few
            times until the counter has demonstrably improved, instead of trusting
            the fire-and-forget HTTP 200. This runs for a Medium (yellow) counter
            just as for a red one; only a counter already at Good is skipped.
          * Slave: the cloud CANNOT clear a Slave's counter at all - the reset is
            applied only by the zone Master to the Master's own counter, while the
            Slave keeps its own. So we do NOT claim the status "will follow on the
            next poll"; we say plainly that a reset directly at the unit is needed,
            and (if SLAVE_FILTER_SOFT_RESET is on) record a bridge-side "serviced"
            acknowledgement so dashboards can go green without faking the raw value.
        """
        serial = device.serial_number
        is_slave = self._zone_master(device) is not None
        before = await self.read_status(device)
        before_fs = str((before or {}).get("filters_status") or "").lower()
        before_num = filter_status_to_num(before_fs)
        # Only a counter that is positively "Good" has nothing to reset. Medium
        # (yellow) is a real reset case - filters are usually cleaned before the
        # alarm turns red - and an unknown value is never a reason to skip
        # silently; we send the documented reset and report what really happens.
        if before_num == 0:
            log.info("filter reset for %s: filter status is already %r - nothing to do",
                     serial, (before or {}).get("filters_status"))
            return "confirmed"
        accepted = False
        for tserial, label in self._reset_candidates(device):
            status, _allow, text = await self._reset_request(
                device, "GET", "device/reset-filter", {"deviceSerialNumber": tserial})
            if status is None:
                continue
            if status >= 400:
                log.info("filter reset [%s] %s -> HTTP %s %s",
                         label, tserial, status, (text or "").strip())
                continue
            accepted = True
            log.info("filter reset [%s] %s -> HTTP %s (sent)", label, tserial, status)
        if not accepted:
            log.info("filter reset for %s: device not reachable right now - nothing sent",
                     serial)
            return "unconfirmed"
        # Verify against the real device status. A Slave can never clear remotely,
        # so we check once and then tell the truth; a Master we re-check a few times.
        verify_attempts = 1 if is_slave else 3
        after = before
        for _ in range(verify_attempts):
            await asyncio.sleep(FILTER_RESET_VERIFY_DELAY)
            after = await self.read_status(device)
            after_fs = str((after or {}).get("filters_status") or "").lower() if after else ""
            after_num = filter_status_to_num(after_fs)
            # Confirmed only when the counter really improved: cleared to Good, or
            # at least one step less urgent than before. Testing for "not red"
            # would report success for a Medium counter that never moved.
            if after_num is not None and (
                    after_num == 0
                    or (before_num is not None and after_num < before_num)):
                log.info("filter reset for %s: confirmed - filters_status %s -> %s",
                         serial, before_fs or "?", after_fs)
                return "confirmed"
        acknowledged = False
        if is_slave:
            self._filter_ack_write(serial, (after or {}).get("filters_status") or "Bad")
            acknowledged = self._soft_reset_enabled()
            log.warning(
                "filter reset for %s: this is a SLAVE - its filter counter cannot be "
                "reset remotely. The cloud reset is applied only by the zone Master to "
                "the Master's own counter; the Slave keeps its own. Reset the filter "
                "DIRECTLY AT THE UNIT (physical WallPanel / at the device). This is a "
                "device-side limitation, not a transient delay.", serial)
        else:
            log.info(
                "filter reset for %s: sent to the device and its zone Master; the device "
                "still reports filter %r after %d check(s). A cloud reset is fire-and-forget; "
                "if the device applies it, the change appears on a later poll.",
                serial, (after or {}).get("filters_status") if after else None, verify_attempts)
        if acknowledged:
            log.info("filter reset for %s: recorded bridge-side as serviced - the effective "
                     "filter status reports Good while the raw device value stays unchanged.",
                     serial)
            return "acknowledged"
        return "unconfirmed"

    # ---- optionaler Slave-Filter-Softreset (bridge-seitige Wartungs-Quittung) ----
    # Aktivieren mit SLAVE_FILTER_SOFT_RESET=1. Der rohe filters_status wird nie
    # veraendert; nur der EFFEKTIVE Wert (filter_status_num) zeigt fuer einen
    # gewarteten, weiterhin roten Slave "Good", bis FILTER_ACK_TTL_DAYS ablaufen.
    @staticmethod
    def _soft_reset_enabled() -> bool:
        return os.environ.get("SLAVE_FILTER_SOFT_RESET", "0") in ("1", "true", "True", "yes")

    @staticmethod
    def _filter_ack_path() -> str:
        return os.environ.get("FILTER_ACK_PATH", "/data/filter_ack.json")

    @staticmethod
    def _filter_ack_ttl() -> float:
        try:
            return float(os.environ.get("FILTER_ACK_TTL_DAYS", "90")) * 86400.0
        except Exception:
            return 90.0 * 86400.0

    @classmethod
    def _filter_ack_load(cls) -> dict:
        try:
            with open(cls._filter_ack_path(), "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            return {}

    @classmethod
    def _filter_ack_save(cls, data: dict) -> None:
        try:
            p = cls._filter_ack_path()
            d = os.path.dirname(p) or "."
            os.makedirs(d, exist_ok=True)
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, p)
        except Exception:
            pass  # best-effort; never break a reset/poll over a write error

    @classmethod
    def _filter_ack_write(cls, serial: str, raw_status) -> None:
        if not cls._soft_reset_enabled():
            return
        import time
        data = cls._filter_ack_load()
        data[serial] = {"acked_at": time.time(), "raw_when_acked": str(raw_status)}
        cls._filter_ack_save(data)

    @classmethod
    def _filter_ack_effective(cls, serial: str, raw_status):
        """Roh, ausser eine gueltige Quittung ueberschreibt einen weiter faelligen Slave -> 'Good'."""
        if not cls._soft_reset_enabled():
            return raw_status
        import time
        data = cls._filter_ack_load()
        rec = data.get(serial)
        if not rec:
            return raw_status
        if time.time() - float(rec.get("acked_at", 0)) > cls._filter_ack_ttl():
            data.pop(serial, None); cls._filter_ack_save(data); return raw_status
        # Faellig ist alles ueber "Good" - also Gelb wie Rot. Unbekannte Werte
        # gelten als nicht faellig, dann wird die Quittung aufgeraeumt.
        if (filter_status_to_num(raw_status) or 0) <= 0:
            data.pop(serial, None); cls._filter_ack_save(data); return raw_status
        return "Good"


    async def read_status(self, device: Device) -> Optional[dict]:
        try:
            res = await device.status()
        except Exception as e:
            log.exception("status() raised for %s: %s", device.serial_number, e)
            return None
        if isinstance(res, Failure):
            log.warning("status() failed for %s: %s", device.serial_number, res)
            return None
        return res.unwrap()

    async def set_device_mode(self, device, operating_mode, fan_speed, humidity_level) -> bool:
        # change_mode requires all four attributes; light_sensor_level is not a
        # parameter here, so fill it from the user's remembered level (falling
        # back to a live status read) instead of dropping it - dropping it both
        # raised KeyError and would have reset the user's dusk sensor.
        serial = device.serial_number
        light = self._last_light.get(serial)
        if light is None:
            st = await self.read_status(device)
            if st is not None:
                light = st["light_sensor_level"]
                if st["operating_mode"] != OperatingMode.Off:
                    self._last_light[serial] = light
        if light is None:
            light = LightSensorLevel.Off
        mode = {
            "operating_mode": operating_mode,
            "fan_speed": fan_speed,
            "humidity_level": humidity_level,
            "light_sensor_level": light,
        }
        try:
            res = await device.change_mode(mode)
        except Exception as e:
            log.exception("change_mode raised for %s: %s", device.serial_number, e)
            return False
        if isinstance(res, Failure):
            log.error("change_mode failed for %s: %s", device.serial_number, res)
            return False
        log.info("Device %s set to %s / %s.", device.serial_number, operating_mode.name, fan_speed.name)
        return True

    # ----- MQTT -----
    def _mqtt_connect(self) -> None:
        self.client = mqtt.Client(client_id=self.cfg.mqtt_client_id, clean_session=True)
        if self.cfg.mqtt_user:
            self.client.username_pw_set(self.cfg.mqtt_user, self.cfg.mqtt_pass)
        if self.cfg.mqtt_tls:
            self.client.tls_set()
        self.client.on_connect = self._on_mqtt_connect
        self.client.on_message = self._on_mqtt_message
        self.client.reconnect_delay_set(min_delay=1, max_delay=120)
        self.client.will_set(bridge_avail_topic(self.cfg.topic_prefix),
                             "offline", qos=0, retain=True)
        log.info("Connecting to MQTT broker %s:%s ...", self.cfg.mqtt_host, self.cfg.mqtt_port)
        self.client.connect(self.cfg.mqtt_host, self.cfg.mqtt_port, keepalive=60)
        self.client.loop_start()

    def _dewpoint_sensor_map(self) -> dict:
        return {
            self.cfg.dewpoint_indoor_temp_topic: "indoor_t",
            self.cfg.dewpoint_indoor_humidity_topic: "indoor_rh",
            self.cfg.dewpoint_outdoor_temp_topic: "outdoor_t",
            self.cfg.dewpoint_outdoor_humidity_topic: "outdoor_rh",
        }

    def _subscribe_all(self, client) -> None:
        for serial in self.devices:
            client.subscribe(f"{self.cfg.topic_prefix}/{serial}/set/+")
        if self.cfg.neuracell_enabled:
            if self.cfg.radon_source == "device":
                log.info("NeuraCell-X: radon read from meter %s (Ambientika cloud, no MQTT input).",
                         self.cfg.radon_device_serial)
            else:
                if self.cfg.radon_topic:
                    client.subscribe(self.cfg.radon_topic)
                if self.cfg.radon_alarm_topic:
                    client.subscribe(self.cfg.radon_alarm_topic)
                log.info("NeuraCell-X: radon topics subscribed (%s / %s).",
                         self.cfg.radon_topic, self.cfg.radon_alarm_topic)
        if self.cfg.dewpoint_enabled:
            if self.cfg.dewpoint_source == "computed":
                for topic in self._dewpoint_sensor_map():
                    if topic:
                        client.subscribe(topic)
                log.info("NeuraCell-X: dew-point computed from sensor topics.")
            elif self.cfg.dewpoint_source == "device":
                log.info("NeuraCell-X: dew-point read from TPS device %s (Ambientika cloud, no MQTT input).",
                         self.cfg.dewpoint_device_serial)
            else:
                if self.cfg.dewpoint_block_topic:
                    client.subscribe(self.cfg.dewpoint_block_topic)
                log.info("NeuraCell-X: dew-point block topic subscribed (%s).",
                         self.cfg.dewpoint_block_topic)

    def _on_mqtt_connect(self, client, userdata, flags, rc) -> None:
        if rc == 0:
            log.info("Connected to MQTT broker.")
            self.client.publish(bridge_avail_topic(self.cfg.topic_prefix),
                                "online", qos=0, retain=True)
            self._subscribe_all(client)
            # Publish discovery + NeuraCell-X status here (after CONNACK) so the
            # retained messages are never lost to a not-yet-connected socket.
            # Re-runs on every reconnect, which also refreshes HA auto-discovery.
            self._publish_discovery()
            self.publish_neuracell_state()
        else:
            log.error("MQTT connection failed (rc=%s).", rc)

    def _dispatch(self, coro) -> None:
        if self.loop is not None:
            asyncio.run_coroutine_threadsafe(coro, self.loop)

    def _on_mqtt_message(self, client, userdata, msg) -> None:
        try:
            payload = msg.payload.decode("utf-8", errors="replace").strip()
            topic = msg.topic

            if self.cfg.neuracell_enabled and self.cfg.radon_source != "device":
                if topic == self.cfg.radon_topic:
                    self._dispatch(self.neuracell.on_radon_value(payload)); return
                if topic == self.cfg.radon_alarm_topic:
                    self._dispatch(self.neuracell.on_radon_alarm(payload)); return

            if self.cfg.dewpoint_enabled:
                if self.cfg.dewpoint_source == "signal" and topic == self.cfg.dewpoint_block_topic:
                    self._dispatch(self.neuracell.on_dewpoint_block(payload)); return
                if self.cfg.dewpoint_source == "computed":
                    which = self._dewpoint_sensor_map().get(topic)
                    if which:
                        self._dispatch(self.neuracell.on_dewpoint_sensor(which, payload)); return

            # Device command topics: <prefix>/<serial>/set/<attr>
            parts = topic.split("/")
            if len(parts) < 4 or parts[-2] != "set":
                return
            serial = parts[-3]
            attr = parts[-1]
            log.info("Command received: serial=%s attr=%s value=%s", serial, attr, payload)
            self._dispatch(self._handle_command(serial, attr, payload))
        except Exception as e:
            log.exception("Error handling MQTT message: %s", e)

    # Which attrs form the restore baseline, and how to parse their value.
    _BASELINE_ATTRS = {
        "operating_mode": OperatingMode,
        "fan_speed": FanSpeed,
        "humidity_level": HumidityLevel,
    }

    async def _handle_command(self, serial: str, attr: str, value: str) -> None:
        device = self.devices.get(serial)
        if device is None:
            log.warning("Unknown device serial: %s", serial)
            return

        # Filter reset: fire the documented cloud reset once, quietly (like the
        # app, which shows success without checking - confirmed by decompiling
        # WebService.dll). _reset_filter sends it and reads the real status once
        # for the hidden reset-status sensor; it does not retry or alarm. Run it
        # in the background so this MQTT handler returns immediately.
        if attr == "reset_filter":
            self._start_reset(device)
            return

        # Parse the target attribute first - this needs no live status, so a
        # baseline change can be deferred even while the device is offline.
        enum_cls = self._BASELINE_ATTRS.get(attr)
        if enum_cls is None and attr != "light_sensor_level":
            log.warning("Unsupported attribute: %s", attr)
            return
        # Strict lookup on purpose: a bad payload must not create a new enum
        # member via the compatibility map, and compatibility members (e.g. the
        # reported-only fan speed "Night") must not be sent back to the API.
        parsed = strict_enum_lookup(enum_cls if enum_cls is not None
                                    else LightSensorLevel, value)
        if parsed is None:
            log.error("Invalid value %r for %s", value, attr)
            return

        # While this device is under active protection control, manual changes
        # to baseline attributes (mode/fan/humidity) must not fight the
        # controller. Record them as the new baseline (applied once protection
        # clears) instead of applying them now. Devices NOT under control (e.g.
        # units outside a targeted dew-point block) stay fully controllable.
        # Non-baseline attrs (light sensor) always pass through.
        if self.neuracell._device_under_control(serial, device) and attr in self._BASELINE_ATTRS:
            nc = self.neuracell
            if serial in nc._saved_modes:
                nc._saved_modes[serial][attr] = parsed
            else:
                nc._pending_manual.setdefault(serial, {})[attr] = parsed
            log.warning(
                "NeuraCell-X active: deferring manual %s change on %s until protection clears.",
                attr, serial,
            )
            return

        # Live change: read current status to fill the unchanged attributes.
        status = await self.read_status(device)
        if status is None:
            log.error("Cannot read current status of %s", serial)
            return
        cur_mode = status["operating_mode"]
        op = cur_mode
        fan = status["fan_speed"]
        hum = status["humidity_level"]
        light = status["light_sensor_level"]
        # A powered-off unit reports a default light-sensor level (e.g. Medium),
        # not the user's setting. Re-sending that on the next change would
        # silently overwrite the user's dusk-sensor choice (e.g. Off). So while
        # the unit is Off, keep the last light-sensor level we saw while it ran
        # (or that the user set) - unless the user is changing it right now.
        if (attr != "light_sensor_level" and cur_mode == OperatingMode.Off
                and serial in self._last_light):
            light = self._last_light[serial]
        if attr == "operating_mode":
            op = parsed
        elif attr == "fan_speed":
            fan = parsed
        elif attr == "humidity_level":
            hum = parsed
        elif attr == "light_sensor_level":
            light = parsed
            self._last_light[serial] = parsed

        mode = {
            "operating_mode": op, "fan_speed": fan,
            "humidity_level": hum, "light_sensor_level": light,
        }
        try:
            res = await device.change_mode(mode)
        except Exception as e:
            log.exception("change_mode raised for %s: %s", serial, e)
            return
        if isinstance(res, Failure):
            log.error("change_mode failed for %s: %s", serial, res)
        else:
            log.info("change_mode OK for %s", serial)


    def publish_neuracell_state(self) -> None:
        if self.client is None or not (self.cfg.neuracell_enabled or self.cfg.dewpoint_enabled):
            return
        nc = self.neuracell
        payload = {
            "radon_protection": nc.radon_active,
            "radon": nc.last_radon,
            "radon_threshold": self.cfg.radon_threshold,
            "dewpoint_block": nc.dewpoint_block,
            "dewpoint_block_devices": sorted(self.cfg.dewpoint_block_device_tokens) or "all",
            "indoor_dew_point": round(nc.indoor_dew_point, 1) if nc.indoor_dew_point is not None else None,
            "outdoor_dew_point": round(nc.outdoor_dew_point, 1) if nc.outdoor_dew_point is not None else None,
            "override_active": nc.override_active,
        }
        self.client.publish(neuracell_state_topic(self.cfg.topic_prefix),
                            json.dumps(payload), qos=0, retain=True)

    # ----- Ambientika -----
    async def _login(self) -> None:
        log.info("Authenticating with Ambientika API at %s ...", self.cfg.host)
        res = await authenticate(self.cfg.username, self.cfg.password, self.cfg.host)
        if isinstance(res, Failure):
            log.error("Authentication failed: %s", res)
            raise RuntimeError("Cannot authenticate with Ambientika API. Check username/password.")
        self.api = res.unwrap()
        self._reauth_ts = time.monotonic()
        log.info("Authentication successful.")

    async def _discover_devices(self) -> None:
        assert self.api is not None
        houses_res = await self.api.houses()
        if isinstance(houses_res, Failure):
            log.error("Could not fetch houses: %s", houses_res)
            raise RuntimeError("Could not fetch houses from Ambientika API.")
        houses = houses_res.unwrap()

        self.devices = {}
        for house in houses:
            for room in house.rooms:
                for device in room.devices:
                    self.devices[device.serial_number] = device
                    log.info("  Device: %s  (serial: %s)", device.name, device.serial_number)
        log.info("Found %d device(s).", len(self.devices))

        # dew-point source='device': take the TPS OUT of the controllable-fan set,
        # so it is never exposed/commanded as a fan - it is only read for its state.
        self.dewpoint_device = None
        if self.cfg.dewpoint_enabled and self.cfg.dewpoint_source == "device":
            serial = self.cfg.dewpoint_device_serial
            self.dewpoint_device = self.devices.pop(serial, None) if serial else None
            if self.dewpoint_device is None:
                log.error("dewpoint_source=device but TPS serial %r not found. Available serials: %s",
                          serial, ", ".join(self.devices.keys()) or "(none)")
            else:
                log.info("Dew-point source device: %s (serial %s) - read-only, not exposed as a fan.",
                         self.dewpoint_device.name, serial)

        # radon source='device': take the radon meter OUT of the controllable-fan
        # set, so it is only read for its radon state, never commanded as a fan.
        self.radon_device = None
        if self.cfg.neuracell_enabled and self.cfg.radon_source == "device":
            serial = self.cfg.radon_device_serial
            self.radon_device = self.devices.pop(serial, None) if serial else None
            if self.radon_device is None:
                log.error("radon_source=device but radon meter serial %r not found. Available serials: %s",
                          serial, ", ".join(self.devices.keys()) or "(none)")
            else:
                log.info("Radon source device: %s (serial %s) - read-only, not exposed as a fan.",
                         self.radon_device.name, serial)

    def _publish_discovery(self) -> None:
        if not self.cfg.enable_discovery or self.client is None:
            return
        for serial, device in self.devices.items():
            for topic, payload in build_discovery_configs(self.cfg, serial, device.name):
                self.client.publish(topic, json.dumps(payload), qos=0, retain=True)
        if self.cfg.neuracell_enabled or self.cfg.dewpoint_enabled:
            for topic, payload in build_neuracell_discovery(self.cfg):
                self.client.publish(topic, json.dumps(payload), qos=0, retain=True)
        for topic, payload in build_bridge_discovery(self.cfg):
            self.client.publish(topic, json.dumps(payload), qos=0, retain=True)
        log.info("HA Auto-Discovery published for all devices.")

    async def _poll_loop(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            cycle_ok = 0
            saw_auth_error = False
            for serial, device in list(self.devices.items()):
                try:
                    res = await device.status()
                    if isinstance(res, Failure):
                        try:
                            code = res.failure().get("status_code")
                        except Exception:
                            code = None
                        if code in (401, 403):
                            saw_auth_error = True
                        log.warning("status() failed for %s: %s", serial, res)
                        self._note_poll_failure(serial, "status() failed")
                        continue
                    s = res.unwrap()
                    # Remember the user's light-sensor level while the unit runs;
                    # a powered-off unit reports a default (e.g. Medium) instead.
                    if s["operating_mode"] != OperatingMode.Off:
                        self._last_light[serial] = s["light_sensor_level"]
                    # Filterstatus einmal aufloesen: der effektive Wert (mit
                    # Wartungsquittung) steht in den Hauptfeldern, der rohe
                    # Geraetewert daneben in den *_raw-Feldern. Ohne aktive
                    # Quittung sind beide identisch.
                    fs_raw = s["filters_status"]
                    fs_eff = self._filter_ack_effective(serial, fs_raw)
                    payload = {
                        "operating_mode": s["operating_mode"].name,
                        "fan_speed": s["fan_speed"].name,
                        "humidity_level": s["humidity_level"].name,
                        "light_sensor_level": s["light_sensor_level"].name,
                        "temperature": s["temperature"],
                        "humidity": s["humidity"],
                        "air_quality": s["air_quality"],
                        "humidity_alarm": s["humidity_alarm"],
                        "filters_status": fs_eff,
                        "filters_status_raw": fs_raw,
                        "night_alarm": s["night_alarm"],
                        "device_role": s["device_role"],
                        "last_operating_mode": s["last_operating_mode"].name,
                        "zone_index": device.zone_index,
                        # --- numerische Begleitwerte (Zahl je Textwert) ---
                        "operating_mode_num": _enum_num(s["operating_mode"]),
                        "last_operating_mode_num": _enum_num(s["last_operating_mode"]),
                        "fan_speed_num": _fan_speed_num(s["fan_speed"]),
                        "humidity_level_num": _enum_num(s["humidity_level"]),
                        "light_sensor_level_num": _enum_num(s["light_sensor_level"]),
                        "air_quality_num": air_quality_to_num(s["air_quality"]),
                        "filter_status_num": filter_status_to_num(fs_eff),
                        "filter_status_raw_num": filter_status_to_num(fs_raw),
                    }
                    if self.client is not None:
                        self.client.publish(state_topic(self.cfg.topic_prefix, serial),
                                            json.dumps(payload), qos=0, retain=True)
                        self.client.publish(avail_topic(self.cfg.topic_prefix, serial),
                                            "online", qos=0, retain=True)
                    self._note_poll_success(serial)
                    cycle_ok += 1
                except Exception as e:
                    # An exception here used to publish nothing at all, so the
                    # entity silently kept its last retained values and looked
                    # live in the dashboard. Count it like any other failed poll.
                    log.exception("Error polling %s: %s", serial, e)
                    self._note_poll_failure(serial, "exception during poll")

            # Token hygiene: re-auth on an explicit 401/403, if a whole cycle of
            # >=2 devices produced no success (an expired token looks like this), or
            # proactively every REAUTH_INTERVAL. _reauth never raises.
            reason = None
            if saw_auth_error:
                reason = "401"
            elif len(self.devices) >= 2 and cycle_ok == 0:
                reason = "all-devices-failed"
            elif (time.monotonic() - self._reauth_ts) >= REAUTH_INTERVAL:
                reason = "periodic"
            if reason and self.devices:
                await self._reauth(reason)
            
            # Read the radon meter (source='device') FIRST - radon has priority
            # over dew point, so its state must be current before we enforce.
            if (self.cfg.neuracell_enabled and self.cfg.radon_source == "device"
                    and self.radon_device is not None):
                try:
                    await self.neuracell.poll_radon_device(self.radon_device)
                except Exception as e:
                    log.exception("NeuraCell-X radon device poll error: %s", e)

            # Read the TPS device (source='device') and derive the block.
            if (self.cfg.dewpoint_enabled and self.cfg.dewpoint_source == "device"
                    and self.dewpoint_device is not None):
                try:
                    await self.neuracell.poll_dewpoint_device(self.dewpoint_device)
                except Exception as e:
                    log.exception("NeuraCell-X dew-point device poll error: %s", e)

            # Keep asserting the active protection state.
            try:
                await self.neuracell.enforce()
            except Exception as e:
                log.exception("NeuraCell-X enforce error: %s", e)

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.cfg.poll_interval)
            except asyncio.TimeoutError:
                pass

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()

        await self._login()
        await self._discover_devices()
        self._mqtt_connect()
        # Subscriptions, discovery and NeuraCell-X status are (re)published from
        # the on_connect callback once the CONNACK is received (see _on_mqtt_connect).
        # Resume any filter reset that was still unconfirmed before a restart or
        # update, and give every other device a defined reset-status value.
        self._load_pending()
        for serial in list(self._pending_resets):
            device = self.devices.get(serial)
            if device is not None:
                log.info("resuming unconfirmed filter reset for %s after restart", serial)
                self._start_reset(device)
            else:
                self._pending_resets.discard(serial)
        self._save_pending()
        for serial in self.devices:
            if serial not in self._reset_inflight:
                self._publish_reset_state(serial, "idle")

        log.info("Starting poll loop (every %ss) ...", self.cfg.poll_interval)
        if self.cfg.neuracell_enabled:
            if self.cfg.radon_source == "device":
                rsrc = "device(serial=%s, field=%s)" % (
                    self.cfg.radon_device_serial or "?", self.cfg.radon_device_alarm_field)
            else:
                rsrc = "signal(threshold=%d Bq/m3)" % self.cfg.radon_threshold
            log.info("NeuraCell-X radon: source=%s -> alarm => Intake/%s (highest priority).",
                     rsrc, self.cfg.radon_protection_fan)
        if self.cfg.dewpoint_enabled:
            scope = ", ".join(sorted(self.cfg.dewpoint_block_device_tokens)) or "ALL devices"
            src = self.cfg.dewpoint_source
            if src == "device":
                block_modes = "/".join(m.name for m in sorted(
                    self.cfg.dewpoint_device_block_mode_set, key=lambda x: x.value))
                src = "device(serial=%s, block-when-mode=%s)" % (
                    self.cfg.dewpoint_device_serial or "?", block_modes)
            log.info("NeuraCell-X dew point: source=%s, scope=%s -> block => Off (radon has priority).",
                     src, scope)
        await self._poll_loop()

    def stop(self) -> None:
        log.info("Shutting down ...")
        if self._stop_event is not None and self.loop is not None:
            self.loop.call_soon_threadsafe(self._stop_event.set)
        if self.client is not None:
            try:
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def load_config() -> BridgeConfig:
    ha_options = "/data/options.json"
    yaml_path = os.environ.get("CONFIG_YAML", "config.yaml")
    if os.path.exists(ha_options):
        return BridgeConfig.from_ha_options(ha_options)
    if os.path.exists(yaml_path):
        return BridgeConfig.from_yaml(yaml_path)
    return BridgeConfig.from_env()


def main() -> None:
    cfg = load_config()
    logging.basicConfig(
        level=getattr(logging, str(cfg.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
    )
    _banner = ("Ambientika MQTT Bridge v%s" % _BRIDGE_VERSION) if _BRIDGE_VERSION else "Ambientika MQTT Bridge"
    log.info("=== %s (NeuraCell-X: radon + dew point)  starting ===", _banner)
    log.info("API host      : %s", cfg.host)
    log.info("MQTT broker   : %s:%s", cfg.mqtt_host, cfg.mqtt_port)
    log.info("Topic prefix  : %s", cfg.topic_prefix)
    log.info("Poll interval : %ss", cfg.poll_interval)

    if not cfg.username or not cfg.password:
        log.error("Ambientika username/password missing. Set them in the add-on options or config.yaml.")
        sys.exit(1)

    bridge = AmbientikaBridge(cfg)

    def _handle_signal(*_):
        bridge.stop()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        bridge.stop()
    except Exception as e:
        log.exception("Fatal error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
