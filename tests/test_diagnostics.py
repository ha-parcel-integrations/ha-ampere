"""Tests for Ampère diagnostics."""
from datetime import timedelta
from unittest.mock import MagicMock

from custom_components.ampere.diagnostics import (
    TO_REDACT,
    async_get_config_entry_diagnostics,
)


async def test_diagnostics_redacts_and_counts(hass):
    """Diagnostics get pasted into public issues — nothing identifying may survive."""
    entry = MagicMock()
    entry.options = {}
    entry.runtime_data.coordinator.data = [
        {
            "barcode": "AMPBFSD00123456789",
            "sender": "bol",
            "receiver": "1234 AB Amsterdam",
            "status": "out_for_delivery",
            "raw": {
                "parcel_token": "abc123opaquetoken",
                "barcode": "AMPBFSD00123456789",
                "address": "1234 AB Amsterdam",
            },
        }
    ]
    entry.runtime_data.coordinator.delivered = []
    entry.runtime_data.coordinator.current_tier_minutes = 15
    entry.runtime_data.coordinator.update_interval = timedelta(minutes=15)

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["polling"] == {
        "current_tier_minutes": 15,
        "update_interval_seconds": 900.0,
    }
    assert result["counts"] == {"incoming_active": 1, "delivered": 0}
    # barcode, address and the internal parcel-token are redacted, at every
    # nesting level — including inside the raw payload.
    assert result["incoming"][0]["barcode"] == "**REDACTED**"
    assert result["incoming"][0]["receiver"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["parcel_token"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["barcode"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["address"] == "**REDACTED**"
    # non-identifying fields survive, or the diagnostics would be useless
    assert result["incoming"][0]["status"] == "out_for_delivery"


def test_diagnostics_redact_list_covers_the_session_cookie():
    """The tnt_sessions cookie is a bearer-equivalent credential — belt and
    suspenders even though entry.data is not currently included above."""
    assert "cookie" in TO_REDACT
    assert "parcel_token" in TO_REDACT
