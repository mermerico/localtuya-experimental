"""Native mesh groups configured explicitly from known app group identifiers."""

import asyncio
import base64
import binascii
import logging
from statistics import mean

import voluptuous as vol
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_HS_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
CONF_LIGHT_GROUPS = "light_groups"
DP = vol.All(vol.Coerce(str), vol.Match(r"^[1-9][0-9]*$"))
RGB_DPS = ("color_dp", "color_mode_dp", "color_switch_dp", "white_switch_dp")
GROUP_DPS = ("power_dp", "brightness_dp", "temperature_dp", *RGB_DPS)


def decode_color(value):
    """Decode the verified POWERASIA base64 HH/S/V byte layout."""
    if not isinstance(value, str):
        return None
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        return None
    if len(raw) != 4:
        return None
    hue, saturation, brightness = int.from_bytes(raw[:2], "big"), raw[2], raw[3]
    if hue > 360 or saturation > 100 or brightness > 100:
        return None
    return hue, saturation, brightness


def encode_color(hs, brightness):
    """Encode degrees, saturation percent and HA brightness for the mesh."""
    hue = max(0, min(360, round(hs[0])))
    saturation = max(0, min(100, round(hs[1])))
    value = max(1, min(100, round(brightness * 100 / 255)))
    return base64.b64encode(bytes([hue >> 8, hue & 255, saturation, value])).decode()


def validate_group(config):
    """Reject ambiguous identities and invalid ranges before adding entities."""
    if config["device_id"] not in config["members"]:
        raise vol.Invalid("The representative device must be a member")
    if len(set(config["members"])) != len(config["members"]):
        raise vol.Invalid("Duplicate group member")
    if config["min_kelvin"] >= config["max_kelvin"]:
        raise vol.Invalid("min_kelvin must be less than max_kelvin")
    dps = [config[k] for k in GROUP_DPS if k in config]
    if len(dps) != len(set(dps)):
        raise vol.Invalid("Group datapoints must be distinct")
    if "temperature_dp" in config and "brightness_dp" not in config:
        raise vol.Invalid("Temperature control requires brightness control")
    if any(k in config for k in RGB_DPS) and not all(k in config for k in RGB_DPS):
        raise vol.Invalid(
            "RGB requires color, mode, color-switch and white-switch datapoints"
        )
    return config


GROUP_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required("unique_id"): cv.string,
            vol.Required("name"): cv.string,
            vol.Required("device_id"): cv.string,
            vol.Required("group_id"): vol.All(str, vol.Length(min=1)),
            vol.Required("members"): vol.All(
                cv.ensure_list, [cv.string], vol.Length(min=1)
            ),
            vol.Required("power_dp"): DP,
            vol.Optional("brightness_dp"): DP,
            vol.Optional("temperature_dp"): DP,
            vol.Optional("color_dp"): DP,
            vol.Optional("color_mode_dp"): DP,
            vol.Optional("color_switch_dp"): DP,
            vol.Optional("white_switch_dp"): DP,
            vol.Optional("min_kelvin", default=2700): cv.positive_int,
            vol.Optional("max_kelvin", default=6500): cv.positive_int,
        }
    ),
    validate_group,
)


def validate_groups(groups):
    """Unique entity IDs and disjoint members avoid competing state overrides."""
    ids, members = set(), set()
    for group in groups:
        if group["unique_id"] in ids or members.intersection(group["members"]):
            raise vol.Invalid(
                "Native light groups need unique IDs and disjoint members"
            )
        ids.add(group["unique_id"])
        members.update(group["members"])
    return groups


GROUPS_SCHEMA = vol.All(cv.ensure_list, [GROUP_SCHEMA], validate_groups)


async def async_setup_groups(hass, entry, async_add_entities):
    """Attach each configured group to its representative's config entry."""
    configs = entry.data["devices"]
    runtime = hass.data[DOMAIN][entry.entry_id].devices
    entities, addresses = [], set()
    for config in hass.data[DOMAIN].get(CONF_LIGHT_GROUPS, []):
        if config["device_id"] not in configs:
            continue
        try:
            members = []
            representative = None
            for device_id in config["members"]:
                device_config = configs[device_id]
                host, cid = device_config["host"], device_config["node_id"]
                device = runtime[f"{host}_{cid}"]
                lights = [
                    e for e in device_config["entities"] if e["platform"] == "light"
                ]
                if len(lights) != 1 or float(device_config["protocol_version"]) != 3.4:
                    raise ValueError("Each group member needs one protocol-3.4 light")
                light = lights[0]
                mapping = {config["power_dp"]: light["id"]}
                for group_key, member_key in (
                    ("brightness_dp", "brightness"),
                    ("temperature_dp", "color_temp"),
                ):
                    if group_key in config:
                        mapping[config[group_key]] = light[member_key]
                if (
                    "brightness_dp" in config
                    and light.get("brightness_upper", 1000) != 1000
                ):
                    raise ValueError("Native groups require 0-1000 member brightness")
                if "temperature_dp" in config and (
                    light.get("color_temp_reverse", False)
                    or light.get("color_temp_min_kelvin", 2700) != config["min_kelvin"]
                    or light.get("color_temp_max_kelvin", 6500) != config["max_kelvin"]
                ):
                    raise ValueError(
                        "Members must use the same temperature range and direction"
                    )
                members.append((device, mapping))
                if device_id == config["device_id"]:
                    representative = device
            if (
                representative is None
                or representative.gateway is None
                or any(d.gateway is not representative.gateway for d, _ in members)
            ):
                raise ValueError("All members must share one gateway")
            address = (representative.gateway.id, config["group_id"])
            if address in addresses:
                raise ValueError("Duplicate native group address")
            addresses.add(address)
            entities.append(NativeLightGroup(config, representative, members))
        except (KeyError, ValueError) as exc:
            _LOGGER.error("Cannot set up native group %s: %s", config["name"], exc)
    async_add_entities(entities)


