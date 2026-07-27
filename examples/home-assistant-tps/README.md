# Ambientika Taupunktsteuerung ↔ SMART/OFFICE — Kopplung über Home Assistant / MQTT

Diese Anleitung schließt die letzte Lücke: Sie bringt die **Ventilations-Entscheidung der Taupunktsteuerung (TPS)** auf das MQTT-Topic, das die Ambientika-Bridge auswertet. Dann schaltet die Bridge automatisch **SMART und OFFICE** ab (Modus `Off`) und stellt sie bei Freigabe wieder auf den vorherigen Modus zurück.

Voraussetzung ist der PR mit der neuen Option `dewpoint_block_devices` (Bridge v1.4.0, NeuraCell-X). Die Block-Logik steckt bereits in der Bridge — hier bauen wir nur den **Eingang**.

## Wie die TPS wirklich schaltet (wichtig)

TPS und Funkempfänger liefern **keinen** potenzialfreien Zustandskontakt. Sie schalten die **Netzspannung** der Lüfterleitung:

```
   Netz VORHANDEN  ─►  lüften erlaubt            ─►  "clear"
   Netz WEG        ─►  TPS sperrt (Relais offen) ─►  "block"  ─►  SMART/OFFICE aus
```

Wir erfassen deshalb nicht einen Kontakt, sondern **ob auf der TPS-geschalteten Leitung Netzspannung anliegt**. Die Logik ist damit invertiert: *Netz da = clear, Netz weg = block*.

> Damit die Bridge SMART/OFFICE überhaupt schalten kann, müssen diese an **Dauerstrom** hängen (eigene, permanente Versorgung) — nicht an der von der TPS geschalteten Leitung. Liegen sie auf der geschalteten Leitung, gehen sie mit dem Netz ohnehin aus und brauchen keine Kopplung.

## Zwei Grundwege

- **Hardwarefrei (Variante D, empfohlen):** Hängt die TPS im selben Ambientika-Konto, liest die Bridge ihren Zustand direkt aus der Cloud — kein Relais, kein ESP. Siehe unten.
- **Netz-Präsenz erfassen (Varianten A/B/C):** Falls die TPS *nicht* im Konto ist, wird stattdessen erfasst, ob auf der geschalteten Leitung Netzspannung anliegt. Das folgende Kapitel gilt nur für diesen Weg.

## Netz-Präsenz erfassen — Hardware für die Varianten A/B/C

- **Koppelrelais (empfohlen, elektrikerüblich):** ein Installations-/Koppelrelais mit **230-V-AC-Spule** parallel zur geschalteten Lüfter-Phase. Der potenzialfreie Kontakt geht an den Steuer-Eingang. Netz da → Spule an → Kontakt geschlossen. Robust, prellarm, potenzialfrei.
  ```
  [TPS geschaltete Phase L] ──┐
                              │  230-V-Spule Koppelrelais
  [Neutralleiter N] ──────────┘
       Kontakt (potenzialfrei, Schließer):  COM → GPIO/Eingang,  NO → GND
  ```
- **AC-Präsenz-Optokoppler-Modul:** kompakte Alternative direkt auf einen GPIO. Achtung: liefert bei anliegendem Netz einen 100-Hz-Impuls — in der Firmware entprellen/filtern (die mitgelieferte `delayed_on/off`-Zeit deckt das ab).
- **Leistungs-Messsteckdose:** wenn die TPS-geschaltete Last steckbar ist, eine messende Smart-Steckdose davor. Leistung > 0 = Netz vorhanden. Kein Elektriker nötig; nur bei steckbarer Last.

Zwei Pflicht-Punkte in allen Varianten:
1. **Erfassungs-Knoten an Dauerstrom** (nicht an der geschalteten Leitung) — sonst stirbt er genau dann, wenn er „block" senden müsste.
2. **Fail-safe** (in Variante A eingebaut): fällt der Knoten aus, wird „block" gesetzt (im Zweifel *nicht* lüften). Beim Reconnect wird der echte Zustand sofort wieder gesendet.

Wähle danach **eine** Variante. Empfehlungsreihenfolge: **D** (keine Hardware, wenn die TPS im Konto hängt), sonst **A** (Relais, garantiert), dann B/C.

---

## Variante D — hardwarefrei (empfohlen, wenn die TPS im Ambientika-Konto ist)

Die TPS hängt per WLAN im selben Ambientika-Konto wie SMART und OFFICE. Die Bridge liest ihren Zustand daher **direkt aus der Ambientika-Cloud** — ohne Relais, ohne ESP, ohne zusätzliche Verkabelung.

1. **TPS-Seriennummer finden:** das Add-on einmal starten und ins **Log** schauen. Jedes gefundene Gerät steht dort als `Device: <name> (serial: <serial>)`. Das Gerät neben SMART und OFFICE ist die TPS. (Alternativ listet das mitgelieferte `ambientika_probe.py` alle Geräte auf.)
2. **Im Add-on setzen:**
   ```yaml
   dewpoint_enabled: true
   dewpoint_source: "device"
   dewpoint_device_serial: "<TPS-Seriennummer>"
   dewpoint_device_block_modes: "Off"      # Modus/Modi, in denen die TPS sperrt
   dewpoint_block_devices: "SMART,OFFICE"
   ```
3. **Fertig.** Die Bridge liest bei jedem Poll den TPS-Status, nimmt die TPS **aus dem Lüfter-Satz heraus** (sie wird nie selbst als Lüfter geschaltet) und setzt SMART/OFFICE auf `Off`, sobald die TPS in einem der `dewpoint_device_block_modes` steht (Standard: `Off`).

