"""Constants of the HottoH integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "hottoh"
# Device model: the Wi-Fi module of the stove, reached through hottoh_api (not affiliated with
# the module or stove makers, whose names are only used to describe compatibility)
MODEL: Final = "Wi-Fi module (hottoh_api)"

DEFAULT_PORT: Final = 3000
DEFAULT_SCAN_INTERVAL: Final = 10

# Options: thermostat
CONF_TEMPERATURE_SENSOR: Final = "temperature_sensor"
CONF_REGULATION_MODE: Final = "regulation_mode"
CONF_HYSTERESIS_LOW: Final = "hysteresis_low"
CONF_HYSTERESIS_HIGH: Final = "hysteresis_high"
CONF_MIN_ON_MINUTES: Final = "min_on_minutes"
CONF_MIN_OFF_MINUTES: Final = "min_off_minutes"
CONF_ONOFF_STOVE_SETPOINT: Final = "onoff_stove_setpoint"
CONF_DISABLE_CHRONO: Final = "disable_chrono"
CONF_SENSOR_TIMEOUT_MINUTES: Final = "sensor_timeout_minutes"

REGULATION_ON_OFF: Final = "on_off"
REGULATION_COMPENSATED: Final = "compensated"

DEFAULT_HYSTERESIS_LOW: Final = 1.0
DEFAULT_HYSTERESIS_HIGH: Final = 1.0
DEFAULT_MIN_ON_MINUTES: Final = 60
DEFAULT_MIN_OFF_MINUTES: Final = 30
DEFAULT_ONOFF_STOVE_SETPOINT: Final = 30.0
DEFAULT_SENSOR_TIMEOUT_MINUTES: Final = 15

# Compensated set point: minimum change and minimum delay between two writes
COMPENSATION_MIN_DELTA: Final = 0.2
COMPENSATION_MIN_INTERVAL_S: Final = 300

# Options: notifications
CONF_NOTIFY_SERVICES: Final = "notify_services"
CONF_NOTIFY_ENTITIES: Final = "notify_entities"
CONF_NOTIFY_EVENTS: Final = "notify_events"
CONF_PERSISTENT_NOTIFICATION: Final = "persistent_notification"
CONF_CONNECTION_LOST_MINUTES: Final = "connection_lost_minutes"

EVENT_ALARM: Final = "alarm"
EVENT_ALARM_CLEARED: Final = "alarm_cleared"
EVENT_CONNECTION_LOST: Final = "connection_lost"
EVENT_CONNECTION_RESTORED: Final = "connection_restored"
EVENT_WRITE_FAILED: Final = "write_failed"
EVENT_SENSOR_UNAVAILABLE: Final = "sensor_unavailable"
NOTIFY_EVENTS: Final = [
    EVENT_ALARM,
    EVENT_ALARM_CLEARED,
    EVENT_CONNECTION_LOST,
    EVENT_CONNECTION_RESTORED,
    EVENT_WRITE_FAILED,
    EVENT_SENSOR_UNAVAILABLE,
]
DEFAULT_NOTIFY_EVENTS: Final = [
    EVENT_ALARM,
    EVENT_ALARM_CLEARED,
    EVENT_CONNECTION_LOST,
    EVENT_SENSOR_UNAVAILABLE,
]
DEFAULT_CONNECTION_LOST_MINUTES: Final = 10

# Event fired on the bus for every notification-worthy event, whatever the options say
BUS_EVENT: Final = "hottoh_event"

# Stove states (`index_stove_state_raw`), as decoded by hottoh_api
STOVE_STATES: Final[dict[int, str]] = {
    0: "off",
    1: "starting_1",
    2: "starting_2",
    3: "starting_3",
    4: "starting_4",
    5: "starting_5",
    6: "starting_6",
    7: "starting_7",
    8: "power",
    9: "stopping_1",
    10: "stopping_2",
    11: "eco_stop_1",
    12: "eco_stop_2",
    13: "eco_stop_3",
    14: "low_pellet",
    15: "end_pellet",
    16: "black_out",
    17: "anti_freeze",
    60: "ignition_failed",
    61: "no_pellet",
    69: "cover_open",
}
STATE_UNKNOWN_STOVE: Final = "unknown"
STARTING_STATES: Final = frozenset(range(1, 8))
STOPPING_STATES: Final = frozenset({9, 10})
ECO_STOP_STATES: Final = frozenset({11, 12, 13})


def is_alarm(state_raw: int) -> bool:
    """Alarm states, as `StoveState::is_alarm` in hottoh_api."""
    return 14 <= state_raw <= 16 or 50 <= state_raw <= 99


def stove_state_name(state_raw: int | None) -> str | None:
    """Enum value of a raw stove state."""
    if state_raw is None:
        return None
    return STOVE_STATES.get(state_raw, STATE_UNKNOWN_STOVE)
