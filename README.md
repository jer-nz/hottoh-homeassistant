# HottoH for Home Assistant

Home Assistant integration for pellet stoves fitted with the **HottoH Wifier 2.0** Wi-Fi module
(the one used by the AppFire app, found on CMG, Edilkamin and other brands), through
[hottoh_api](https://github.com/jer-nz/hottoh_api), a local bridge that speaks the module
protocol. No cloud.

- Sensors: state, room/smoke/water temperatures, power, fans, alarms, stove link
- Settings: power level, fans, eco and chrono modes, chrono programs
- Thermostat on the stove probe **or on any temperature sensor**, with two regulation modes
- Notifications on alarms (no pellets, ignition failure, door open...), lost link, refused commands
- Actions for automations and scripts, weekly schedule included
- Several stoves: one hottoh_api instance per stove, one config entry each

## Requirements

A running hottoh_api **2.3.0 or later**, either:

- on any computer of your network (see its README for packages and binaries), or
- as a Home Assistant add-on (Home Assistant OS or Supervised): add the repository
  `https://github.com/jer-nz/hottoh-homeassistant-addons` to the add-on (app) store of *Settings*,
  install **hottoh_api**, set the address of the stove and start it. The integration then offers
  to add the stove by itself.

The Wi-Fi module accepts **a single client**: run one hottoh_api per stove, and nothing else
talking to it in local mode (AppFire in cloud mode is fine).

## Installation

With [HACS](https://hacs.xyz/): *HACS > three dots > Custom repositories*, add
`https://github.com/jer-nz/hottoh-homeassistant` as an *Integration*, then download **HottoH** and
restart Home Assistant.

Manually: copy `custom_components/hottoh` into the `custom_components` folder of your
configuration and restart.

Then *Settings > Devices & services > Add integration > HottoH*, and enter the host and HTTP port
of hottoh_api (3000 by default).
The next step chooses the temperature that drives the stove: its own probe, or any temperature
sensor of Home Assistant.

## Thermostat

Temperature source and regulation mode are chosen when the stove is added; all settings can be
changed later in *Configure* on the integration.

| Temperature source | Who regulates |
|---|---|
| Stove probe (no external sensor) | The stove. The thermostat target is the stove set point, heat/off turns the stove on and off. |
| External sensor, **on/off** (default) | Home Assistant turns the stove on at `target - hysteresis low` and off at `target + hysteresis high` (1 °C each by default), keeping it on and off at least the minimum times (60 and 30 min by default). The stove set point is raised (30 °C by default) so that the stove does not stop by itself first. |
| External sensor, **compensated** | The stove stays on and regulates, Home Assistant shifts its set point by the difference between both probes, at most every 5 minutes. |

With an external sensor, the chrono mode of the stove is turned off (option), the stove is never
turned on again while it is still stopping, nothing is sent during an alarm, and the regulation
pauses while the sensor is unavailable. Turning the thermostat off stops the stove at once.

After an alarm (failed ignition, no pellets...), the regulation does not turn the stove on again by
itself: set the thermostat to heat again, or turn the stove on, once the cause is fixed (a failed
ignition may leave pellets in the burn pot). The `alarm_hold` attribute of the thermostat tells
when it waits.

## Notifications and events

In the second page of the options, choose notify actions (such as `notify.mobile_app_phone`)
and/or notify entities, the events to send, and whether to add a persistent notification.

Every event is also fired on the bus as `hottoh_event`, whatever the options:

```yaml
triggers:
  - trigger: event
    event_type: hottoh_event
    event_data:
      type: alarm
actions:
  - action: notify.mobile_app_phone
    data:
      message: "Stove: {{ trigger.event.data.alarm }}"
```

| `type` | Data |
|---|---|
| `alarm`, `alarm_cleared` | `alarm` (text), `state` (`no_pellet`, `ignition_failed`...), `code`, `started_at` |
| `connection_lost`, `connection_restored` | `minutes`, `detail` |
| `write_failed` | `detail` |
| `sensor_unavailable` | `entity_id`, `minutes` |

All events carry `entry_id` and `device_id`.

## Actions

Target a stove device or any of its entities. Writes wait for the answer of the stove and return
it (`response_variable`); a refused write raises an error.

| Action | Fields |
|---|---|
| `hottoh.set_power_level` | `level` |
| `hottoh.set_eco_mode`, `hottoh.set_chrono_mode` | `enabled` |
| `hottoh.set_fan_speed` | `fan` (1-3), `speed` |
| `hottoh.set_ambiance_temperature` | `ambiance` (1-2), `temperature`: the stove set point itself |
| `hottoh.set_chrono_temperature` | `program` (1-3), `temperature` |
| `hottoh.set_schedule` | `days`: see below |
| `hottoh.sync_clock` | sets the stove clock (hottoh_api feature `clock_write`) |
| `hottoh.restart_module` | restarts the Wi-Fi module (feature `module_restart`) |

```yaml
action: hottoh.set_schedule
target:
  device_id: 0123456789abcdef
data:
  days:
    - day: monday
      ranges:
        - {start: "06:30", end: "08:00", program: 1}
        - {start: "18:00", end: "22:30", program: 2}
    - day: sunday
      ranges: []
```

The thermostat also answers the usual `climate.*` actions.

## Development

```sh
uv sync --group test
uv run pytest
uv run ruff check custom_components tests tools
```

`tools/fake_stove.py` simulates a Wi-Fi module (protocol, ignition, alarms) for hottoh_api, to try
the whole chain without a stove:

```sh
python3 tools/fake_stove.py --speed 10 &               # module on 127.0.0.1:5001, control on :5080
hottoh_api test.ini                                    # [stove] ip = 127.0.0.1
curl -X POST localhost:5080/ -d '{"alarm": 61}'        # no pellets
```

## License

MIT
