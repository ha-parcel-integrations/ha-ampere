"""Tests for the Ampère config and options flow."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from homeassistant.config_entries import SOURCE_USER
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ampere.api import AmpReApiError, AmpReAuthError
from custom_components.ampere.config_flow import CONF_TRACKING_LINK
from custom_components.ampere.const import (
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
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {}
        )
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
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {}
        )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_link"}


async def test_reauth_surfaces_connection_errors(hass):
    entry = _entry()
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    with patch(EXCHANGE, new=AsyncMock(side_effect=AmpReApiError("HTTP 500"))):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {}
        )

    assert result["errors"] == {"base": "cannot_connect"}


async def test_reauth_multi_parcel_targets_the_failed_one(hass):
    """`coordinator.failed_parcel_token` picks the target without asking,
    and re-exchanges *that* parcel's own stored link — not the other one's."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Ampère",
        data={
            CONF_PARCELS: [
                parcel_credential(parcel_token=PARCEL_TOKEN, tracking_link=TRACKING_LINK),
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
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {}
        )
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
                parcel_credential(parcel_token=PARCEL_TOKEN, tracking_link=TRACKING_LINK),
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


async def test_options_menu_order_is_parcel_actions_then_settings(hass):
    entry = _entry([PARCEL_TOKEN, SECOND_TOKEN])
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] == "menu"
    assert result["menu_options"] == ["add_parcel", "remove_parcel", "settings"]


async def test_options_menu_omits_remove_with_no_parcels(hass):
    entry = _entry([])
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["menu_options"] == ["add_parcel", "settings"]


# ---------------------------------------------------------------------------
# options — settings
# ---------------------------------------------------------------------------


async def test_options_flow_saves_and_reloads(hass):
    entry = _entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    assert result["step_id"] == "settings"

    with patch.object(
        hass.config_entries, "async_schedule_reload"
    ) as schedule_reload:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                "delivered": {
                    CONF_DELIVERED_FILTER_TYPE: "parcels",
                    CONF_DELIVERED_FILTER_AMOUNT: 5,
                },
                "history": {CONF_INCLUDE_HISTORY: True},
                "polling": {CONF_REFRESH_INTERVAL: "60"},
            },
        )

    assert result["type"] == "create_entry"
    assert result["data"] == {
        CONF_DELIVERED_FILTER_TYPE: "parcels",
        CONF_DELIVERED_FILTER_AMOUNT: 5,
        CONF_INCLUDE_HISTORY: True,
        CONF_REFRESH_INTERVAL: 60,
    }
    # A changed interval only takes effect on reload, so the flow schedules one
    # itself rather than registering an update listener (which is deprecated in
    # combination with reloading).
    schedule_reload.assert_called_once_with(entry.entry_id)


# ---------------------------------------------------------------------------
# options — add parcel
# ---------------------------------------------------------------------------


async def test_options_add_parcel_success(hass):
    entry = _entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_parcel"}
    )
    assert result["step_id"] == "add_parcel"

    with patch(EXCHANGE, new=AsyncMock(return_value=(COOKIE, SECOND_TOKEN))):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], LINK_INPUT
        )

    assert result["type"] == "abort"
    assert result["reason"] == "parcel_added"
    tokens = {p[CONF_PARCEL_TOKEN] for p in entry.data[CONF_PARCELS]}
    assert tokens == {PARCEL_TOKEN, SECOND_TOKEN}


async def test_options_add_parcel_rejects_one_already_tracked(hass):
    """A link that resolves to a parcel this hub already tracks is rejected."""
    entry = _entry([PARCEL_TOKEN])
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_parcel"}
    )
    with patch(EXCHANGE, new=AsyncMock(return_value=(COOKIE, PARCEL_TOKEN))):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], LINK_INPUT
        )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "already_tracked"}
    assert len(entry.data[CONF_PARCELS]) == 1


async def test_options_add_parcel_strips_whitespace_from_the_link(hass):
    """Pasting from an e-mail client often carries leading/trailing whitespace."""
    entry = _entry([])
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_parcel"}
    )
    exchange = AsyncMock(return_value=(COOKIE, PARCEL_TOKEN))
    with patch(EXCHANGE, new=exchange):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_TRACKING_LINK: f"  {TRACKING_LINK}  "}
        )

    assert exchange.call_args[0][1] == TRACKING_LINK
    assert len(entry.data[CONF_PARCELS]) == 1


@pytest.mark.parametrize(
    "error,expected",
    [
        (AmpReAuthError("HTTP 404"), "invalid_link"),
        (AmpReApiError("HTTP 500"), "cannot_connect"),
        (aiohttp.ClientError("boom"), "cannot_connect"),
    ],
)
async def test_options_add_parcel_surfaces_errors(hass, error, expected):
    entry = _entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_parcel"}
    )
    with patch(EXCHANGE, new=AsyncMock(side_effect=error)):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], LINK_INPUT
        )

    assert result["type"] == "form"
    assert result["errors"] == {"base": expected}


# ---------------------------------------------------------------------------
# options — remove parcel
# ---------------------------------------------------------------------------


async def test_options_remove_parcel(hass):
    entry = _entry([PARCEL_TOKEN, SECOND_TOKEN])
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "remove_parcel"}
    )
    assert result["step_id"] == "remove_parcel"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_PARCEL_TOKEN: SECOND_TOKEN}
    )

    assert result["type"] == "abort"
    assert result["reason"] == "parcel_removed"
    tokens = {p[CONF_PARCEL_TOKEN] for p in entry.data[CONF_PARCELS]}
    assert tokens == {PARCEL_TOKEN}
