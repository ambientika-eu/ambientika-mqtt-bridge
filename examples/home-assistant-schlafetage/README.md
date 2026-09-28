# Home-Assistant-Paket: Schlafetage mit Abend- und Nachtprogramm

Fertiges Paket für eine Etage mit mehreren Ambientika SMART/OFFICE über die Ambientika MQTT Bridge, mit NeuraCell-X-Radonvorrang.

## Was es macht

- **Abends:** 30 Minuten Querlüftung (Luftfluss Master → Slave, Stufe High), danach die ganze Nacht Wärmerückgewinnung auf Stufe Low.
- **Morgens:** zurück in den sensorgeführten SMART-Betrieb.
- **Wochentage:** Montag bis Freitag und Samstag/Sonntag haben getrennte Uhrzeiten, alle vier sind im Dashboard einstellbar.
- **Button „Schlafetage 20 min durchlüften“:** für jedes Familienmitglied. Danach läuft die Etage wieder wie vorher.
- **Handy-Hinweis:** wenn der Radonschutz anspringt oder endet.
- **Radonvorrang:** Solange NeuraCell-X den Radonschutz aktiv hat, pausieren alle Automationen.

## Einbindung

1. In der `configuration.yaml` einmalig eintragen:
   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```
2. `ambientika_schlafetage.yaml` nach `config/packages/` kopieren.
3. Im Skript `lueftung_schlafetage_setzen` die Liste `master` anpassen (dieselbe Liste auch im Button-Skript). Dort gehören nur die **Master**-Geräte der Etage hinein, geschrieben wie der Anfang ihrer Entitäts-ID. Slaves folgen ihrem Master automatisch.
4. Entwicklerwerkzeuge → YAML → Konfiguration prüfen, dann Home Assistant neu starten.

## Die richtigen Namen für `master`

Die Namen stehen unter Entwicklerwerkzeuge → Zustände, Filter `_mode`. Aus `select.buro_l_mode` wird der Eintrag `buro_l`.

Wichtig: Home Assistant behält die Entitäts-ID, auch wenn das Gerät später in der Ambientika-App umbenannt wird. Maßgeblich ist immer die ID, nicht der angezeigte Name.

## Hinweise

- `Lüftung Schlafetage setzen` ist ein Baustein für die Automationen und den Button. Direkt ausgeführt ohne Modus und Stufe, bricht es mit einem verständlichen Hinweis ab.
- Die Uhrzeiten haben `initial`-Werte, die nach jedem Neustart gelten. Wer sie im Dashboard dauerhaft ändern will, löscht nach dem ersten Start die `initial`-Zeilen.
