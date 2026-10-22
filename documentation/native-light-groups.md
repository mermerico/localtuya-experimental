# Native BLE mesh light groups

An ordinary Home Assistant light group sends a command to every bulb. A native
Tuya mesh group sends one gateway command per property, allowing its members to
change together. This implementation was tested with a GW018-DM gateway using
protocol 3.4 and six POWERASIA lights. Power, white brightness, color temperature
and RGB were visually verified. Effects and transitions are not implemented.

Create the group in the Tuya app first. This integration does not create groups or
change mesh membership. Obtain its exact local group ID from an authenticated LAN
capture; it is not necessarily the Tuya cloud group ID. Preserve leading zeroes.
The configured member list must match the app group. Changing that list here only
changes state bookkeeping, not which physical lights receive the multicast.

Add to `configuration.yaml`, using your actual LocalTuya device IDs (not entity IDs):

```yaml
localtuya:
  light_groups:
    - unique_id: living_room_native
      name: Living Room Ceiling Lights
      device_id: representative_device_id
      group_id: "0002"
      members:
        - representative_device_id
        - another_device_id
      power_dp: "119"
      brightness_dp: "3"
      temperature_dp: "4"
      color_dp: "11"
      color_mode_dp: "2"
      color_switch_dp: "108"
      white_switch_dp: "107"
      min_kelvin: 2700
      max_kelvin: 6500
```

Restart Home Assistant after changing YAML. Every member must already be configured
as one LocalTuya light on the same gateway. Group DPs are explicit and device-specific:
POWERASIA group power uses 119 even when individual lights use DP 1. Brightness and
temperature are optional; currently their group ranges are fixed at 10–1000 and
0–1000 respectively. Member brightness must use an upper bound of 1000, and its
temperature range/direction must match the group. Overlapping native groups are
rejected to prevent competing inferred member states.

The four RGB fields are optional as a set. Omit all four for a white-only group.
They select the verified POWERASIA protocol: DP 11 is base64 encoding of four bytes,
two for hue in degrees (big endian), one for saturation percent and one for
brightness percent. RGB brightness therefore has 1% resolution. Color mode uses
`colour`/`white`; the color channel must be enabled for RGB and disabled when
returning to white, with the white channel enabled. Each property is sent separately.
The native entity exposes a color picker, retains its hue when dimmed, and returns
to white for color-temperature requests, including those from Adaptive Lighting.
Existing individual bulb entities keep their configured capabilities; enabling RGB
here does not add individual RGB controls to them.

The entity uses the existing gateway connection and encryption. Each changed
property gets a separate CONTROL_NEW command:

```json
{"protocol":5,"t":0,"data":{"cid":"representative_cid","ctype":2,"mbid":"0002","dps":{"119":true}}}
```

`t` is the current Unix timestamp. Group STATUS reports have `protocol: 4` and the
same nested address. Group data is routed separately from individual reports.
Writes fail visibly on transport errors or rejected acknowledgments; successful
commands update the group and known members optimistically. A gateway reply cannot
prove that every physical mesh receiver received a command, so the entity reports
assumed state. Captured brightness and temperature reports were sometimes delayed.

These bulbs can return stale individual power after group commands, and their
brightness/temperature DPs are write-only in individual queries. Group-derived
values therefore survive queries until an individual update or another group
command supersedes them. Last-known group values are restored after HA restarts;
changes made while HA is offline can remain unknown until another report arrives.

Use this entity directly in dashboards, automations and Adaptive Lighting. It does
not expose the `entity_id` member attribute, preventing Adaptive Lighting from
expanding it back into individual commands. Existing helper groups must be retired
or renamed before assigning their old entity ID to the native entity. Keep members
out of room-wide service targets if those targets would otherwise command both the
native group and every individual bulb.

Sources: [POWERASIA DP schema](https://github.com/make-all/tuya-local/issues/2577),
[shared gateway and sequential DP observations](https://github.com/make-all/tuya-local/issues/6224),
[native group envelope discussion](https://github.com/jasonacox/tinytuya/discussions/529#discussioncomment-10684809).
