# Ambientika MQTT Bridge

Connects your Ambientika ventilation units to Home Assistant over MQTT, with
auto-discovery. The add-on runs locally on your Home Assistant machine; it signs
in to your Ambientika account to reach the units.

## Before you start

You need two things:

1. **An MQTT broker.** If you have none, install the official **Mosquitto broker**
   add-on first (Settings → Add-ons → Add-on Store → Mosquitto broker) and start it.
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
| `slave_filter_soft_reset` | `false` | Maintenance acknowledgement for Slave units, see below |
| `filter_ack_ttl_days` | `90` | How long such an acknowledgement stays valid |

### NeuraCell-X (radon protection and dew-point control)

Thirty further options starting with `radon_` and `dewpoint_` configure the
radon and dew-point protection. They only matter if you have the matching
hardware, and the defaults are safe to leave alone. The Ambientika radon meter
(`radon/<id>/state`) and the Ambientika Taupunktsteuerung (`dew-point/<id>/state`,
`ventilating`) are read directly with the defaults, nothing to configure. The
sensors *Radon Meter Connected* and *Dew Point Controller Connected* show whether
the two devices are still talking to the broker: their Last Will counts at once,
a radon meter silent for longer than `radon_meter_timeout` (30 min) counts as
disconnected as well, also after an add-on restart (the meters seen so far are
remembered on the broker). A third-party dew-point controller needs
`dewpoint_availability_topic` or `dewpoint_signal_timeout` for that (with
`dewpoint_source: device` it follows the cloud reads instead). The full list with an
explanation of each is in the
[project README](https://github.com/ambientika-eu/ambientika-mqtt-bridge#neuracell-x--patent-pending-radon--dew-point-protection).

## Filter reset on Master/Slave groups

The filter reset is carried out by the **Master** of a coupled zone. Every Slave
keeps its own counter, which the cloud cannot reach: a reset sent to a Slave is
acknowledged but never carried out. The bridge says so in plain words instead of
promising a change that will not come. Resetting a Slave's counter for real is
only possible at the unit itself.

Switch on `slave_filter_soft_reset` to record such a maintenance yourself. The
serviced unit then reads green, while the unchanged device value stays visible:

| Field | Content |
|---|---|
| `filters_status` / `filter_status_num` | effective value (acknowledgement applied) |
| `filters_status_raw` / `filter_status_raw_num` | raw device value |

The diagnostic sensor *Filter Reset Status* reports `confirmed` (the counter
really cleared), `acknowledged` (recorded by the bridge) or `unconfirmed`. The
acknowledged value is published right away, together with its log line.

An acknowledgement ends for exactly two reasons, and both are written to the log:
`filter_ack_ttl_days` has run out, or the unit itself reports `Good` for at least
ten polls in a row and at least ten minutes (its filter was reset at the device).
A single poll with an unknown or briefly green value no longer removes it. Up to
1.4.29 one such poll was enough, silently, so an acknowledgement could vanish long
before its time.

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

## What SMART is currently doing

The `Mode` control shows the macro mode you selected (for a Slave: the one
selected on its Master). In `Smart` and `Auto` it stays on that value even
though the unit switches between concrete functions on its own. The read-only sensor **Active Operating Mode (SMART)** shows the
function actually running, and **Fan Speed** shows the real speed.

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
