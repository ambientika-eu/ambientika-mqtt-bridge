# Ambientika MQTT Bridge

Connects your Ambientika ventilation units to Home Assistant over MQTT, with
auto-discovery. The add-on runs locally on your Home Assistant machine; it signs
in to your Ambientika account to reach the units.

## Before you start

You need two things:

1. **An MQTT broker.** If you have none, install the official **Mosquitto broker**
   app first (Settings → Apps → Install app → Mosquitto broker; before HA 2026.2: Settings → Add-ons → Add-on Store) and start it.
2. **Your Ambientika account.** That is the same e-mail address and password you
   use to sign in to the Ambientika app. There is no separate account for the
   bridge, and nothing to register.

## Setting it up

Open the **Configuration** tab of this add-on and fill in:

- `ambientika_username` — your Ambientika app e-mail address
- `ambientika_password` — your Ambientika app password
- `mqtt_username` / `mqtt_password` — a user of your MQTT broker

Then save and start the add-on. Your units appear under
**Settings → Devices & Services → MQTT → Devices**.

> **The MQTT user is not optional.** The official Mosquitto add-on refuses
> anonymous connections. If you leave `mqtt_username` and `mqtt_password` empty,
> the log shows `MQTT connection failed (rc=5)`. Create a user in Mosquitto's
> `logins` option, or use an existing Home Assistant user, and enter it here.

## Options

| Option | Default | Description |
|---|---|---|
| `ambientika_username` | *(required)* | E-mail address of your Ambientika app account |
| `ambientika_password` | *(required)* | Password of your Ambientika app account |
| `mqtt_host` | `core-mosquitto` | Broker hostname. `core-mosquitto` is the Mosquitto add-on |
| `mqtt_port` | `1883` | Broker port |
| `mqtt_username` | *(empty)* | Broker user — required for the Mosquitto add-on |
| `mqtt_password` | *(empty)* | Broker password |
| `mqtt_topic_prefix` | `ambientika` | Prefix of all MQTT topics |
| `poll_interval` | `30` | How often the units are read, in seconds (10–300) |
| `availability_failure_threshold` | `3` | Consecutive failed reads before a unit is shown as unavailable. Prevents flickering on a single cloud hiccup |
| `log_level` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `command_coalesce_ms` | `800` | Commands for the same unit within this window are applied in one call. `0` applies every command immediately |
| `average_window_min` | `30` | Window of the averaged sensors *Humidity (averaged)* and *Temperature (averaged)*, in minutes (1–240; at least twenty poll intervals), see below |
| `average_window_devices` | *(empty)* | Own window for single units: `unit=minutes` entries, comma-separated, by serial number or by the unit's name as in the app (1–240 minutes), e.g. `A1B2C3D4E5F6=15, Schlafzimmer Eltern=60`. The serial number is the safe way (it is in the add-on log and in the app); a name must match the app's spelling - upper/lower case does not matter, and a hyphen, underscore or space count as the same separator; anything else does matter. Units not listed use `average_window_min`, see below |
| `slave_filter_soft_reset` | `false` | Maintenance acknowledgement for Slave units, see below |
| `filter_ack_ttl_days` | `90` | How long such an acknowledgement stays valid (1–3650) |

### NeuraCell-X (radon protection and dew-point control)

