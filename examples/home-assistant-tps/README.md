# Ambientika Taupunktsteuerung ↔ SMART/OFFICE — Kopplung über Home Assistant / MQTT

Diese Anleitung schließt die letzte Lücke: Sie bringt das **„Nicht lüften"-Signal der Taupunktsteuerung (TPS)** auf das MQTT-Topic, das die Ambientika-Bridge auswertet. Dann schaltet die Bridge automatisch **SMART und OFFICE** ab (Modus `Off`) und stellt sie bei Freigabe wieder auf den vorherigen Modus zurück.

Voraussetzung ist der PR mit der neuen Option `dewpoint_block_devices` (Bridge v1.4.0, NeuraCell-X). Die eigentliche Block-Logik steckt bereits in der Bridge — hier bauen wir nur den **Eingang**.

## Funktionsprinzip

```
   TPS "nicht lüften"  ─►  Kontakt/Signal  ─►  MQTT  ambientika/dewpoint/block = "block"
                                                        │
                                              Ambientika-Bridge
                                                        │
                                        SMART + OFFICE  ─►  Modus Off
   TPS gibt frei       ─►  ...           = "clear"  ─►  Geräte auf vorherigen Modus zurück
```

Die Bridge deutet als „sperren" jeden dieser Nutzlast-Werte: `block`, `blocked`, `on`, `true`, `1`, `yes`, `active`, `alarm`. Alles andere (z. B. `clear`, `off`) heißt „freigeben". Wir senden bewusst `block` / `clear`.

## Voraussetzung an der TPS: ein potenzialfreier Kontakt

Damit HA/MQTT den TPS-Zustand kennt, braucht es ein **potenzialfreies (spannungsfreies) Signal**, das anzeigt „TPS blockiert die Lüftung". In der Praxis:

- **Funkempfänger SW10026** oder Schaltausgang der TPS mit potenzialfreiem Kontakt: ideal. Kontakt schließt, wenn die TPS sperrt.
- **TPS schaltet nur die Netzspannung der Lüfter** (Standard-Auslieferung ohne Funkempfänger): dann gibt es keinen sauberen Zustandskontakt. Zwei Wege: (a) ein potenzialfreies Relais/Optokoppler, das die Lüfter-Versorgungsleitung abgreift — Achtung, dann ist die Logik **invertiert** (Spannung weg = blockiert); (b) besser den Funkempfänger nachrüsten und dessen Kontakt nutzen.

> Elektrischer Anschluss an 230 V nur durch eine Elektrofachkraft. Der ESP-/HA-Eingang bleibt strikt **potenzialfrei** (nur trockener Kontakt gegen GND).

Wähle danach **eine** der drei Varianten.

---

## Variante A — ESPHome, direkt (empfohlen)

Datei: `esphome_ambientika_tps.yaml`

Ein kleiner ESP32 liest den TPS-Kontakt an GPIO23 und publiziert **direkt** an die Bridge — Home Assistant ist für die Funktion nicht nötig (robust, unabhängig von HA-Neustarts).

1. In `secrets.yaml` (ESPHome) `wifi_ssid`, `wifi_password`, `fallback_password`, `mqtt_broker`, `mqtt_user`, `mqtt_password` setzen — **gleicher Broker wie die Bridge**.
2. TPS-Kontakt zwischen **GPIO23 und GND** anschließen (interner Pullup ist aktiv).
3. Flashen. Der Knoten sendet `block`/`clear` (retained) und synct bei jedem MQTT-Connect neu.
4. Zwei Test-Buttons („block senden" / „clear senden") sind eingebaut.

Logik-Umkehr (falls der Kontakt bei deiner TPS umgekehrt schaltet): in der Datei `inverted: true` → `false`.

---

## Variante B — Home-Assistant-Automation

Datei: `ha_package_ambientika_tps.yaml`

Nimm diese, wenn der TPS-Kontakt bereits als HA-Entität vorliegt (Shelly, KNX, Modbus, wired input, MQTT …).

1. Datei nach `<config>/packages/ambientika_tps.yaml` legen und Packages aktivieren:
   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```
2. In der Automation `binary_sensor.tps_ventilation_block` durch deine reale Entität ersetzen — **oder** den mitgelieferten MQTT-Binärsensor nutzen und dessen `state_topic` auf deine Kontaktquelle setzen.
3. Die Automation spiegelt den Zustand (retained) und re-synct nach HA-Neustart. Ein Test-Schalter `input_boolean.tps_test_block` ist dabei.

---

## Variante C — „computed" (kein TPS-Kontakt, dafür Sensoren)

Datei: `ha_computed_dewpoint.yaml`

Kein Kontakt, aber Temperatur/Feuchte innen und außen vorhanden? Dann rechnet die Bridge den Taupunkt selbst.

1. In der Automation die vier Sensor-Entitäten anpassen.
2. Im Add-on `dewpoint_source: computed` setzen (statt `signal`).
3. Die Bridge sperrt, wenn der Außen-Taupunkt ≥ Innen-Taupunkt − `dewpoint_margin` ist (lüften würde Feuchte reinholen). `dewpoint_margin` / `dewpoint_hysteresis` steuern das Verhalten.

---

## Ambientika-Bridge / Add-on konfigurieren

Für Variante A oder B (Signal):

```yaml
dewpoint_enabled: true
dewpoint_source: "signal"
dewpoint_block_topic: "ambientika/dewpoint/block"
dewpoint_block_devices: "SMART,OFFICE"     # exakt eure Gerätenamen; leer = alle
```

Für Variante C zusätzlich `dewpoint_source: "computed"` und die vier `dewpoint_*_topic` wie voreingestellt lassen.

Hinweis zu den Gerätenamen: Der Abgleich ist **groß/klein egal** und trifft **Gerätename ODER Seriennummer**. Wie eure Einheiten heißen, steht im Ambientika-Konto bzw. im Bridge-Log („Device: … (serial: …)").

## Test / Abnahme

1. Add-on mit obiger Konfiguration starten. Im Log muss stehen:
   `NeuraCell-X dew point: source=signal, scope=smart, office -> block => Off`.
2. „block" auslösen (Test-Button in A, `input_boolean` in B, oder echten TPS-Kontakt schließen).
3. Prüfen: **SMART und OFFICE gehen auf `Off`** (in der App / in HA), **andere Einheiten laufen weiter**.
4. „clear" auslösen → SMART und OFFICE kehren auf den **vorherigen Modus** zurück.
5. In HA erscheint über Auto-Discovery der Sensor **„Ventilation Blocked (Dew Point)"** (Gerät NeuraCell-X) — der spiegelt den Blockzustand.

## Sicherheits- und Verhaltenshinweise

- **Radon hat Vorrang:** Ein aktiver Radon-Alarm überschreibt den Taupunkt-Block (Geräte laufen dann in Zuluft, Stufe 1). Das ist so gewollt.
- **Nur Zielgeräte:** Der Block betrifft ausschließlich die unter `dewpoint_block_devices` genannten Einheiten; alle anderen bleiben an und in HA frei steuerbar.
- **Wiederherstellung:** Die Bridge merkt sich den Modus vor dem Block und stellt ihn bei Freigabe wieder her (inkl. zwischenzeitlicher manueller Änderungen an den Zielgeräten).
- **Retained:** Alle Signale werden „retained" gesendet, damit Bridge und Eingang nach einem Neustart synchron sind.
- 230-V-Anschluss nur durch eine Elektrofachkraft; der Steuer-Eingang bleibt potenzialfrei.