**Kontrolle beim ersten Lauf (eine Log-Zeile genügt):** Im Log erscheint beim Start `Dew-point source device: <name> (serial ...)` und bei jedem Umschalten `NeuraCell-X: dew-point ventilation BLOCKED (fans off)` bzw. `released`. Steht die TPS beim Sperren in einem anderen Modus als `Off`, einfach `dewpoint_device_block_modes` anpassen (z. B. `"Off,Expulsion"`). Ändert die TPS ihren Modus laut Log gar nicht, nimm **Variante A** (Relais) — die funktioniert unabhängig davon garantiert.

---

## Variante A — ESPHome + Relais (garantierter Rückfall)

Datei: `esphome_ambientika_tps.yaml`

Ein ESP32 erfasst über das Koppelrelais die Netz-Präsenz an GPIO23 und publiziert **direkt** an die Bridge — Home Assistant ist für die Funktion nicht nötig (robust, HA-unabhängig, mit Fail-safe).

1. In `secrets.yaml` (ESPHome) `wifi_ssid`, `wifi_password`, `fallback_password`, `mqtt_broker`, `mqtt_user`, `mqtt_password` setzen — **gleicher Broker wie die Bridge**.
2. Koppelrelais-Kontakt zwischen **GPIO23 und GND** (Schließer, geschlossen = Netz vorhanden). ESP an **Dauerstrom**.
3. Flashen. Der Knoten sendet `clear` (Netz da) bzw. `block` (Netz weg), retained, und synct bei jedem MQTT-Connect neu. Fail-safe via MQTT-Testament ist aktiv.
4. Zwei Test-Buttons („block/clear senden") sind eingebaut.

Andere Kontaktlogik (Öffner statt Schließer)? In der Datei `inverted: true` → `false`.

---

## Variante B — Home-Assistant-Automation

Datei: `ha_package_ambientika_tps.yaml`

Nutze diese, wenn die Netz-Präsenz bereits als HA-Entität vorliegt — z. B. Koppelrelais-Kontakt über Shelly i4/KNX/wired input, oder eine Leistungs-Messsteckdose (Template-Binärsensor: Leistung > Schwelle).

1. Datei nach `<config>/packages/ambientika_tps.yaml` legen und Packages aktivieren:
   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```
2. `binary_sensor.tps_mains_present` auf deine reale Quelle anpassen (die Datei enthält Beispiele für Steckdose und MQTT-Kontakt). Entität ist **AN, wenn Netz vorhanden**.
3. Die Automation invertiert korrekt: Netz da → `clear`, Netz weg → `block`; retained; Re-sync nach HA-Neustart. Test-Schalter `input_boolean.tps_test_block` dabei.

---

## Variante C — „computed" (keine TPS-Erfassung, dafür Sensoren)

Datei: `ha_computed_dewpoint.yaml`

Ganz ohne TPS-Erfassung: Bei vorhandenen Temperatur/Feuchte-Sensoren innen und außen rechnet die Bridge den Taupunkt selbst.

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

Für Variante D `dewpoint_source: "device"` mit `dewpoint_device_serial` (aus dem Add-on-Log) und optional `dewpoint_device_block_modes` (Standard `Off`) — siehe Variante D oben.

Hinweis zu den Gerätenamen: Der Abgleich ist **groß/klein egal** und trifft **Gerätename ODER Seriennummer**. Wie eure Einheiten heißen, steht im Ambientika-Konto bzw. im Bridge-Log („Device: … (serial: …)").

## Test / Abnahme

1. Add-on mit obiger Konfiguration starten. Im Log muss stehen:
   `NeuraCell-X dew point: source=signal, scope=smart, office -> block => Off`.
2. Netz auf der TPS-Leitung wegnehmen (TPS sperren lassen) **oder** in Variante A den „block"-Test-Button / in Variante B den `input_boolean` nutzen.
3. Prüfen: **SMART und OFFICE gehen auf `Off`** (App / HA), **andere Einheiten laufen weiter**.
4. Netz wieder anlegen → SMART und OFFICE kehren auf den **vorherigen Modus** zurück.
5. In HA erscheint über Auto-Discovery der Sensor **„Ventilation Blocked (Dew Point)"** (Gerät NeuraCell-X) — er spiegelt den Blockzustand.

## Sicherheits- und Verhaltenshinweise

- **Dauerstrom für SMART/OFFICE und den Erfassungs-Knoten** — nur die (dummen) TPS-geschalteten Lüfter hängen an der geschalteten Leitung.
- **Fail-safe:** Bei Ausfall des Erfassungs-Knotens wird „block" gesetzt (nicht lüften). Bewusst so gewählt, da für den Feuchteschutz das Nicht-Lüften die sichere Richtung ist; die Einheiten laufen nach Reconnect wieder normal.
- **Radon hat Vorrang:** Ein aktiver Radon-Alarm überschreibt den Taupunkt-Block (Geräte laufen dann in Zuluft, Stufe 1). Das ist so gewollt.
- **Nur Zielgeräte:** Der Block betrifft ausschließlich die unter `dewpoint_block_devices` genannten Einheiten; alle anderen bleiben an und in HA frei steuerbar.
- **Wiederherstellung:** Die Bridge merkt sich den Modus vor dem Block und stellt ihn bei Freigabe wieder her (inkl. zwischenzeitlicher manueller Änderungen an den Zielgeräten).
- **Retained:** Alle Signale werden „retained" gesendet, damit Bridge und Eingang nach einem Neustart synchron sind.
- 230-V-Anschluss (Relaisspule) nur durch eine Elektrofachkraft; der Steuer-Eingang bleibt potenzialfrei.
