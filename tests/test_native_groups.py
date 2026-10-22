"""Native multicast framing, state isolation, validation and command behavior."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import voluptuous as vol
from homeassistant.exceptions import HomeAssistantError

from custom_components.localtuya.coordinator import TuyaDevice
from custom_components.localtuya.core.pytuya import TuyaProtocol, EmptyListener
from custom_components.localtuya.core.pytuya.const import CMDType, TuyaMessage
from custom_components.localtuya.light_groups import GROUPS_SCHEMA, NativeLightGroup


def config(**changes):
    return GROUPS_SCHEMA([{
        "unique_id": "room", "name": "Room", "device_id": "a",
        "group_id": "0002", "members": ["a", "b"],
        "power_dp": "119", "brightness_dp": "3", "temperature_dp": "4",
        **changes,
    }])[0]


def member(power=True):
    device = object.__new__(TuyaDevice)
    device._fake_gateway = False
    device._status = {"1": power}
    device._group_overrides = {}
    device._handle_event = Mock()
    device._dispatch_status = Mock()
    return device


def group():
    devices = [member(), member()]
    rep = SimpleNamespace(connected=True, set_group_dps=AsyncMock())
    entity = NativeLightGroup(config(), rep, [(d, {"119": "1", "3": "3", "4": "4"}) for d in devices])
    entity.async_write_ha_state = Mock()
    return entity, rep, devices


@pytest.mark.parametrize("changes", [
    {"group_id": 2}, {"members": ["b"]}, {"members": ["a", "a"]},
    {"power_dp": "3"}, {"min_kelvin": 6500}, {"brightness_dp": "0"},
])
def test_invalid_config(changes):
    with pytest.raises(vol.Invalid):
        config(**changes)


def test_no_overlapping_groups():
    with pytest.raises(vol.Invalid):
        GROUPS_SCHEMA([config(), config(unique_id="another")])


async def test_packet_preserves_native_address_and_datapoint():
    listener = EmptyListener()
    protocol = TuyaProtocol("gateway", "0123456789abcdef", 3.4, False, listener)
    protocol.exchange = AsyncMock()
    await protocol.set_group_dps({"119": False}, "child", "0002")
    args, kwargs = protocol.exchange.call_args
    assert args == (CMDType.CONTROL_NEW,)
    assert kwargs["strict"]
    data = json.loads(kwargs["payload"].payload)
    assert data["data"] == {"cid": "child", "ctype": 2, "mbid": "0002", "dps": {"119": False}}
    assert data["protocol"] == 5 and type(data["t"]) is int


async def test_group_reports_do_not_pollute_child_cache():
    listener = EmptyListener()
    child = Mock()
    listener.sub_devices = {"child": child}
    listener.group_status_updated = Mock()
    protocol = TuyaProtocol("gateway", "0123456789abcdef", 3.4, False, listener)
    protocol._decode_payload = Mock(return_value={
        "dps": {"119": False}, "cid": "child",
        "data": {"cid": "child", "ctype": 2, "mbid": "0002", "dps": {"119": False}},
    })
    protocol.dispatcher.callback_status_update(TuyaMessage(1, CMDType.STATUS, 0, b"", 0))
    listener.group_status_updated.assert_called_once_with("0002", {"119": False})
    child.status_updated.assert_not_called()
    assert protocol.dps_cache == {}


async def test_unicast_delta_and_cached_query_are_distinguished():
    listener = EmptyListener()
    child = Mock()
    listener.sub_devices = {"child": child}
    protocol = TuyaProtocol("gateway", "0123456789abcdef", 3.4, False, listener)
    protocol.dps_cache = {"child": {"1": True}}
    protocol._decode_payload = Mock(return_value={"cid": "child", "dps": {"3": 700}})
    msg = TuyaMessage(1, CMDType.STATUS, 0, b"", 0)
    protocol.dispatcher.callback_status_update(msg)
    child.status_updated.assert_called_once_with({"3": 700})
    protocol._decode_payload.return_value = {"cid": "child", "dps": {"1": True}, "data": {"type": "query"}}
    protocol.dispatcher.callback_status_update(msg)
    child.status_query_updated.assert_called_once_with({"1": True})


@pytest.mark.parametrize("failure", ["disconnected", "aborted", "rejected", "write", "negotiate"])
async def test_strict_group_exchange_raises_on_failure(failure):
    listener = EmptyListener()
    protocol = TuyaProtocol("gateway", "0123456789abcdef", 3.4, False, listener)
    protocol.transport = Mock()
    protocol.transport.is_closing.return_value = failure == "disconnected"
    protocol.local_key = b"abcdefghijklmnop"
    protocol.transport_write = AsyncMock()
    protocol.clean_up_session = Mock()
    protocol.dispatcher.wait_for = AsyncMock(return_value=None)
    if failure == "rejected":
        protocol.dispatcher.wait_for.return_value = TuyaMessage(1, CMDType.CONTROL_NEW, 1, b"", 0)
    if failure == "write":
        protocol.transport_write.side_effect = OSError("write failed")
    if failure == "negotiate":
        protocol.local_key = protocol.real_local_key
        protocol._negotiate_session_key = AsyncMock(return_value=False)
    with pytest.raises((ConnectionError, RuntimeError, OSError)):
        await protocol.set_group_dps({"119": True}, "child", "0002")


def test_group_values_survive_stale_queries_but_not_individual_updates():
    device = member()
    device.apply_group_status({"1": False, "3": 300})
    device.status_query_updated({"1": True})
    assert device._status == {"1": False, "3": 300}
    device.status_updated({"4": 500})
    assert device._group_overrides == {"1": False, "3": 300}
    device.status_updated({"1": True})
    assert device._status["1"] is True
    assert "1" not in device._group_overrides


async def test_on_off_use_one_multicast_each_and_synchronize_members():
    entity, rep, devices = group()
    await entity.async_turn_off()
    assert not entity.is_on
    assert all(d._status["1"] is False for d in devices)
    await entity.async_turn_on()
    assert entity.is_on
    assert rep.set_group_dps.call_args_list[0].args == ("0002", {"119": False})
    assert rep.set_group_dps.call_args_list[1].args == ("0002", {"119": True})
    assert rep.set_group_dps.call_count == 2


async def test_white_controls_separate_frames_before_power():
    entity, rep, devices = group()
    for device in devices:
        device._status["1"] = False
    await entity.async_turn_on(brightness=128, color_temp_kelvin=4600)
    assert [call.args[1] for call in rep.set_group_dps.call_args_list] == [
        {"3": 502}, {"4": 500}, {"119": True}]
    assert entity.brightness == 128
    assert entity.color_temp_kelvin == 4600
    assert "entity_id" not in entity.extra_state_attributes


async def test_failed_write_does_not_update_state():
    entity, rep, devices = group()
    rep.set_group_dps.side_effect = ConnectionError()
    with pytest.raises(HomeAssistantError):
        await entity.async_turn_off()
    assert entity.is_on
    assert entity._values == {}


async def test_zero_brightness_turns_off():
    entity, rep, _ = group()
    await entity.async_turn_on(brightness=0)
    rep.set_group_dps.assert_awaited_once_with("0002", {"119": False})


def test_restored_values_filter_unrelated_and_invalid_datapoints():
    entity, _, devices = group()
    entity._apply_values({"119": False, "3": 700, "4": 1000, "125": "0"})
    assert not entity.is_on
    assert entity.brightness == 178
    assert entity.color_temp_kelvin == 6500
    assert "125" not in devices[0]._status
    entity._apply_values({"119": 1, "3": True, "4": 1001})
    assert not entity.is_on and entity.color_temp_kelvin == 6500
