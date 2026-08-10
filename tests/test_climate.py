"""Test for localtuya."""

from unittest.mock import call

from . import *
import custom_components.localtuya.climate as climate_module
from custom_components.localtuya.climate import (
    LocalTuyaClimate,
    HVACAction,
    HVACMode,
    DOMAIN as PLATFORM_DOMAIN,
)
from homeassistant.exceptions import HomeAssistantError

FAN_SPEED_LIST = ["auto", "low", "middle", "high"]
CONFIG = {
    DEVICE_NAME: {
        **DEVICE_CONFIG,
        "entities": [
            {
                "friendly_name": "Terrace Floor",
                "entity_category": "None",
                "target_temperature_dp": "16",
                "current_temperature_dp": "24",
                "temperature_step": "1",
                "min_temperature": 7.0,
                "max_temperature": 35.0,
                "precision": "1",
                "target_precision": "1",
                "hvac_mode_dp": "2",
                "hvac_mode_set": {
                    "auto": "program",
                    "heat": "manual",
                    "cool": "COLD",
                    "dry": "dehumidify",
                    "fan_only": "fan",
                },
                "hvac_action_dp": "100",
                "hvac_action_set": {
                    "heating": True,
                    "idle": False,
                },
                "preset_dp": "5",
                "preset_set": {"holiday": "Holiday Friendly Name"},
                "fan_speed_dp": "6",
                "fan_speed_list": ",".join(FAN_SPEED_LIST),
                "swing_mode_dp": "11",
                "swing_modes": {
                    "both": "up-and-down",
                    "up": "up-only",
                    "down": "down-only",
                },
                "swing_horizontal_dp": "12",
                "swing_horizontal_modes": {
                    "both": "left-and-right",
                    "left": "left-only",
                    "right": "right-only",
                },
                "temperature_unit": "fahrenheit/celsius",
                "id": "1",
                "eco_dp": "101",
                "eco_value": "eco_on",
                "platform": "climate",
                "icon": "",
                "heuristic_action": False,
            }
        ],
    }
}

DPS_STATUS = {
    "1": True,
    "2": "COLD",
    "5": "holiday",
    "6": "middle",
    "11": "both",
    "12": "right",
    "16": 68,  # F
    "24": 24,  # c
    "100": False,
    "101": "ECO_NOT",
}


async def test_climate():
    device = await init(CONFIG, PLATFORM_DOMAIN, LocalTuyaClimate)
    entities: list[LocalTuyaClimate] = get_entites(device)

    assert len(entities) > 0
    entity_1, *_ = entities
    assert type(entity_1) is LocalTuyaClimate

    assert entity_1._is_on == None

    status = {**DPS_STATUS, **{"1": False}}
    device.status_updated(status)
    assert type(entity_1._state_on) is bool

    assert entity_1._is_on == False
    assert entity_1.hvac_action == HVACAction.OFF
    assert entity_1.hvac_mode == HVACMode.OFF

    device.status_updated(DPS_STATUS)
    assert entity_1._is_on == True
    assert entity_1.hvac_action == HVACAction.IDLE
    assert entity_1.hvac_mode == HVACMode.COOL
    assert entity_1.fan_modes == FAN_SPEED_LIST
    assert entity_1.preset_mode == "Holiday Friendly Name"
    assert entity_1.current_temperature == 24
    assert entity_1.target_temperature == 20  # f to c
    assert entity_1.fan_mode == "middle"

    # Eco Preset
    device.status_updated({**DPS_STATUS, **{"101": "eco_on"}})
    assert entity_1.preset_mode == "eco"

    # heuristic_action
    entity_1._hvac_action_dp = None
    status = {**DPS_STATUS, **{"2": "manual", "24": 18.9}}
    device.status_updated(status)
    assert entity_1.hvac_action == HVACAction.HEATING
    status = {**DPS_STATUS, **{"2": "manual", "24": 21.1}}
    device.status_updated(status)
    assert entity_1.hvac_action == HVACAction.IDLE

    # String DP ID
    status = {**DPS_STATUS, **{"1": "OFF"}}
    device.status_updated(status)
    assert not entity_1._is_on
    assert type(entity_1._state_on) is str

    # Integer DP ID
    status = {**DPS_STATUS, **{"1": 1}}
    device.status_updated(status)
    assert entity_1._is_on
    assert type(entity_1._state_on) is int

    # Swing modes
    assert entity_1.swing_mode == "up-and-down"
    assert entity_1.swing_horizontal_mode == "right-only"
    device.status_updated({**DPS_STATUS, **{"11": "up", "12": "both"}})
    assert entity_1.swing_mode == "up-only"
    assert entity_1.swing_horizontal_mode == "left-and-right"


