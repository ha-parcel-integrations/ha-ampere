"""Diagnostics support for the Ampère parcel tracker integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import AmpReConfigEntry

# Diagnostics are pasted into public issues, so redact anything that
# identifies a person, an address or a specific parcel. Over-redacting is
# cheap; under-redacting leaks a user's home address into a GitHub thread.
#
# The field list covers:
#
# * the delivery-address block (postcode + city + any street data),
# * the barcode value,
# * critically, the ``tnt_sessions`` cookie value and the opaque-key-token it
#   carries — these are bearer-equivalent credentials and must never be
#   logged, not even in debug diagnostics,
# * ``parcel_token`` too, per the suite default, though it is only
#   meaningful in combination with the cookie.
#
# ``async_redact_data`` matches dict *keys* recursively, so this list catches
# every occurrence — including inside a normalised parcel's nested ``raw``
# dict — without walking the structure by hand.
TO_REDACT = {
    # canonical fields we publish ourselves
    "tracking_code",
    "barcode",
    "sender",
    "receiver",
    "url",
    # carrier payload fields (api.py's raw dict + entry.data)
    "parcel_token",
    "cookie",
    "address",
    "receiver_name",
    "postal_code",
    "postalCode",
    "city",
    "street",
    "email",
    "name",
    "driver",
    "signature",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AmpReConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the Ampère config entry."""
    coordinator = entry.runtime_data.coordinator

    return {
        "entry_options": async_redact_data(dict(entry.options), TO_REDACT),
        "polling": {
            "current_tier_minutes": coordinator.current_tier_minutes,
            "update_interval_seconds": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None
            ),
        },
        "counts": {
            "incoming_active": len(coordinator.data or []),
            "delivered": len(coordinator.delivered or []),
        },
        "incoming": async_redact_data(coordinator.data or [], TO_REDACT),
        "delivered": async_redact_data(coordinator.delivered or [], TO_REDACT),
    }
