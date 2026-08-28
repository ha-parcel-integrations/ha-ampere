"""Tests for the Ampère config and options flow."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import SOURCE_USER
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ampere.api import AmpReApiError, AmpReAuthError
from custom_components.ampere.config_flow import CONF_TRACKING_LINK, _parcel_list_value
from custom_components.ampere.const import (
    CONF_BARCODE,
    CONF_COOKIE,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_PARCEL_TOKEN,
    CONF_PARCELS,
    CONF_REFRESH_INTERVAL,
    DOMAIN,
)

from .payloads import COOKIE, PARCEL_TOKEN, TRACKING_LINK, parcel_credential

LINK_INPUT = {CONF_TRACKING_LINK: TRACKING_LINK}
SECOND_TOKEN = "second-opaque-token"
SECOND_LINK = "https://link.bol.com/t/othermailtoken?notificationId=5678"

EXCHANGE = "custom_components.ampere.config_flow.async_exchange_tracking_link"


def _entry(parcel_tokens: list[str] | None = None) -> MockConfigEntry:
    tokens = [PARCEL_TOKEN] if parcel_tokens is None else parcel_tokens
    return MockConfigEntry(
        domain=DOMAIN,
        title="Ampère",
        data={CONF_PARCELS: [parcel_credential(parcel_token=t) for t in tokens]},
        options={
            CONF_DELIVERED_FILTER_TYPE: "days",
            CONF_DELIVERED_FILTER_AMOUNT: 7,
            CONF_INCLUDE_HISTORY: False,
            CONF_REFRESH_INTERVAL: 30,
        },
    )


def _attach_coordinator(entry: MockConfigEntry, failed_parcel_token: str | None = None):
    """Attach a lightweight fake runtime_data.coordinator, as __init__.py would."""
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(
            failed_parcel_token=failed_parcel_token, data=[], delivered=[]
        )
    )


# ---------------------------------------------------------------------------
# user step
# ---------------------------------------------------------------------------


async def test_user_flow_creates_empty_hub_with_no_input(hass):
    """Single instance, no account, no postcode: no form, no exchange."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    assert result["type"] == "create_entry"
    assert result["title"] == "Ampère"
    assert result["data"] == {CONF_PARCELS: []}
    assert result["options"][CONF_REFRESH_INTERVAL] == 30


async def test_user_flow_is_single_instance(hass):
    """single_config_entry in the manifest — only one Ampère hub can exist."""
    _entry([]).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    assert result["type"] == "abort"
    assert result["reason"] == "single_instance_allowed"


# ---------------------------------------------------------------------------
# reauth
# ---------------------------------------------------------------------------


async def test_reauth_updates_the_cookie_and_token(hass):
    """A single-parcel hub has no ambiguity — that one parcel is always the
    target, and the stored link is re-exchanged with no form input at all.
    The exchange returning a *different* parcel-token (confirmed live —
    every re-open of a mail link mints a fresh one for the same physical
    parcel) must be accepted, not rejected: there is no link typed by the
    user here to compare it against."""
    entry = _entry()
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    assert result["data_schema"].schema == {}

    new_token = "freshly-minted-token"
    with patch(
        EXCHANGE, new=AsyncMock(return_value=("new-cookie", new_token))
    ) as exchange:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()

    assert exchange.await_args.args[1] == TRACKING_LINK
    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PARCELS] == [
        {
            CONF_COOKIE: "new-cookie",
            CONF_PARCEL_TOKEN: new_token,
            CONF_TRACKING_LINK: TRACKING_LINK,
        }
    ]


async def test_reauth_surfaces_invalid_link(hass):
    entry = _entry()
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    with patch(EXCHANGE, new=AsyncMock(side_effect=AmpReAuthError("HTTP 404"))):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_link"}


async def test_reauth_surfaces_connection_errors(hass):
    entry = _entry()
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    with patch(EXCHANGE, new=AsyncMock(side_effect=AmpReApiError("HTTP 500"))):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["errors"] == {"base": "cannot_connect"}


