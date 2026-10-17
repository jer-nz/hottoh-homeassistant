<p align="center">
  <img src="https://raw.githubusercontent.com/jer-nz/hottoh-homeassistant/main/custom_components/hottoh/brand/icon.png" alt="" width="96">
</p>

# HottoH for Home Assistant

Local integration for pellet stoves fitted with the **HottoH Wifier 2.0** Wi-Fi module (used by
the AppFire app, on CMG, Edilkamin and other brands), through
[hottoh_api](https://github.com/jer-nz/hottoh_api). No cloud.

- Sensors, settings (power, fans, eco, chrono) and alarms of the stove
- Thermostat on the stove probe or on any temperature sensor
- Notifications, events and actions for automations
- Web interface of hottoh_api in the sidebar

## Installation

With [HACS](https://hacs.xyz/): add `https://github.com/jer-nz/hottoh-homeassistant` as a custom
repository of type *Integration*, download **HottoH** and restart Home Assistant. Or copy
`custom_components/hottoh` into the `custom_components` folder of your configuration.

Then *Settings > Devices & services > Add integration > HottoH*, and choose where hottoh_api runs:

- **In Home Assistant**: enter the address of the stove (port 5001; give it a fixed address in
  your router). The integration downloads hottoh_api from GitHub, checks its SHA-256 and runs it
  with Home Assistant. 64-bit Linux only (amd64, arm64). Its files are in `<config>/hottoh/`.
- **On another computer**: enter the address of a hottoh_api **2.3.0 or later** that you run
  yourself (HTTP port 3000 by default).

The Wi-Fi module accepts **a single client**: one hottoh_api per stove, and AppFire in cloud mode
only. To switch a stove from one mode to the other, remove it and add it again.

## Web interface

The interface of hottoh_api (stove, schedule, history, module, diagnostics) can be shown:

- **in the sidebar**, for administrators only;
- **on the local network** at `http://<Home Assistant>:<port>/`, in embedded mode only. It has
  **no authentication**: anyone on the network can then drive the stove.

Both are set in *Configure > Web interface*.

## Thermostat

The stove always regulates on **its own probe**, close to it and warmer than the room: a set point
around 30 °C may be needed for 20 °C in the room. The thermostat chooses what the stove gets:

| Temperature source | Behaviour |
|---|---|
| Stove probe | As with the remote control: the target is the stove set point. |
| External sensor, **on/off** | Home Assistant turns the stove on below `target - 1 °C` and off above `target + 1 °C`, at least 60 min on and 30 min off. The stove set point stays at 30 °C. |
| External sensor, **compensated** | The stove stays on; its set point is `target + (stove probe - sensor)`, updated at most every 5 min. |

Values in *Configure*. With an external sensor, the chrono mode of the stove is turned off, nothing
is sent while the stove stops or is in alarm, and the regulation pauses while the sensor is
unavailable. A set point typed on the stove becomes the new target (in on/off mode, the stove then
shows 30 °C again). After an alarm, the stove is not turned on again by itself (attribute
`alarm_hold`). The "external probe / dry contact" input of some stove boards is not used.

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
| `setpoint_changed` | `stove_setpoint`, `target`: set point changed on the stove, new target |

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
| `hottoh.sync_clock` | sets the stove clock (hottoh_api feature `clock_write`); hottoh_api sends it at the start of the next minute, up to 60 s later |
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

## Disclaimer

This project is independent. It is not affiliated with, endorsed by or supported by HottoH,
Astrel Group, 4-Noks or any stove manufacturer. Their names are only used to describe the
hardware this project works with; all trademarks belong to their owners. No logo of these
companies is used.

## License

MIT
