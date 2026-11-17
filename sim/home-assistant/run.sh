#!/bin/bash
# Live test in a real Home Assistant, driven from inside by a small custom component.
#
#   sim/home-assistant/run.sh bridge       [result.json]   bridge.py -> Mosquitto -> MQTT integration
#   sim/home-assistant/run.sh integration <custom_components/ambientika> [result.json]
#                                                          the integration "Ambientika for Home Assistant"
#
# Needs: hass (pip install homeassistant paho-mqtt; for the integration also its
# requirements from manifest.json), mosquitto, and python with requirements.txt.
#   HASS=/path/to/hass  PYTHON=/path/to/python  MOSQUITTO=/path/to/mosquitto
#
# bridge:      the real bridge.py runs against sim/fake_cloud.py; Home Assistant sets up
#              its MQTT integration through the normal config flow. Checked: devices,
#              entities, states, commands (select, button), a unit that drops out, the
#              bridge stopped and killed.
# integration: the released integration is copied unchanged except for DEFAULT_HOST,
#              which points at sim/fake_cloud.py. Checked: config flow, entities,
#              climate services, button, outages, how often the cloud is asked,
#              requests the cloud never answers, reload. SIM_SCENARIO=same-name
#              gives two units the same name.
#
# Updates of the integration (an old version sets an installation up, a new one
# takes over) are tested by sim/home-assistant/upgrade_test.py.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; REPO="$(cd "$HERE/../.." && pwd)"
HASS="${HASS:-hass}"; PYTHON="${PYTHON:-python3}"; MOSQUITTO="${MOSQUITTO:-mosquitto}"
MODE="${1:?bridge or integration}"; shift
W="$(mktemp -d /tmp/ambientika-ha-sim-XXXXXX)"; chmod 755 "$W"
port() { "$PYTHON" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])'; }
CP="$(port)"; BP="$(port)"; HP="$(port)"
"$PYTHON" "$REPO/sim/fake_cloud.py" "$CP" > "$W/cloud.log" 2>&1 & CLOUD_PID=$!
C="$W/config"; mkdir -p "$C"
if [ "$MODE" = "bridge" ]; then
  RESULT="${1:-$W/result.json}"
  printf 'listener %s 127.0.0.1\nallow_anonymous true\npersistence false\n' "$BP" > "$W/mosquitto.conf"
  "$MOSQUITTO" -c "$W/mosquitto.conf" > "$W/mosquitto.log" 2>&1 & BROKER_PID=$!
  cp -r "$HERE/bridge/." "$C/"
else
  SRC="${1:?path to custom_components/ambientika}"; RESULT="${2:-$W/result.json}"; BROKER_PID=""
  cp -r "$HERE/integration/." "$C/"
  cp -r "$SRC" "$C/custom_components/ambientika"
  sed -i "s#^DEFAULT_HOST = .*#DEFAULT_HOST = \"http://127.0.0.1:$CP\"#" "$C/custom_components/ambientika/const.py"
fi
sed -i "s#server_port: .*#server_port: $HP#" "$C/configuration.yaml"
sleep 1.5
SIM_BROKER_PORT="$BP" SIM_CLOUD_URL="http://127.0.0.1:$CP" SIM_BRIDGE="$REPO/bridge.py" SIM_PYTHON="$PYTHON" \
  SIM_HA_RESULT="$RESULT" SIM_WORK="$W" SIM_SCENARIO="${SIM_SCENARIO:-}" timeout 900 "$HASS" -c "$C" > "$W/hass.log" 2>&1
HASS_RC=$?
kill $CLOUD_PID $BROKER_PID 2>/dev/null
if [ ! -s "$RESULT" ]; then
  echo "Home Assistant ended (exit code $HASS_RC) without a result of the test driver. The end of its log:"
  tail -n 60 "$W/hass.log"
  echo "Home Assistant log and work folder: $W"
  exit 1
fi
"$PYTHON" - "$RESULT" <<'PY'
import json, sys
rows = json.load(open(sys.argv[1], encoding="utf-8"))
for r in rows:
    print(("  PASS " if r["ok"] else "  FAIL ") + r["name"] + ("" if r["ok"] and not r["name"].startswith("OBSERVATION") else "  <<< " + str(r["detail"])))
bad = [r for r in rows if not r["ok"]]
print("\nRESULT: %d checks, %s" % (len(rows), "ALL PASS" if not bad else "FAILED %d" % len(bad)))
sys.exit(1 if bad else 0)
PY
RC=$?
if [ $RC -ne 0 ]; then
  echo "The log of Home Assistant without the lines of the test driver (last 80 lines):"
  grep -v "simtest[a-z_]*\] \(PASS\|FAIL\)" "$W/hass.log" | tail -n 80 | cut -c1-400
fi
echo "Home Assistant log and work folder: $W"
exit $RC