async def test_reauth_multi_parcel_targets_the_failed_one(hass):
    """`coordinator.failed_parcel_token` picks the target without asking,
    and re-exchanges *that* parcel's own stored link — not the other one's."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Ampère",
        data={
            CONF_PARCELS: [
                parcel_credential(
                    parcel_token=PARCEL_TOKEN, tracking_link=TRACKING_LINK
                ),
                parcel_credential(parcel_token=SECOND_TOKEN, tracking_link=SECOND_LINK),
            ]
        },
    )
    entry.add_to_hass(hass)
    _attach_coordinator(entry, failed_parcel_token=SECOND_TOKEN)

    result = await entry.start_reauth_flow(hass)
    assert result["data_schema"].schema == {}

    with patch(
        EXCHANGE, new=AsyncMock(return_value=("new-cookie", SECOND_TOKEN))
    ) as exchange:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()

    assert exchange.await_args.args[1] == SECOND_LINK
    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    by_token = {p[CONF_PARCEL_TOKEN]: p for p in entry.data[CONF_PARCELS]}
    assert by_token[SECOND_TOKEN][CONF_COOKIE] == "new-cookie"
    assert by_token[PARCEL_TOKEN][CONF_COOKIE] == COOKIE  # untouched


async def test_reauth_confirm_with_no_parcels_left_aborts(hass):
    """The failing parcel could have been removed via remove_parcel while a
    reauth flow was still pending on it — confirming then must not crash."""
    entry = _entry()
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)

    hass.config_entries.async_update_entry(entry, data={CONF_PARCELS: []})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] == "abort"
    assert result["reason"] == "no_parcels"


async def test_reauth_multi_parcel_ambiguous_asks_user_to_pick(hass):
    """No coordinator / no failed_parcel_token known — the form asks which
    parcel instead of guessing, then reuses *that* parcel's own stored link."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Ampère",
        data={
            CONF_PARCELS: [
                parcel_credential(
                    parcel_token=PARCEL_TOKEN, tracking_link=TRACKING_LINK
                ),
                parcel_credential(parcel_token=SECOND_TOKEN, tracking_link=SECOND_LINK),
            ]
        },
    )
    entry.add_to_hass(hass)
    # No runtime_data attached at all — matches "first refresh after setup failed".

    result = await entry.start_reauth_flow(hass)
    assert CONF_PARCEL_TOKEN in result["data_schema"].schema
    assert CONF_TRACKING_LINK not in result["data_schema"].schema

    with patch(
        EXCHANGE, new=AsyncMock(return_value=("new-cookie", SECOND_TOKEN))
    ) as exchange:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PARCEL_TOKEN: SECOND_TOKEN}
        )
        await hass.async_block_till_done()

    assert exchange.await_args.args[1] == SECOND_LINK
    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    by_token = {p[CONF_PARCEL_TOKEN]: p for p in entry.data[CONF_PARCELS]}
    assert by_token[SECOND_TOKEN][CONF_COOKIE] == "new-cookie"
    assert by_token[PARCEL_TOKEN][CONF_COOKIE] == COOKIE


# ---------------------------------------------------------------------------
# options — menu
# ---------------------------------------------------------------------------


async def _open_options_step(hass, entry, step_id: str):
    """Start the options flow and select one of its two top-level routes."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "menu"
    assert result["menu_options"] == ["parcels", "settings"]
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step_id}
    )


async def test_options_parcel_list_accepts_a_tracking_link(hass):
    """A new list item is exchanged into Ampère's stored credential."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_PARCELS: []},
    )
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "parcels")
    with patch(EXCHANGE, new=AsyncMock(return_value=(COOKIE, PARCEL_TOKEN))):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"tracking_codes": [TRACKING_LINK]}
        )

    assert result["type"] == "abort"
    assert result["reason"] == "parcels_updated"
    assert entry.data[CONF_PARCELS] == [parcel_credential()]


async def test_options_parcel_list_uses_cached_tracking_codes(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_PARCELS: [{**parcel_credential(), CONF_BARCODE: "AMP123"}]},
    )
    entry.add_to_hass(hass)
    await _open_options_step(hass, entry, "parcels")
    assert _parcel_list_value(entry.data[CONF_PARCELS][0]) == "AMP123"


async def test_options_parcel_list_can_be_cleared(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "parcels")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tracking_codes": []}
    )

    assert result["reason"] == "parcels_updated"
    assert entry.data[CONF_PARCELS] == []


async def test_options_rejects_a_new_code_without_its_tracking_link(hass):
    entry = _entry([])
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "parcels")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tracking_codes": ["AMP123"]}
    )

    assert result["errors"] == {"base": "tracking_link_required"}


async def test_options_settings_preserve_parcel_list(hass):
    """Saving settings must never replace the manually tracked parcel list."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_PARCELS: [parcel_credential()]})
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "settings")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_DELIVERED_FILTER_TYPE: "days",
            CONF_DELIVERED_FILTER_AMOUNT: 7,
            CONF_INCLUDE_HISTORY: False,
            CONF_REFRESH_INTERVAL: "30",
        },
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_REFRESH_INTERVAL] == 30
