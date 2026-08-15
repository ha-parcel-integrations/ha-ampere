"""The device every entity of this integration belongs to.

One place, because sensors, the button and the calendar must all land on the
*same* device entry — every tracked parcel's entities included, since
``single_config_entry`` means Ampère is single-hub, single-device (see
:func:`build_device_info` below).
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN

# Ampère has no public marketing site of its own (the tracking host,
# bol.prd.amperebezorgt.nl, requires the per-parcel session cookie and is not
# a page a user should open from a device card) — point at bol.com itself,
# whose own brand is what the tracking page reports
# (`company-name-value` == "bol").
CONFIGURATION_URL = "https://www.bol.com"

ATTRIBUTION = "Data provided by Ampère"


def build_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Return the DeviceInfo shared by every entity of this hub.

    ``single_config_entry`` means there is only ever one Ampère entry, and it
    can track several parcels (``const.CONF_PARCELS``) — so this is one
    device for the whole hub, not one per parcel: every per-parcel sensor,
    the summary/delivered sensors, the button and the calendar all land on
    it. ``entry.title`` is always "Ampère" (set at creation in
    ``config_flow.py``), so no extra wrapping is needed.
    """
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="Ampère",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url=CONFIGURATION_URL,
    )
