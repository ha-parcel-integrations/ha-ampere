"""Tests for the contents action and the store lifecycle."""

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ampere.const import CONF_PARCELS, CONF_TRACKING_CODE, DOMAIN

from .payloads import (
    BARCODE,
    PARCEL_TOKEN,
    delivered_sample,
    out_for_delivery_sample,
    parcel_credential,
)

CLIENT = "custom_components.ampere.api.AmpReApiClient"
UNTRACKED = "AMPXXXX00000000000"
STORE_KEY = f"{DOMAIN}.contents.%s"


def _config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Ampère",
        data={CONF_PARCELS: [parcel_credential()]},
    )


async def _setup(hass, entry=None, parcels=None) -> MockConfigEntry:
    entry = entry or _config_entry()
    parcels = [out_for_delivery_sample()] if parcels is None else parcels
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.async_get_parcels", new=AsyncMock(return_value=parcels)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def _call(hass, data: dict) -> None:
    await hass.services.async_call(DOMAIN, "update_contents", data, blocking=True)
    await hass.async_block_till_done()


def _contents(entry: MockConfigEntry, code: str = BARCODE) -> str | None:
    parcels = {p["barcode"]: p for p in entry.runtime_data.coordinator.data}
    return parcels[code]["contents"]


async def test_a_parcel_has_no_contents_without_a_stored_value(hass):
    entry = await _setup(hass)
    assert _contents(entry) is None


async def test_update_contents_sets_replaces_and_clears(hass):
    entry = await _setup(hass)

    await _call(hass, {CONF_TRACKING_CODE: BARCODE, "contents": " Running  shoes "})
    assert _contents(entry) == "Running shoes"

    await _call(hass, {CONF_TRACKING_CODE: BARCODE, "contents": "Winter socks"})
    assert _contents(entry) == "Winter socks"

    await _call(hass, {CONF_TRACKING_CODE: BARCODE, "contents": "  "})
    assert _contents(entry) is None


async def test_update_contents_without_text_clears(hass):
    entry = await _setup(hass)
    await _call(hass, {CONF_TRACKING_CODE: BARCODE, "contents": "Running shoes"})

    await _call(hass, {CONF_TRACKING_CODE: BARCODE})

    assert _contents(entry) is None


async def test_update_contents_normalizes_the_code(hass):
    entry = await _setup(hass)
    await _call(hass, {CONF_TRACKING_CODE: f" {BARCODE.lower()} ", "contents": "Shoes"})
    assert _contents(entry) == "Shoes"


async def test_update_contents_reaches_a_delivered_parcel(hass, freezer):
    freezer.move_to("2026-08-13T12:00:00+00:00")
    entry = await _setup(hass, parcels=[delivered_sample()])
    await _call(hass, {CONF_TRACKING_CODE: BARCODE, "contents": "Running shoes"})

    delivered = entry.runtime_data.coordinator.delivered
    assert [p["contents"] for p in delivered] == ["Running shoes"]


async def test_update_contents_works_for_a_code_that_is_not_listed(hass):
    entry = await _setup(hass)
    await _call(hass, {CONF_TRACKING_CODE: UNTRACKED, "contents": "Running shoes"})

    store = entry.runtime_data.coordinator.contents_store
    assert store.get(UNTRACKED) == "Running shoes"


async def test_update_contents_never_polls_the_carrier(hass):
    entry = await _setup(hass)
    coordinator = entry.runtime_data.coordinator
    data = {CONF_TRACKING_CODE: BARCODE, "contents": "Running shoes"}
    with (
        patch(f"{CLIENT}.async_get_parcels", new=AsyncMock()) as fetch,
        patch.object(
            coordinator,
            "async_update_listeners",
            wraps=coordinator.async_update_listeners,
        ) as notify,
    ):
        await _call(hass, data)
        assert notify.call_count == 1
        await _call(hass, data)
        assert notify.call_count == 1
        await _call(hass, {CONF_TRACKING_CODE: UNTRACKED})
        assert notify.call_count == 1
    fetch.assert_not_called()


async def test_update_contents_rejects_an_empty_code(hass):
    await _setup(hass)
    with pytest.raises(ServiceValidationError):
        await _call(hass, {CONF_TRACKING_CODE: "  "})


async def test_update_contents_needs_a_loaded_entry(hass):
    entry = await _setup(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    try:
        with pytest.raises(ServiceValidationError):
            await _call(hass, {CONF_TRACKING_CODE: BARCODE, "contents": "Shoes"})
    finally:
        entry.mock_state(hass, ConfigEntryState.LOADED)


async def test_the_action_goes_when_the_entry_unloads(hass):
    entry = await _setup(hass)
    assert hass.services.has_service(DOMAIN, "update_contents")

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not hass.services.has_service(DOMAIN, "update_contents")


async def test_a_stored_value_survives_a_restart_of_the_entry(hass, hass_storage):
    entry = await _setup(hass)
    await _call(hass, {CONF_TRACKING_CODE: BARCODE, "contents": "Running shoes"})
    store = entry.runtime_data.coordinator.contents_store
    await store._store.async_save(store._data_to_save())

    assert await hass.config_entries.async_unload(entry.entry_id)
    with patch(
        f"{CLIENT}.async_get_parcels",
        new=AsyncMock(return_value=[out_for_delivery_sample()]),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert _contents(entry) == "Running shoes"


async def test_removing_the_entry_removes_its_store(hass, hass_storage):
    entry = await _setup(hass)
    await _call(hass, {CONF_TRACKING_CODE: BARCODE, "contents": "Running shoes"})
    store = entry.runtime_data.coordinator.contents_store
    await store._store.async_save(store._data_to_save())
    key = STORE_KEY % entry.entry_id
    assert key in hass_storage

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert key not in hass_storage


async def test_a_contents_change_alone_fires_no_event(hass):
    await _setup(hass)
    events = []
    for name in ("registered", "status_changed", "delivered"):
        hass.bus.async_listen(f"{DOMAIN}_parcel_{name}", events.append)

    await _call(hass, {CONF_TRACKING_CODE: BARCODE, "contents": "Running shoes"})
    await hass.async_block_till_done()

    assert events == []


async def test_a_text_set_before_the_barcode_arrives_follows_the_parcel(hass):
    entry = await _setup(hass, parcels=[out_for_delivery_sample(barcode=None)])
    coordinator = entry.runtime_data.coordinator
    assert coordinator.data[0]["barcode"] == PARCEL_TOKEN

    await _call(hass, {CONF_TRACKING_CODE: PARCEL_TOKEN, "contents": "Running shoes"})
    assert coordinator.data[0]["contents"] == "Running shoes"

    with patch(
        f"{CLIENT}.async_get_parcels",
        new=AsyncMock(return_value=[out_for_delivery_sample()]),
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert coordinator.data[0]["barcode"] == BARCODE
    assert coordinator.data[0]["contents"] == "Running shoes"
    assert coordinator.contents_store.get(BARCODE) == "Running shoes"
    assert coordinator.contents_store.get(PARCEL_TOKEN) is None


async def test_a_barcode_text_wins_over_a_stale_token_text(hass):
    entry = await _setup(hass)
    coordinator = entry.runtime_data.coordinator
    store = coordinator.contents_store
    store.async_set(BARCODE, "Winter socks")
    store.async_set(PARCEL_TOKEN, "Running shoes")

    coordinator.async_reapply_contents()

    assert coordinator.data[0]["contents"] == "Winter socks"
    assert store.get(PARCEL_TOKEN) is None
