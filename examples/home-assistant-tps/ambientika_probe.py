#!/usr/bin/env python3
# =============================================================================
# Ambientika-Konto-Prüfer  –  zeigt ALLE Geräte + ihren Live-Status
# =============================================================================
# Zweck: herausfinden, ob die Taupunktsteuerung (TPS) als eigenes Gerät im
# Ambientika-Konto auftaucht, welchen `deviceType` sie hat und in WELCHEM
# Statusfeld ihr "lüften / nicht lüften" steht. Genau das braucht die Bridge
# für die hardwarefreie Kopplung.
#
# Spricht ausschliesslich die offizielle Ambientika-Cloud an (HTTPS), exakt
# die Endpunkte, die die Bridge/`ambientika_py` benutzt. Es werden KEINE
# Passwörter ausgegeben.
#
# Nutzung (lokal, wo ihr die Zugangsdaten habt):
#   pip install requests           # falls noch nicht vorhanden
#   export AMBIENTIKA_USERNAME="euer-login"
#   export AMBIENTIKA_PASSWORD="euer-passwort"
#   python3 ambientika_probe.py
#
# WICHTIG für die exakte Feld-Zuordnung:
#   Skript ZWEIMAL laufen lassen – einmal während die TPS LÜFTEN lässt und
#   einmal während sie SPERRT – und die beiden Ausgaben vergleichen. Das Feld,
#   das sich bei der TPS ändert, ist der gesuchte "Block"-Indikator.
# =============================================================================
import json
import os
import sys

try:
    import requests
except ImportError:
    print("Bitte zuerst:  pip install requests")
    sys.exit(1)

HOST = os.environ.get("AMBIENTIKA_HOST", "https://app.ambientika.eu:4521")
USER = os.environ.get("AMBIENTIKA_USERNAME") or os.environ.get("AMBIENTIKA_USER")
PW = os.environ.get("AMBIENTIKA_PASSWORD") or os.environ.get("AMBIENTIKA_PASS")

# Nach Namen: was sind vermutlich die Lüfter, was ist vermutlich die TPS?
FAN_HINTS = ("smart", "office")
TPS_HINTS = ("taupunkt", "dew", "tps", "keller", "cellar")


def _get(session, token, path, params=None):
    r = session.get(f"{HOST}/{path}", headers={"Authorization": f"Bearer {token}"},
                    params=params or {}, timeout=30)
    r.raise_for_status()
    return r.json() if r.content and "application/json" in r.headers.get("content-type", "") else r.text


def main():
    if not USER or not PW:
        print("Bitte AMBIENTIKA_USERNAME und AMBIENTIKA_PASSWORD als Umgebungsvariablen setzen.")
        sys.exit(1)

    s = requests.Session()
    # --- Login ---
    r = s.post(f"{HOST}/users/authenticate", json={"username": USER, "password": PW}, timeout=30)
    if r.status_code != 200:
        print(f"Login fehlgeschlagen (HTTP {r.status_code}): {r.text[:300]}")
        sys.exit(1)
    token = r.json()["jwtToken"]
    print("Login OK\n")

    # --- Häuser + Räume + Geräte ---
    houses = _get(s, token, "house/houses-info")
    devices = []
    for h in houses:
        info = _get(s, token, "house/house-complete-info", {"houseId": h["houseId"]})
        for room in info.get("rooms", []):
            for d in room.get("devices", []):
                d["_room"] = room.get("name")
                devices.append(d)

    print(f"{len(devices)} Gerät(e) im Konto gefunden:\n")
    print(f"{'name':<22}{'deviceType':<20}{'role':<12}{'raum':<14}serial")
    print("-" * 90)
    for d in devices:
        print(f"{str(d.get('name'))[:21]:<22}{str(d.get('deviceType'))[:19]:<20}"
              f"{str(d.get('role'))[:11]:<12}{str(d.get('_room'))[:13]:<14}{d.get('serialNumber')}")

    # --- Live-Status jedes Geräts (der interessante Teil) ---
    print("\n\n=== Live-Status je Gerät (device/device-status) ===")
    for d in devices:
        name = str(d.get("name"))
        low = name.lower()
        tag = ""
        if any(h in low for h in FAN_HINTS):
            tag = "  <-- vermutlich LÜFTER"
        if any(h in low for h in TPS_HINTS) or (d.get("deviceType") and "smart" not in str(d.get("deviceType")).lower()):
            tag = "  <-- KANDIDAT TPS / prüfen"
        print(f"\n--- {name}  (type={d.get('deviceType')}, serial={d.get('serialNumber')}){tag}")
        try:
            st = _get(s, token, "device/device-status", {"deviceSerialNumber": d.get("serialNumber")})
            print(json.dumps(st, indent=2, ensure_ascii=False))
        except Exception as e:
            print(f"  Status nicht lesbar: {e}")

    print("\nFertig. Bitte diese Ausgabe (ohne sensible Daten) teilen — daraus baue ich die")
    print("hardwarefreie Variante: welcher deviceType die TPS ist und welches Feld 'block' anzeigt.")


if __name__ == "__main__":
    main()
