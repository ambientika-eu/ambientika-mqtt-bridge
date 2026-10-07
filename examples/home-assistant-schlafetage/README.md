# Home-Assistant-Paket: Schlafetage mit Abend- und Nachtprogramm

Fertiges Paket für eine Etage mit mehreren Ambientika SMART/OFFICE über die Ambientika MQTT Bridge, mit NeuraCell-X-Radonvorrang.

## Was es macht

- **Abends:** 30 Minuten Querlüftung (Luftfluss Master → Slave, Stufe High), danach die ganze Nacht Wärmerückgewinnung auf Stufe Low.
- **Morgens:** zurück in den sensorgeführten SMART-Betrieb.
- **Wochentage:** Montag bis Freitag und Samstag/Sonntag haben getrennte Uhrzeiten, alle vier sind im Dashboard einstellbar.
- **Button „Schlafetage 20 min durchlüften“:** für jedes Familienmitglied. Danach läuft die Etage wieder wie vorher.
- **Handy-Hinweis:** wenn der Radonschutz anspringt oder endet, und wenn Radon-Messgerät oder Taupunktsteuerung länger als 20 Minuten nicht verbunden sind.
- **Selbstheilung (optional):** Radon-Messgerät und Taupunktsteuerung werden über eine schaltbare Steckdose automatisch neu gestartet, wenn sie 10 Minuten nicht verbunden sind – siehe unten.
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

Zu jedem Master braucht das Skript beide Auswahl-Entitäten der Bridge, *Mode* und *Fan Speed*. Es erkennt sie an ihren Optionen, nicht am Namen. Die Liste von *Fan Speed* enthält neben Low, Medium und High auch die Stufen Night und Turbo, die ein Gerät von selbst wählt (z. B. im SMART-Betrieb) und die nur angezeigt, nicht gesetzt werden können. Die Fassung vom 5. Oktober 2026 verlangte genau drei Einträge, fand die Stufen-Entität deshalb nie und setzte stillschweigend nur den Modus; die Stufe blieb dem Gerät überlassen – und die Bridge bis 1.4.28 schickte dann die Stufe aus dem Cloud-Status zurück, was die Cloud ablehnte, wenn das Gerät gerade auf Turbo stand (eine Stufe, die die Bridge bis dahin nicht kannte). Fehlt die Stufen-Entität, schaltet das Skript jetzt nichts und meldet sich; `nur_pruefen` zeigt zu jedem Master beide Entitäten.

## Selbstheilung: automatischer Neustart über eine Steckdose

Bleibt das Radon-Messgerät oder die Taupunktsteuerung hängen (keine Verbindung mehr zum Broker, Gerät kommt von selbst nicht zurück), hilft bisher nur, es kurz vom Strom zu nehmen. Das Paket kann das übernehmen: jedes der beiden Geräte hängt an einer Steckdose, die Home Assistant schalten kann, und wird automatisch neu gestartet, sobald die Bridge es 10 Minuten lang als nicht verbunden meldet (Sensor *Radon Meter Connected* bzw. *Dew Point Controller Connected* aus; ab Bridge 1.4.27 zählt dafür auch ein Messgerät, das nur still wird).

**Steckdosen:** jede Steckdose, die in Home Assistant als Schalter erscheint und ihren Schaltzustand zurückmeldet – zum Beispiel FRITZ!DECT 200/210 (Integration *AVM FRITZ!SmartHome*, passt zu einer FritzBox), Shelly Plug S (Integration *Shelly*), eine Tasmota-Steckdose (Integration *Tasmota* über MQTT) oder eine Zigbee-Steckdose. Zwei Stück, je eine pro Gerät. Wenn die Steckdose es anbietet, als Verhalten nach Stromausfall „Ein“ einstellen (Shelly „Power On Default Mode“, Tasmota `PowerOnState 1`), damit die Geräte auch dann Strom haben, wenn Home Assistant einmal nicht läuft.

**Einrichten:**

1. Die Entitäts-IDs der beiden Steckdosen nachsehen (Einstellungen → Geräte & Dienste → Entitäten, z. B. `switch.fritz_dect_200_1`).
2. Im Skript `ambientika_geraet_neu_starten` unter `steckdosen:` eintragen (`radon:` und `taupunkt:`, die Marken `&steckdose_radon` / `&steckdose_taupunkt` stehen lassen), Konfiguration prüfen, Home Assistant neu starten.
3. Kontrolle: Entwicklerwerkzeuge → Aktionen → `Ambientika Gerät über Steckdose neu starten`, `geraet` = `radon`, Haken bei `nur_pruefen`, ausführen. Die Benachrichtigung zeigt, ob die Steckdose gefunden wird und wie sie steht. Dasselbe mit `taupunkt`.
4. Probelauf: Skript `Radon-Messgerät jetzt neu starten` ausführen – die Steckdose geht 10 Sekunden aus und wieder an, das Messgerät meldet sich danach wieder. Ein Neustart von Hand wird nicht gezählt und löst keine Nachricht aufs Handy aus, nur eine Benachrichtigung in Home Assistant.
5. Im Dashboard die Schalter **Radon-Messgerät automatisch neu starten** und **Taupunktsteuerung automatisch neu starten** einschalten.

