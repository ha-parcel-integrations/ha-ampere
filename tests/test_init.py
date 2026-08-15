"""Tests for Ampère setup and unload."""
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ampere.api import AmpReApiError, AmpReAuthError
from custom_components.ampere.const import CONF_PARCELS, DOMAIN

from .payloads import (
    PARCEL_TOKEN,
    delivered_sample,
    out_for_delivery_sample,
    parcel_credential,
)

CLIENT = "custom_components.ampere.api.AmpReApiClient"


def _entry(parcel_tokens: list[str] | None = None) -> MockConfigEntry:
    tokens = parcel_tokens or [PARCEL_TOKEN]
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"Ampère parcel …{tokens[0][-6:]}",
        data={CONF_PARCELS: [parcel_credential(parcel_token=t) for t in tokens]},
    )


async def test_setup_and_unload(hass):
    entry = _entry()
    entry.add_to_hass(hass)

    with patch(
        f"{CLIENT}.async_get_parcels",
        new=AsyncMock(return_value=[delivered_sample()]),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert len(entry.runtime_data.clients) == 1

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_delivered_parcels"
    )
    assert entity_id is not None
    delivered = hass.states.get(entity_id)
    assert delivered is not None
    assert delivered.state == "1"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_builds_one_client_per_tracked_parcel(hass):
    """A hub tracking two parcels ends up with two clients, and both are
    polled — merged into one incoming list."""
    entry = _entry(["token-a", "token-b"])
    entry.add_to_hass(hass)

    parcels_by_call = [
        [out_for_delivery_sample(parcel_token="token-a")],
        [out_for_delivery_sample(barcode="AMPBFSD00888888888", parcel_token="token-b")],
    ]

    with patch(
        f"{CLIENT}.async_get_parcels",
        new=AsyncMock(side_effect=lambda: parcels_by_call.pop(0)),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert len(entry.runtime_data.clients) == 2
    assert {c.parcel_token for c in entry.runtime_data.clients} == {
        "token-a",
        "token-b",
    }
    incoming = entry.runtime_data.coordinator.data
    assert {p["barcode"] for p in incoming} == {
        out_for_delivery_sample()["barcode"],
        "AMPBFSD00888888888",
    }


async def test_expired_session_starts_reauth(hass):
    entry = _entry()
    entry.add_to_hass(hass)

    with patch(
        f"{CLIENT}.async_get_parcels",
        new=AsyncMock(side_effect=AmpReAuthError("HTTP 401")),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(
        flow["context"]["source"] == "reauth"
        for flow in hass.config_entries.flow.async_progress()
    )
    # entry.runtime_data must be attached even though the first refresh is
    # what raised — the reauth flow reads coordinator.failed_parcel_token
    # through it to target the one broken parcel instead of asking the user
    # to pick. Attaching it only after a successful first refresh would leave
    # this unset on exactly the path that needs it (see __init__.py).
    assert entry.runtime_data is not None
    assert entry.runtime_data.coordinator.failed_parcel_token == PARCEL_TOKEN


@pytest.mark.parametrize(
    "error",
    [AmpReApiError("HTTP 500"), aiohttp.ClientError("boom")],
)
async def test_outage_retries_instead_of_reauth(hass, error):
    """A 5xx must retry with backoff — never push the user into reauth."""
    entry = _entry()
    entry.add_to_hass(hass)

    with patch(f"{CLIENT}.async_get_parcels", new=AsyncMock(side_effect=error)):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert not hass.config_entries.flow.async_progress()


async def test_per_parcel_sensor_spawn_and_remove(hass):
    entry = _entry()
    entry.add_to_hass(hass)

    parcels = AsyncMock(return_value=[out_for_delivery_sample()])
    with patch(f"{CLIENT}.async_get_parcels", new=parcels):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        registry = er.async_get(hass)
        assert registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_{out_for_delivery_sample()['barcode']}"
        )

        # A later poll reports a different barcode: the summary sensor spawns
        # a new per-parcel sensor and removes the stale one via the registry.
        other = out_for_delivery_sample(barcode="AMPBFSD00999999999")
        parcels.return_value = [other]
        await entry.runtime_data.coordinator.async_request_refresh()
        await hass.async_block_till_done()

        assert registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_AMPBFSD00999999999"
        )
        assert (
            registry.async_get_entity_id(
                "sensor",
                DOMAIN,
                f"{entry.entry_id}_{out_for_delivery_sample()['barcode']}",
            )
            is None
        )