class NativeLightGroup(RestoreEntity, LightEntity):
    """One gateway multicast per property, with no individual-light fan-out."""

    _attr_should_poll = False
    # Gateway acceptance does not prove that every mesh receiver heard a command.
    _attr_assumed_state = True

    def __init__(self, config, representative, members):
        self.config = config
        self.representative = representative
        self.members = members
        for member, _ in members:
            member._native_group_member = True
        self._attr_unique_id = f"native_group_{config['unique_id']}"
        self._attr_name = config["name"]
        self._attr_min_color_temp_kelvin = config["min_kelvin"]
        self._attr_max_color_temp_kelvin = config["max_kelvin"]
        mode = ColorMode.ONOFF
        if "brightness_dp" in config:
            mode = ColorMode.BRIGHTNESS
        if "temperature_dp" in config:
            mode = ColorMode.COLOR_TEMP
        self._attr_supported_color_modes = {mode}
        if "color_dp" in config:
            self._attr_supported_color_modes.add(ColorMode.HS)
            self._attr_supported_color_modes.discard(ColorMode.ONOFF)
            self._attr_supported_color_modes.discard(ColorMode.BRIGHTNESS)
            if mode in (ColorMode.ONOFF, ColorMode.BRIGHTNESS):
                mode = ColorMode.HS
        self._attr_color_mode = mode
        self._values = {}
        self._command_lock = asyncio.Lock()

    @property
    def available(self):
        return bool(self.representative.connected)

    @property
    def is_on(self):
        dp = self.config["power_dp"]
        states = [d._status.get(mapping[dp]) for d, mapping in self.members]
        known = [value for value in states if isinstance(value, bool)]
        return any(known) if known else self._values.get(dp)

    def _mean_value(self, group_key):
        dp = self.config.get(group_key)
        values = [d._status.get(mapping.get(dp)) for d, mapping in self.members]
        known = [
            v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)
        ]
        return mean(known) if known else self._values.get(dp)

    @property
    def brightness(self):
        if self.color_mode == ColorMode.HS:
            color = decode_color(self._values.get(self.config.get("color_dp")))
            return round(color[2] * 255 / 100) if color else None
        value = self._mean_value("brightness_dp")
        return round(value * 255 / 1000) if value is not None else None

    @property
    def color_temp_kelvin(self):
        if self.color_mode == ColorMode.HS:
            return None
        value = self._mean_value("temperature_dp")
        if value is None:
            return None
        return round(
            self.min_color_temp_kelvin
            + value / 1000 * (self.max_color_temp_kelvin - self.min_color_temp_kelvin)
        )

    @property
    def hs_color(self):
        color = decode_color(self._values.get(self.config.get("color_dp")))
        return color[:2] if color else None

    @property
    def extra_state_attributes(self):
        # Deliberately no entity_id attribute: Adaptive Lighting should address
        # this native device, not expand it into individual light commands.
        return {
            "native_group_id": self.config["group_id"],
            "native_group_dps": dict(self._values),
        }

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        signal = f"localtuya_group_{self.representative.gateway.id}_{self.config['group_id']}"
        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal, self._group_report)
        )
        for member, _ in self.members:
            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass, f"localtuya_{member.id}", self._member_report
                )
            )
        if (last := await self.async_get_last_state()) is not None:
            self._apply_values(last.attributes.get("native_group_dps", {}))

    @callback
    def _member_report(self, _status):
        # Save the aggregate after individual overrides as well as group writes.
        if self.is_on is not None:
            self._values[self.config["power_dp"]] = self.is_on
        for key in ("brightness_dp", "temperature_dp"):
            if key in self.config and (value := self._mean_value(key)) is not None:
                self._values[self.config[key]] = round(value)
        self.async_write_ha_state()

    def _apply_values(self, values):
        allowed = {self.config[k] for k in GROUP_DPS if k in self.config}
        values = {
            dp: value
            for dp, value in values.items()
            if dp in allowed
            and (
                (
                    dp
                    in {
                        self.config.get(k)
                        for k in ("power_dp", "color_switch_dp", "white_switch_dp")
                    }
                    and type(value) is bool
                )
                or (
                    dp
                    in {self.config.get(k) for k in ("brightness_dp", "temperature_dp")}
                    and type(value) is int
                    and 0 <= value <= 1000
                )
                or (
                    dp == self.config.get("color_mode_dp")
                    and value in ("white", "colour")
                )
                or (
                    dp == self.config.get("color_dp")
                    and decode_color(value) is not None
                )
            )
        }
        self._values.update(values)
        if "color_dp" in self.config:
            mode = values.get(self.config["color_mode_dp"])
            color_switch = values.get(self.config["color_switch_dp"])
            if mode == "colour" or (mode is None and color_switch is True):
                self._attr_color_mode = ColorMode.HS
            elif mode == "white" or (mode is None and color_switch is False):
                self._attr_color_mode = (
                    ColorMode.COLOR_TEMP
                    if "temperature_dp" in self.config
                    else ColorMode.HS
                )
        for member, mapping in self.members:
            # RGB is native-group state; existing white-only member entities do
            # not suddenly gain unsupported individual RGB controls.
            translated = {
                mapping[dp]: value for dp, value in values.items() if dp in mapping
            }
            if translated:
                member.apply_group_status(translated)

    @callback
    def _group_report(self, values):
        self._apply_values(values)
        self.async_write_ha_state()

    async def _send(self, dp, value):
        try:
            await self.representative.set_group_dps(
                self.config["group_id"], {dp: value}
            )
        except Exception as exc:
            raise HomeAssistantError("Native light group command failed") from exc
        # Optimistic until a group report arrives; never update on a failed send.
        self._apply_values({dp: value})
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs):
        if kwargs.get(ATTR_BRIGHTNESS) == 0:
            return await self.async_turn_off()
        async with self._command_lock:
            rgb = "color_dp" in self.config
            previous_brightness = self.brightness
            use_color = rgb and (
                ATTR_HS_COLOR in kwargs
                or (
                    self.color_mode == ColorMode.HS
                    and ATTR_COLOR_TEMP_KELVIN not in kwargs
                )
            )
            if use_color:
                switching_to_color = self.color_mode != ColorMode.HS
                if (
                    switching_to_color
                    or self._values.get(self.config["color_switch_dp"]) is not True
                ):
                    await self._send(self.config["color_switch_dp"], True)
                if (
                    switching_to_color
                    or self._values.get(self.config["color_mode_dp"]) != "colour"
                ):
                    await self._send(self.config["color_mode_dp"], "colour")
                if ATTR_HS_COLOR in kwargs or ATTR_BRIGHTNESS in kwargs:
                    hs = kwargs.get(ATTR_HS_COLOR, self.hs_color or (0, 100))
                    brightness = kwargs.get(ATTR_BRIGHTNESS, previous_brightness or 255)
                    await self._send(
                        self.config["color_dp"], encode_color(hs, brightness)
                    )
                if not self.is_on or not kwargs:
                    await self._send(self.config["power_dp"], True)
                return
            if rgb and ATTR_COLOR_TEMP_KELVIN in kwargs:
                switching_to_white = self.color_mode == ColorMode.HS
                if self.color_mode == ColorMode.HS and ATTR_BRIGHTNESS not in kwargs:
                    kwargs = {**kwargs, ATTR_BRIGHTNESS: previous_brightness or 255}
                for key, value in (
                    ("color_switch_dp", False),
                    ("color_mode_dp", "white"),
                    ("white_switch_dp", True),
                ):
                    # Channel switches may be mutually exclusive in firmware;
                    # reassert white on a mode change even if its cache says on.
                    if (
                        switching_to_white
                        or self._values.get(self.config[key]) != value
                    ):
                        await self._send(self.config[key], value)
            # Separate property frames are required by POWERASIA BLE lights.
            # Pre-stage brightness to avoid flashing at the previous level.
            if ATTR_BRIGHTNESS in kwargs and "brightness_dp" in self.config:
                value = max(10, min(1000, round(kwargs[ATTR_BRIGHTNESS] * 1000 / 255)))
                await self._send(self.config["brightness_dp"], value)
            if ATTR_COLOR_TEMP_KELVIN in kwargs and "temperature_dp" in self.config:
                value = round(
                    1000
                    * (kwargs[ATTR_COLOR_TEMP_KELVIN] - self.min_color_temp_kelvin)
                    / (self.max_color_temp_kelvin - self.min_color_temp_kelvin)
                )
                await self._send(
                    self.config["temperature_dp"], max(0, min(1000, value))
                )
            if not self.is_on or not kwargs:
                await self._send(self.config["power_dp"], True)

    async def async_turn_off(self, **kwargs):
        async with self._command_lock:
            await self._send(self.config["power_dp"], False)