**Ablauf:** 10 Minuten nicht verbunden → Steckdose 10 s aus, wieder an → Zähler *Neustarts Radon-Messgerät heute* bzw. *Neustarts Taupunktsteuerung heute* +1 (um Mitternacht auf null). Höchstens ein Neustart pro Stunde und Gerät; kommt das Gerät nicht zurück, wird alle 30 Minuten geprüft und nach Ablauf der Stunde erneut neu gestartet. Wird ein Schalter eingeschaltet, während das Gerät schon länger als 10 Minuten nicht verbunden ist, kommt der erste Neustart spätestens mit der nächsten halbstündlichen Prüfung. Aufs Handy kommt nur der erste automatische Neustart des Tages, die weiteren stehen im Zähler. Hilft ein Neustart nicht, meldet sich wie bisher der Hinweis „seit 20 Minuten nicht verbunden“, jetzt mit dem Zusatz, dass der automatische Neustart nicht geholfen hat (oder dass er noch aussteht, etwa wegen der Sperrzeit).

**Sicherheit:** Das Skript wartet, bis die Steckdose „aus“ meldet (sonst kein Neustart, nur ein Hinweis in Home Assistant), schaltet danach bis zu dreimal ein, bis sie „an“ meldet, und schickt sonst einen Hinweis aufs Handy. Bleibt eine Steckdose trotzdem länger als 2 Minuten aus – etwa weil Home Assistant mitten im Schaltvorgang neu gestartet wurde –, schaltet der Wächter (Automation „Steckdose wieder einschalten“) sie wieder ein, solange der zugehörige Schalter „automatisch neu starten“ an ist. Zum Warten an einem Gerät also zuerst diesen Schalter ausschalten. Meldet die Integration der Steckdose selbst einen Fehler, kann der Hinweis aufs Handy ausbleiben; der Wächter greift trotzdem. Die Hinweise aufs Handy gehen über `notify.notify` (Companion-App).

Ab Bridge 1.4.28 werden die NeuraCell-X-Sensoren „nicht verfügbar“, solange das Add-on nicht läuft; die Selbstheilung pausiert dann automatisch, statt mit einem veralteten Stand zu schalten. Fällt dagegen das WLAN oder der Broker für mehr als 10 Minuten aus, werden beide Geräte einmal neu gestartet – das ist unschädlich.

Nebenwirkung: Das Messgerät beginnt nach einem Neustart neu zu messen (erster Wert nach rund 10 Minuten, der laufende Mittelwert geht verloren). Das passiert aber nur, wenn es ohnehin keine Werte mehr liefert; solange es nicht verbunden ist, arbeitet der Radonschutz mit dem letzten Stand. Die Taupunktsteuerung ist etwa eine Minute weg; für die Sperre im Keller gilt in der Zeit `dewpoint_lost_action` der Bridge.

Dashboard-Karte dazu (Entitäten): `input_boolean.ambientika_neustart_radon`, `input_boolean.ambientika_neustart_taupunkt`, `counter.ambientika_neustarts_radon`, `counter.ambientika_neustarts_taupunkt`, `script.ambientika_radon_messgeraet_neu_starten`, `script.ambientika_taupunktsteuerung_neu_starten`.

## Hinweise

- `Lüftung Schlafetage setzen` ist ein Baustein für die Automationen und den Button. Direkt ausgeführt ohne Modus und Stufe, bricht es mit einem verständlichen Hinweis ab. Nur mit `nur_pruefen` zeigt es die gefundenen Geräte an.
- Ohne eingetragene Steckdosen bleibt die Selbstheilung wirkungslos: Solange die Schalter „automatisch neu starten“ aus sind, passiert nichts; sind sie an und die Steckdose fehlt, erscheint eine Benachrichtigung in Home Assistant, und der Hinweis „seit 20 Minuten nicht verbunden“ sagt, dass der Neustart nicht möglich war.
- Die Uhrzeiten haben `initial`-Werte, die nach jedem Neustart gelten. Wer sie im Dashboard dauerhaft ändern will, löscht nach dem ersten Start die `initial`-Zeilen.
