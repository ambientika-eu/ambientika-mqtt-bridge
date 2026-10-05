# Home-Assistant-Paket: Schlafetage mit Abend- und Nachtprogramm

Fertiges Paket für eine Etage mit mehreren Ambientika SMART/OFFICE über die Ambientika MQTT Bridge, mit NeuraCell-X-Radonvorrang.

## Was es macht

- **Abends:** 30 Minuten Querlüftung (Luftfluss Master → Slave, Stufe High), danach die ganze Nacht Wärmerückgewinnung auf Stufe Low.
- **Morgens:** zurück in den sensorgeführten SMART-Betrieb.
- **Wochentage:** Montag bis Freitag und Samstag/Sonntag haben getrennte Uhrzeiten, alle vier sind im Dashboard einstellbar.
- **Button „Schlafetage 20 min durchlüften“:** für jedes Familienmitglied. Danach läuft die Etage wieder wie vorher.
- **Handy-Hinweis:** wenn der Radonschutz anspringt oder endet, und wenn Radon-Messgerät oder Taupunktsteuerung länger als 20 Minuten nicht verbunden sind.
- **Radonvorrang:** Solange NeuraCell-X den Radonschutz aktiv hat, pausieren alle Automationen.

## Einbindung

1. In der `configuration.yaml` einmalig eintragen:
   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```
2. `ambientika_schlafetage.yaml` nach `config/packages/` kopieren.
3. Im Skript `lueftung_schlafetage_setzen` die Liste `master_serials` anpassen. Dort gehören nur die **Master**-Geräte der Etage hinein, mit ihrer Seriennummer. Slaves folgen ihrem Master automatisch.
4. Entwicklerwerkzeuge → YAML → Konfiguration prüfen, dann Home Assistant neu starten.
5. Kontrolle: Entwicklerwerkzeuge → Aktionen → `Lüftung Schlafetage setzen`, Haken bei `nur_pruefen`, ausführen. Es erscheint eine Benachrichtigung mit den gefundenen Geräten und ihren Entitäten. Es werden dabei keine Befehle gesendet.

## Die Seriennummern für `master_serials`

Die Seriennummer steht im Log der Bridge (Einstellungen → Add-ons → Ambientika MQTT Bridge → Protokoll), zum Beispiel:

```
Device: Eltern  (serial: 441BF633FDD4)
```

Daraus wird der Eintrag `"441BF633FDD4"`. Groß-/Kleinschreibung spielt keine Rolle.

Das Paket sucht die Geräte über die Seriennummer, nicht über den Namen. Ein Gerät darf deshalb jederzeit in der Ambientika-App oder in Home Assistant umbenannt werden, die Automationen laufen weiter. (Frühere Versionen dieses Pakets haben die Geräte über den Anfang der Entitäts-ID gesucht. Diese ID vergibt Home Assistant beim ersten Erkennen aus dem damaligen Gerätenamen und behält sie auch nach einer Umbenennung. Wer die Geräte umbenannt hat, hat dann Befehle nur noch bei den Geräten gesehen, deren Name gleich geblieben ist. Mit den Seriennummern entfällt das.)

Wird ein Gerät nicht gefunden, bricht das Skript mit einem Hinweis ab und legt eine Benachrichtigung an, welche Seriennummern fehlen.

## Hinweise

- `Lüftung Schlafetage setzen` ist ein Baustein für die Automationen und den Button. Direkt ausgeführt ohne Modus und Stufe, bricht es mit einem verständlichen Hinweis ab. Nur mit `nur_pruefen` zeigt es die gefundenen Geräte an.
- Die Uhrzeiten haben `initial`-Werte, die nach jedem Neustart gelten. Wer sie im Dashboard dauerhaft ändern will, löscht nach dem ersten Start die `initial`-Zeilen.
