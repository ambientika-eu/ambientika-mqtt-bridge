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
| `slave_filter_soft_reset` | `false` | Maintenance acknowledgement for Slave units, see below |
| `filter_ack_ttl_days` | `90` | How long such an acknowledgement stays valid |

### NeuraCell-X (radon protection and dew-point control)

Twenty-one further options starting with `radon_` and `dewpoint_` configure the
radon and dew-point protection. They only matter if you have the matching
hardware, and the defaults are safe to leave alone. The full list with an
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
really cleared), `acknowledged` (recorded by the bridge) or `unconfirmed`.

## What SMART is currently doing

The `Mode` control shows the macro mode you selected. In `Smart` and `Auto` it
stays on that value even though the unit switches between concrete functions on
its own. The read-only sensor **Active Operating Mode (SMART)** shows the
function actually running, and **Fan Speed** shows the real speed.

## Setting several values in one automation

Commands for the same unit that arrive close together are applied in a single
call, so setting the mode and then the fan speed no longer overwrites the mode.
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