The options starting with `radon_` and `dewpoint_` configure the radon and
dew-point protection. They only matter if you have the matching hardware, and
the defaults are safe to leave alone. The Ambientika radon meter
(`radon/<id>/state`) and the Ambientika Taupunktsteuerung (`dew-point/<id>/state`,
`ventilating`) are read directly with the defaults, nothing to configure. The
sensors *Radon Meter Connected* and *Dew Point Controller Connected* show whether
the two devices are still talking to the broker: their Last Will counts at once,
a radon meter silent for longer than `radon_meter_timeout` (30 min) counts as
disconnected as well, also after an add-on restart (the meters seen so far are
remembered on the broker). A third-party dew-point controller needs
`dewpoint_availability_topic` or `dewpoint_signal_timeout` for that (with
`dewpoint_source: device` it follows the cloud reads instead). How the two
protections work together (radon first, exact restore of every unit's mode) is
described in the
[project README](https://github.com/ambientika-eu/ambientika-mqtt-bridge#neuracell-x--patent-pending-radon--dew-point-protection).

**Radon protection** - radon alarm: every unit to Intake (supply air) at
`radon_protection_fan` until the value is back below the threshold minus the
hysteresis.

| Option | Default | Description |
|---|---|---|
| `neuracell_enabled` | `true` | Radon protection on or off |
| `radon_source` | `signal` | `signal`: the radon value and alarm come from the MQTT topics below. `device`: the radon meter's status is read from the Ambientika cloud, no MQTT signal needed |
| `radon_threshold` | `300` | Bq/m³ at which radon protection starts (50–1000; 300 is the German reference value). With `radon_source: device` also the limit for a numeric alarm field |
| `radon_hysteresis` | `50` | Bq/m³ the value must fall below the threshold before protection ends (0–500) |
| `radon_protection_fan` | `Low` | Fan speed during radon protection: `Low`, `Medium` or `High` |
| `radon_meter_topic` | `radon/+/state` | Native topic of the Ambientika radon meter (MQTT mode 4, `{"mittelwert": …}`), read directly. `none` switches the direct read off |
| `radon_meter_timeout` | `30` | Minutes without any message from an Ambientika radon meter before it counts as disconnected (0 = off; the meter reports every 10 minutes, 20 or more is recommended) |
| `radon_topic` | `ambientika/radon/value` | Topic with a radon value in Bq/m³ from any other source: a plain number or JSON (see `radon_value_key`); MQTT wildcards allowed. With several sources the highest current value decides |
| `radon_alarm_topic` | `ambientika/radon/alarm` | Topic with an explicit alarm (`ON`/`OFF`, `true`/`false`). Value and alarm are combined, either one keeps protection on. `none` = ignored, so a stale retained `ON` can never hold protection |
| `radon_value_key` | `mittelwert` | JSON key of the radon value when `radon_topic` or the meter topic carries JSON |
| `radon_device_serial` | *(empty)* | `radon_source: device`: serial number of the radon meter (shown in the add-on log). The meter is read only, never exposed as a fan |
| `radon_device_alarm_field` | `air_quality` | `radon_source: device`: the status field of the meter that carries its alarm or level. A numeric field is compared with `radon_threshold`, a text field with the values below |
| `radon_device_alarm_values` | `Bad,Poor,Very Bad,Alarm,Alert` | `radon_source: device`: text values of that field that mean "radon alarm" (comma-separated, upper/lower case does not matter) |

**Dew-point control** - ventilating would raise indoor humidity: the units in
`dewpoint_block_devices` switch off until conditions are favourable again. Radon
protection always has priority.

| Option | Default | Description |
|---|---|---|
| `dewpoint_enabled` | `true` | Dew-point control on or off |
| `dewpoint_source` | `signal` | `signal`: a block signal from MQTT - the Ambientika Taupunktsteuerung on its own topic (below) or a third-party source on `dewpoint_block_topic`. `computed`: the bridge computes indoor and outdoor dew point from four sensor topics. `device`: the Taupunktsteuerung's status is read from the Ambientika cloud |
| `dewpoint_block_devices` | *(empty)* | Units the block applies to, by name as in the app or serial number, comma-separated (e.g. the basement units). Empty = all units |
| `dewpoint_controller_topic` | `dew-point/+/state` | Native topic of the Ambientika Taupunktsteuerung (MQTT firmware), read directly; its Last Will on `dew-point/<id>/availability/state` and its sensor messages are watched automatically. `none` switches the direct read off |
| `dewpoint_controller_key` | `ventilating` | JSON key in those messages; `false` means: block ventilation |
| `dewpoint_block_topic` | `ambientika/dewpoint/block` | `signal`: topic of a third-party block signal (`ON`/`true`/`1` = block). The last message wins. Leave it at its default so it does not overlap the controller topic |
| `dewpoint_block_key` | *(empty)* | JSON key of the block flag when `dewpoint_block_topic` carries a JSON object |
| `dewpoint_availability_topic` | *(empty)* | `signal`: availability topic with Last Will of a third-party controller (`online`/`offline`, `true`/`false` or JSON with `state`). The Ambientika controller needs none |
| `dewpoint_signal_timeout` | `0` | `signal`: minutes without any message from the controller before it counts as disconnected (0 = off). For controllers without Last Will; not applied while a Last Will reports online |
| `dewpoint_lost_action` | `keep` | While the controller counts as disconnected: `keep` the last block state, `release` ventilation, or `block` the units. Radon keeps priority either way |
| `dewpoint_indoor_temp_topic` | `ambientika/dewpoint/indoor_temp` | `computed`: indoor temperature in °C |
| `dewpoint_indoor_humidity_topic` | `ambientika/dewpoint/indoor_humidity` | `computed`: indoor relative humidity in % |
| `dewpoint_outdoor_temp_topic` | `ambientika/dewpoint/outdoor_temp` | `computed`: outdoor temperature in °C |
| `dewpoint_outdoor_humidity_topic` | `ambientika/dewpoint/outdoor_humidity` | `computed`: outdoor relative humidity in % |
| `dewpoint_margin` | `1.0` | `computed`: block as soon as the outdoor dew point reaches the indoor dew point minus this margin (°C) - ventilating would then add moisture |
| `dewpoint_hysteresis` | `0.5` | `computed`: release only once the outdoor dew point is this much (°C) below that line again |
| `dewpoint_device_serial` | *(empty)* | `dewpoint_source: device`: serial number of the Taupunktsteuerung (shown in the add-on log). It is read only, never exposed as a fan |
| `dewpoint_device_block_modes` | `Off` | `dewpoint_source: device`: operating modes of the Taupunktsteuerung that mean "block" (comma-separated) |

## Filter reset on Master/Slave groups

The manufacturer resets filters per zone through the **Master** only. Every Slave
keeps its own counter, which nothing clears: a reset sent to a Slave is
acknowledged but never carried out, the app shows only the Master's filter
status, and there is nothing to press at the unit either (the unit's own filter
alarm is active on Masters only). The bridge says so in plain words instead of
promising a change that will not come.

The only way to clear a Slave's own counter is a reset while the unit is a
Master: set it up in the app as a standalone unit for the moment, reset the
filter, then couple it as a Slave again. For everyday use the acknowledgement
below is the simpler way.

Switch on `slave_filter_soft_reset` to record such a maintenance yourself. The
serviced unit then reads green, while the unchanged device value stays visible:

| Field | Content |
|---|---|
| `filters_status` / `filter_status_num` | effective value (acknowledgement applied) |
| `filters_status_raw` / `filter_status_raw_num` | raw device value |

The diagnostic sensor *Filter Reset Status* reports `confirmed` (the counter
really cleared), `acknowledged` (recorded by the bridge) or `unconfirmed`. The
acknowledged value is published right away, together with its log line. The
sensor keeps saying `acknowledged` for as long as the acknowledgement is in
force - also after a restart or an update of the add-on (up to 1.4.34 every
unit started as `idle`, so nothing showed that an acknowledgement was in
force) - and drops back to `idle` the moment it ends.

With `slave_filter_soft_reset` on, every unit also has two diagnostic
timestamp sensors, **Filter Acknowledged Since** and **Filter Acknowledgement
Valid Until** (`filter_ack_since` / `filter_ack_until` in the state). They show
when the acknowledgement was recorded and the day it runs out; without one they
are *unknown*. So the day the effective status falls back to the device's own
value is visible in advance, not a surprise.

An acknowledgement ends for exactly two reasons, and both are written to the log:
`filter_ack_ttl_days` has run out, or the unit itself reports `Good` for at least
ten polls in a row and at least ten minutes (its counter was cleared after all,
e.g. by a reset as a temporary standalone unit).
A single poll with an unknown or briefly green value no longer removes it. Up to
1.4.29 one such poll was enough, silently, so an acknowledgement could vanish long
before its time. At start-up the log names every acknowledgement still in force,
with its date and the day it runs out.

## Mode changes on Master/Slave groups

The cloud answers a mode change with OK as soon as it has accepted the call, not
when the unit has carried it out. The log line `change_mode OK ... (accepted by
the cloud)` therefore only says that much. The bridge then watches the following
polls: `operating mode ... confirmed` means the unit has really switched. If it
still reports a different mode after three minutes, a warning says so.

In a coupled zone a Slave runs with its **Master**, so set the mode on the
Master. A Slave's own mode field is not what it is doing: a Slave can report
`Surveillance` and still ventilate in the Master's rhythm. Like the Ambientika app,
which shows only the Master for a zone, the `Mode` of a Slave therefore shows its
Master's mode; the unit's own value is in `Mode raw`:

| Field | Content |
|---|---|
| `operating_mode` / `operating_mode_num` | effective mode (the Master's for a Slave) |
| `operating_mode_raw` / `operating_mode_raw_num` | the unit's own value |

While NeuraCell-X protection controls a unit, or if its Master has not been read
for a while, the unit's own value is shown. Which unit is a Slave and which its
Master is taken from the role each unit reports in its own status, so re-coupling
or resetting units in the app is followed without restarting the add-on. The log
notes once per change when a Slave's own value differs from what is shown.

For automations this means: a Slave's `Mode` / `Mode (num)` now follow the
Master. Selecting a mode on a Slave's `Mode` control still sends the command to
that unit, but the control shows the Master's mode again on the next poll - set
the mode on the Master instead.

## Implausible readings

Temperature and humidity outside the sensor range are never published (humidity
1-100 %, temperature -40 to 85 °C). A single impossible drop such as 6 % humidity
between 55 and 70 % is held back as well: below 20 % a value is published only if
dry air was already measured shortly before (among the last three readings, or a
published dry reading within the last hour). Rises, shower peaks and the normal
reversing rhythm are never held back; in dry winter air only the first dry
reading after a long humid stretch is delayed by one reading. Reading the same
status packet again within two minutes (the cloud keeps the unit's last packet)
does not count as a new reading. Meanwhile the last plausible value stays, for at most ten minutes, then
the value shows as unknown - a failed sensor never looks live. Both events are
written to the log once, at most once an hour for a flapping sensor.

## Averaged humidity and temperature

The unit reverses its airflow about every half minute, so its sensor sees room
air and incoming air in turn and the humidity reading jumps by up to about 20
percentage points between two reads. A single reading is therefore hard to use:
a dashboard tile shows something different at every look, and an automation on
a humidity threshold fires depending on the phase it happens to read.

Each unit therefore has two more sensors, **Humidity (averaged)** and
**Temperature (averaged)**: the time-weighted mean of the published readings over
the last `average_window_min` minutes (30 by default, at least twenty poll
intervals). Use them for the dashboard, for history and for automations with a
threshold. The raw *Humidity* and *Temperature* stay unchanged next to them and
react at once, e.g. to a shower. The mean covers the whole reversing cycle, room
air and incoming air - a steady value, not a pure room-air value.

What to expect: the cloud hands out about one packet per half minute, i.e. about
one reading per reversal. Which phase of the cycle it catches drifts slowly, so
the mean is much calmer than the raw value but can still wander by several
points over an hour - with an upload rhythm close to the reversal rhythm more,
with an irregular one less. A longer window (60 min) halves that swing at the
price of a slower reaction. Keep `poll_interval` at 30 s (the default) or
shorter; at 60 s and above the mean needs many more reads and scatters more.

In a real installation with five units over three days, the 30-minute mean
varied by about 2.5 percentage points within an hour (raw: about 9), and its daily
mean matched the raw daily mean to 0.1 points. The unit reports humidity in steps
of 2–3 points, so a longer window gains little there and mainly adds delay.

**A window per unit.** A bathroom should follow a shower quickly, a bedroom can
take much more smoothing. `average_window_devices` gives single units their own
window: `unit=minutes` entries separated by commas, by the unit's serial number
or its name as in the Ambientika app, for example

```yaml
average_window_devices: "A1B2C3D4E5F6=15, Schlafzimmer Eltern=60"
```

The serial number is the safe way: it is unique, it stands in the add-on log
(`Device: <name> (serial: …)`) and in the app, and it cannot be misspelt the way a
name can. A name has to match the app's spelling - upper/lower case does not matter, and
a hyphen, underscore or space count as the same separator (`Gäste-Bad` finds a
unit called `Gäste Bad`, `Gästebad` does not); anything else does matter. The log tells you when an entry matches no unit, and lists the
units with their serial numbers.

Units not listed keep `average_window_min`. Each entry takes 1–240 minutes; the
floor of twenty poll intervals applies here too (10 minutes at the default 30 s).
A shorter window follows a change faster but shows more of the residual swing
described above: simulated with an irregular upload rhythm, the 15-minute mean
swings about twice as much within an hour as the 30-minute mean, the 60-minute
mean about half as much. 15 minutes is a sensible lower end for a bathroom.
The add-on log shows the windows in force right after the units are found, e.g.
`Averaging per unit: Gäste Bad 15 min, Schlafzimmer Eltern 60 min; all others
30 min.` The minutes are a plain number (decimal point, e.g. `7.5`; a trailing
`min` is fine). A name that matches no unit, or an entry the bridge cannot read,
is named in the log and ignored. For a unit whose name contains a comma or
semicolon, use its serial number. Names are read at start-up: after renaming a
unit in the app, restart the add-on.

Held-back readings (see above) do not count. After a restart the averaged sensors
show *unknown* until about two minutes of readings are in (with a long poll
interval: two reads). Like the raw values they turn *unknown* when the sensor has
delivered no plausible reading for ten minutes.

## What SMART is currently doing

The `Mode` control shows the macro mode you selected (for a Slave: the one
selected on its Master). In `Smart` and `Auto` it stays on that value even
though the unit switches between concrete functions on its own. The read-only
sensor **Last Mode** (`last_operating_mode`) shows the function actually running,
and **Fan Speed** shows the real speed.

**Fan Speed** can show `Night` or `Turbo`: the unit chose that step itself. You
can read these values but not set them. A command that names one (for example a
scene that restores a snapshotted `Turbo`) or that does not name the speed at all
(a mode change alone) never sends them back: the bridge uses the last speed the
cloud accepted for that unit, or `Low` for `Night` and `High` for `Turbo` if it
never saw one, and writes a line about it to the log. If you want a specific
speed, name it in the command.

## Setting several values in one automation

Commands for the same unit that arrive close together are applied in a single
call, so setting the mode and then the fan speed no longer overwrites the mode.
The window is `command_coalesce_ms` (800 ms by default); a single command is
therefore carried out up to that much later. Set it to `0` if you prefer every
command to go out immediately.

You can also send everything at once to `ambientika/<serial>/set`:

```json
{"operating_mode": "MasterSlaveFlow", "fan_speed": "High"}
```

## When something does not work

The **Log** tab is the place to look. The two most common lines:

- `Ambientika username/password missing` — the two `ambientika_*` options are empty.
- `MQTT connection failed (rc=5)` — the broker refused the login, see the note above.

## Support

- Issues: <https://github.com/ambientika-eu/ambientika-mqtt-bridge/issues>
- Website: <https://www.ambientika.eu>