async def test_climate_retries_until_command_is_confirmed(monkeypatch):
    """Climate commands are retried until device status confirms the change."""
    device = await init(CONFIG, PLATFORM_DOMAIN, LocalTuyaClimate)
    entity, *_ = get_entites(device)
    device.status_updated(DPS_STATUS)

    monkeypatch.setattr(climate_module, "COMMAND_CONFIRM_INTERVAL", 0)
    monkeypatch.setattr(climate_module, "COMMAND_CONFIRM_CHECKS", 2)

    attempts = 0

    async def set_dps(states):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            device.status_updated({**DPS_STATUS, **states})

    device.set_dps = AsyncMock(side_effect=set_dps)
    device.async_refresh_status = AsyncMock(return_value={})

    await entity.async_set_hvac_mode(HVACMode.HEAT)

    expected = {"2": "manual", "16": 68}
    assert device.set_dps.await_args_list == [call(expected), call(expected)]
    device.async_refresh_status.assert_awaited_once_with()


async def test_climate_raises_when_command_is_not_confirmed(monkeypatch):
    """An unconfirmed command reports failure after all retry attempts."""
    device = await init(CONFIG, PLATFORM_DOMAIN, LocalTuyaClimate)
    entity, *_ = get_entites(device)
    device.status_updated(DPS_STATUS)

    monkeypatch.setattr(climate_module, "COMMAND_RETRIES", 2)
    monkeypatch.setattr(climate_module, "COMMAND_CONFIRM_INTERVAL", 0)
    monkeypatch.setattr(climate_module, "COMMAND_CONFIRM_CHECKS", 2)

    device.set_dps = AsyncMock()
    device.async_refresh_status = AsyncMock(return_value={})

    with pytest.raises(HomeAssistantError, match="did not confirm"):
        await entity._async_set_dps_verified({"2": "manual"})

    assert device.set_dps.await_count == 2
    assert device.async_refresh_status.await_count == 2


async def test_climate_ignores_non_setpoint_temperature_by_mode():
    """Dry and fan modes do not overwrite the last real target temperature."""
    device = await init(CONFIG, PLATFORM_DOMAIN, LocalTuyaClimate)
    entity, *_ = get_entites(device)
    device.status_updated(DPS_STATUS)
    target_temperature = entity.target_temperature

    device.status_updated({**DPS_STATUS, "2": "fan", "16": 99})
    assert entity.target_temperature == target_temperature

    device.status_updated({**DPS_STATUS, "2": "COLD", "16": 70})
    assert entity.target_temperature != target_temperature


async def test_refresh_status_dispatches_readback():
    """A requested device readback is dispatched to all entities."""
    device = await init(CONFIG, PLATFORM_DOMAIN, LocalTuyaClimate)
    status = {"2": "manual"}
    device.check_connection = AsyncMock()
    device._interface = Mock()
    device._interface.status = AsyncMock(return_value=status)
    device.status_updated = Mock()

    result = await device.async_refresh_status()

    assert result == status
    device._interface.status.assert_awaited_once_with(cid=device._node_id)
    device.status_updated.assert_called_once_with(status)
