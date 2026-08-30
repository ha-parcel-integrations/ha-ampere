"""Tests for the Ampère coordinator: fetching and events.

The parcel mapping itself is covered by ``test_parcels.py``. A single
Ampère hub can track several parcels — each its own already-exchanged
:class:`AmpReApiClient` — so the coordinator takes a *list* of clients and
concatenates their results, same as a genuinely multi-parcel account-based
carrier's coordinator would (via ``payloads.py``'s ``barcode`` overrides to
tell the resulting parcels apart).
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ampere.api import AmpReAuthError
from custom_components.ampere.const import (
    CONF_BARCODE,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_PARCEL_TOKEN,
    CONF_PARCELS,
    CONF_REFRESH_INTERVAL,
    DOMAIN,
    HOT_INTERVAL_MINUTES,
    MID_INTERVAL_MINUTES,
    REFRESH_INTERVAL_AUTO,
    STAGGER_MINUTES,
    ParcelStatus,
)
from custom_components.ampere.coordinator import (
    AmpReCoordinator,
    _hottest_tier_minutes,
    _in_quiet_window,
    _next_anchor,
    _next_update_interval,
    _refresh_interval,
    _refresh_setting,
    _stagger_minutes,
)

from .payloads import (
    BARCODE,
    PARCEL_TOKEN,
    delivered_sample,
    out_for_delivery_sample,
    parcel_credential,
    sorted_sample,
)


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"Ampère parcel …{PARCEL_TOKEN[-6:]}",
        data={CONF_PARCELS: [parcel_credential()]},
        # Keep-most-recent-100 so the delivered-retention filter never trims
        # the sample parcels these tests assert on.
        options={
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
        },
    )


def _client(parcels: list[dict] | None = None, *, token: str = PARCEL_TOKEN) -> AsyncMock:
    """A fake AmpReApiClient — a plain list return or a side_effect callable."""
    client = AsyncMock()
    client.parcel_token = token
    if parcels is not None:
        client.async_get_parcels.return_value = parcels
    return client


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------


async def test_update_splits_active_and_delivered(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client(
        [
            out_for_delivery_sample(),
            delivered_sample(barcode="AMPBFSD00999999999"),
        ]
    )
    coordinator = AmpReCoordinator(hass, [client], entry)

    data = await coordinator._async_update_data()

    assert [parcel["barcode"] for parcel in data] == [BARCODE]
    assert len(coordinator.delivered) == 1
    assert coordinator.last_success_time is not None


async def test_update_merges_multiple_clients(hass):
    """A hub with two tracked parcels fetches and merges both sessions."""
    entry = _entry()
    entry.add_to_hass(hass)
    client_a = _client([out_for_delivery_sample()], token="token-a")
    client_b = _client(
        [out_for_delivery_sample(barcode="AMPBFSD00888888888")], token="token-b"
    )
    coordinator = AmpReCoordinator(hass, [client_a, client_b], entry)

    data = await coordinator._async_update_data()

    assert {parcel["barcode"] for parcel in data} == {
        BARCODE,
        "AMPBFSD00888888888",
    }


async def test_update_handles_no_parcels(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = AmpReCoordinator(hass, [], entry)

    assert await coordinator._async_update_data() == []


async def test_expired_session_triggers_reauth(hass):
    """An expired session must start reauth, not retry forever."""
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client(token=PARCEL_TOKEN)
    client.async_get_parcels.side_effect = AmpReAuthError("HTTP 401")
    coordinator = AmpReCoordinator(hass, [client], entry)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()

    assert coordinator.failed_parcel_token == PARCEL_TOKEN


async def test_one_dead_session_does_not_lose_the_others_this_cycle(hass):
    """A hub with two parcels, one session dead — the cycle is discarded
    wholesale (raising ConfigEntryAuthFailed), so the still-good parcel's
    *previous* data stays visible rather than silently vanishing."""
    entry = _entry()
    entry.add_to_hass(hass)
    good = _client([out_for_delivery_sample()], token="token-good")
    bad = _client(token="token-bad")
    bad.async_get_parcels.side_effect = AmpReAuthError("HTTP 401")
    coordinator = AmpReCoordinator(hass, [good, bad], entry)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()

    # Both clients were tried — the good one's data was fetched even though
    # the cycle as a whole was discarded.
    good.async_get_parcels.assert_awaited_once()
    bad.async_get_parcels.assert_awaited_once()
    assert coordinator.failed_parcel_token == "token-bad"


async def test_multiple_simultaneous_failures_target_the_first(hass, caplog):
    entry = _entry()
    entry.add_to_hass(hass)
    first = _client(token="token-1")
    first.async_get_parcels.side_effect = AmpReAuthError("HTTP 401")
    second = _client(token="token-2")
    second.async_get_parcels.side_effect = AmpReAuthError("HTTP 401")
    coordinator = AmpReCoordinator(hass, [first, second], entry)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()

    assert coordinator.failed_parcel_token == "token-1"
    assert "2 tracked parcels" in caplog.text


# ---------------------------------------------------------------------------
# barcode caching — config_flow.py's remove-parcel picker reads this so it
# can label a parcel even with no live coordinator data (e.g. mid
# auth-failure, exactly when a user is most likely to open it)
# ---------------------------------------------------------------------------


async def test_cache_barcodes_persists_onto_entry_data(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client([out_for_delivery_sample()])
    coordinator = AmpReCoordinator(hass, [client], entry)

    await coordinator._async_update_data()

    assert entry.data[CONF_PARCELS][0][CONF_BARCODE] == BARCODE


async def test_cache_barcodes_survives_partial_failure(hass):
    """The good client's barcode is cached even though the cycle as a whole
    raises — a picker opened during exactly this failure still benefits."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Ampère",
        data={
            CONF_PARCELS: [
                parcel_credential(parcel_token="token-good"),
                parcel_credential(parcel_token="token-bad"),
            ]
        },
    )
    entry.add_to_hass(hass)
    good = _client(
        [out_for_delivery_sample(parcel_token="token-good")], token="token-good"
    )
    bad = _client(token="token-bad")
    bad.async_get_parcels.side_effect = AmpReAuthError("HTTP 401")
    coordinator = AmpReCoordinator(hass, [good, bad], entry)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()

    by_token = {p[CONF_PARCEL_TOKEN]: p for p in entry.data[CONF_PARCELS]}
    assert by_token["token-good"][CONF_BARCODE] == BARCODE
    assert CONF_BARCODE not in by_token["token-bad"]


async def test_cache_barcodes_never_caches_the_token_fallback(hass):
    """Only a genuine scraped barcode counts — not normalize_parcel's
    parcel-token fallback for a parcel that scraped nothing."""
    entry = _entry()
    entry.add_to_hass(hass)
    raw = out_for_delivery_sample()
    raw["barcode"] = None
    client = _client([raw])
    coordinator = AmpReCoordinator(hass, [client], entry)

    await coordinator._async_update_data()

    assert CONF_BARCODE not in entry.data[CONF_PARCELS][0]


async def test_cache_barcodes_skips_the_update_when_unchanged(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client([out_for_delivery_sample()])
    coordinator = AmpReCoordinator(hass, [client], entry)
    await coordinator._async_update_data()  # first poll writes the cache

    with patch.object(
        hass.config_entries, "async_update_entry"
    ) as update_entry:
        await coordinator._async_update_data()  # same barcode again

    update_entry.assert_not_called()


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


async def test_first_refresh_fires_nothing(hass):
    """Otherwise every restart floods the user with "registered" events."""
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client([out_for_delivery_sample()])
    coordinator = AmpReCoordinator(hass, [client], entry)

    fired = []
    for suffix in (
        "parcel_registered",
        "parcel_status_changed",
        "parcel_delivered",
        "parcel_delivery_time_changed",
    ):
        hass.bus.async_listen(f"{DOMAIN}_{suffix}", lambda e: fired.append(e))

    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert fired == []


async def test_event_carries_device_id(hass):
    from homeassistant.helpers import device_registry as dr

    entry = _entry()
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
    )
    client = _client()
    coordinator = AmpReCoordinator(hass, [client], entry)

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_status_changed", lambda e: events.append(e)
    )

    client.async_get_parcels.return_value = [sorted_sample()]
    await coordinator._async_update_data()
    client.async_get_parcels.return_value = [out_for_delivery_sample()]
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert events[0].data["device_id"] == device.id


async def test_fires_status_changed_event(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client()
    coordinator = AmpReCoordinator(hass, [client], entry)

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_status_changed", lambda e: events.append(e)
    )

    client.async_get_parcels.return_value = [sorted_sample()]
    await coordinator._async_update_data()  # first refresh: suppressed
    client.async_get_parcels.return_value = [out_for_delivery_sample()]
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["old_status"] == ParcelStatus.IN_TRANSIT
    assert events[0].data["new_status"] == ParcelStatus.OUT_FOR_DELIVERY


async def test_delivery_fires_delivered_event_and_not_status_changed(hass):
    """The hop to delivered fires exactly one, dedicated event."""
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client()
    coordinator = AmpReCoordinator(hass, [client], entry)

    delivered = []
    changed = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", lambda e: delivered.append(e))
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_status_changed", lambda e: changed.append(e)
    )

    client.async_get_parcels.return_value = [out_for_delivery_sample()]
    await coordinator._async_update_data()
    client.async_get_parcels.return_value = [delivered_sample()]
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert changed == []
    assert len(delivered) == 1
    assert delivered[0].data["status"] == ParcelStatus.DELIVERED


async def test_no_events_for_parcel_first_seen_delivered(hass):
    """A parcel already delivered when it first appears fires nothing."""
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client()
    coordinator = AmpReCoordinator(hass, [client], entry)

    fired = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", lambda e: fired.append(e))
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", lambda e: fired.append(e))

    client.async_get_parcels.return_value = [out_for_delivery_sample()]
    await coordinator._async_update_data()  # first refresh seeds the state
    client.async_get_parcels.return_value = [
        out_for_delivery_sample(),
        delivered_sample(barcode="AMPBFSD00999999999"),
    ]
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert fired == []


async def test_fires_registered_event_for_new_parcel(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client()
    coordinator = AmpReCoordinator(hass, [client], entry)

    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", lambda e: events.append(e))

    client.async_get_parcels.return_value = [out_for_delivery_sample()]
    await coordinator._async_update_data()  # first refresh: suppressed
    client.async_get_parcels.return_value = [
        out_for_delivery_sample(),
        out_for_delivery_sample(barcode="AMPBFSD00888888888"),
    ]
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["barcode"] == "AMPBFSD00888888888"


async def test_fires_delivery_time_changed_event(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client()
    coordinator = AmpReCoordinator(hass, [client], entry)

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_delivery_time_changed", lambda e: events.append(e)
    )

    client.async_get_parcels.return_value = [out_for_delivery_sample()]
    await coordinator._async_update_data()  # first refresh: suppressed

    moved = out_for_delivery_sample()
    moved["delivery_window"] = {
        "from": "2026-08-12T19:00:00.000Z",
        "to": "2026-08-12T21:00:00.000Z",
    }
    client.async_get_parcels.return_value = [moved]
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["new_planned_from"] == "2026-08-12T19:00:00.000Z"


async def test_losing_the_eta_is_silent(hass):
    """value -> null just means the carrier lost the window; not worth an alert."""
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client()
    coordinator = AmpReCoordinator(hass, [client], entry)

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_delivery_time_changed", lambda e: events.append(e)
    )

    client.async_get_parcels.return_value = [out_for_delivery_sample()]
    await coordinator._async_update_data()

    dropped = out_for_delivery_sample()
    dropped["delivery_window"] = {}
    client.async_get_parcels.return_value = [dropped]
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert events == []


# ---------------------------------------------------------------------------
# Dynamic polling (Section 2.1, barcode-based) — pure helpers
# ---------------------------------------------------------------------------

UTC = timezone.utc


def _auto_entry_with(parcels: list[dict]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={CONF_PARCELS: parcels},
        options={
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
            CONF_REFRESH_INTERVAL: REFRESH_INTERVAL_AUTO,
        },
    )


def test_refresh_interval_starts_hot_when_auto():
    entry = _auto_entry_with([])
    assert _refresh_interval(entry).total_seconds() == HOT_INTERVAL_MINUTES * 60


def test_refresh_setting_passes_through_auto():
    entry = _auto_entry_with([])
    assert _refresh_setting(entry) == REFRESH_INTERVAL_AUTO


def test_quiet_window_is_midnight_to_six():
    assert _in_quiet_window(datetime(2026, 1, 1, 0, 0, tzinfo=UTC))
    assert _in_quiet_window(datetime(2026, 1, 1, 5, 59, tzinfo=UTC))
    assert not _in_quiet_window(datetime(2026, 1, 1, 6, 0, tzinfo=UTC))
    assert not _in_quiet_window(datetime(2026, 1, 1, 23, 59, tzinfo=UTC))


def test_next_anchor_before_six_is_six_today():
    now = datetime(2026, 1, 1, 2, 30, tzinfo=UTC)
    assert _next_anchor(now) == datetime(2026, 1, 1, 6, 0, tzinfo=UTC)


def test_next_anchor_after_six_is_midnight_tomorrow():
    now = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    assert _next_anchor(now) == datetime(2026, 1, 2, 0, 0, tzinfo=UTC)


def test_stagger_is_stable_and_bounded():
    a = _stagger_minutes("entry-1")
    b = _stagger_minutes("entry-1")
    c = _stagger_minutes("entry-2")
    assert a == b
    assert 0 <= a < STAGGER_MINUTES
    assert 0 <= c < STAGGER_MINUTES


def test_tier_is_none_when_nothing_active():
    assert _hottest_tier_minutes([], datetime(2026, 1, 1, 12, tzinfo=UTC)) is None


def test_tier_is_mid_for_non_hot_statuses():
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    parcels = [
        {"status": "registered", "planned_from": None},
        {"status": "problem", "planned_from": None},
        {"status": "returning", "planned_from": None},
    ]
    assert _hottest_tier_minutes(parcels, now) == MID_INTERVAL_MINUTES


def test_tier_is_hot_when_out_for_delivery_without_planned_from():
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    parcels = [
        {"status": "in_transit", "planned_from": None},
        {"status": "out_for_delivery", "planned_from": None},
    ]
    assert _hottest_tier_minutes(parcels, now) == HOT_INTERVAL_MINUTES


def test_tier_is_hot_within_lookahead_of_planned_from():
    planned = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    now = planned - timedelta(minutes=30)  # inside the 1h lookahead
    parcels = [{"status": "out_for_delivery", "planned_from": planned.isoformat()}]
    assert _hottest_tier_minutes(parcels, now) == HOT_INTERVAL_MINUTES


def test_tier_is_mid_before_lookahead_of_planned_from():
    planned = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    now = planned - timedelta(hours=3)  # well outside the 1h lookahead
    parcels = [{"status": "out_for_delivery", "planned_from": planned.isoformat()}]
    assert _hottest_tier_minutes(parcels, now) == MID_INTERVAL_MINUTES


def test_next_update_interval_is_none_for_none_tier():
    assert (
        _next_update_interval(datetime(2026, 1, 1, 12, tzinfo=UTC), None, "entry-1")
        is None
    )


def test_daytime_candidate_outside_window_is_tier_plus_stagger():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    interval = _next_update_interval(now, MID_INTERVAL_MINUTES, "entry-1")
    stagger = _stagger_minutes("entry-1")
    assert interval == timedelta(minutes=MID_INTERVAL_MINUTES + stagger)


def test_now_inside_quiet_window_jumps_to_next_anchor():
    now = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)  # an anchor poll itself
    interval = _next_update_interval(now, HOT_INTERVAL_MINUTES, "entry-1")
    assert now + interval == datetime(2026, 1, 1, 6, 0, tzinfo=UTC)


def test_candidate_landing_in_quiet_window_clamps_to_the_midnight_anchor():
    now = datetime(2026, 1, 1, 23, 50, tzinfo=UTC)
    interval = _next_update_interval(now, MID_INTERVAL_MINUTES, "entry-1")
    assert now + interval == datetime(2026, 1, 2, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Dynamic polling — wired into _async_update_data
# ---------------------------------------------------------------------------


async def test_auto_mode_stops_entirely_with_nothing_tracked(hass):
    entry = _auto_entry_with([])
    entry.add_to_hass(hass)
    coordinator = AmpReCoordinator(hass, [], entry)

    await coordinator._async_update_data()

    assert coordinator.current_tier_minutes is None
    assert coordinator.update_interval is None


async def test_auto_mode_is_hot_for_an_out_for_delivery_parcel(hass):
    entry = _auto_entry_with([parcel_credential()])
    entry.add_to_hass(hass)
    client = _client([out_for_delivery_sample()])
    coordinator = AmpReCoordinator(hass, [client], entry)

    await coordinator._async_update_data()

    assert coordinator.current_tier_minutes == HOT_INTERVAL_MINUTES
    assert coordinator.update_interval is not None


async def test_auto_mode_is_mid_for_a_non_hot_active_parcel(hass):
    entry = _auto_entry_with([parcel_credential()])
    entry.add_to_hass(hass)
    client = _client([sorted_sample()])
    coordinator = AmpReCoordinator(hass, [client], entry)

    await coordinator._async_update_data()

    assert coordinator.current_tier_minutes == MID_INTERVAL_MINUTES
    assert coordinator.update_interval is not None


async def test_auto_mode_stops_once_every_tracked_parcel_is_delivered(hass):
    entry = _auto_entry_with([parcel_credential()])
    entry.add_to_hass(hass)
    client = _client([delivered_sample()])
    coordinator = AmpReCoordinator(hass, [client], entry)

    await coordinator._async_update_data()

    assert coordinator.current_tier_minutes is None
    assert coordinator.update_interval is None


async def test_fixed_mode_keeps_configured_interval(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client([out_for_delivery_sample()])
    coordinator = AmpReCoordinator(hass, [client], entry)

    await coordinator._async_update_data()

    assert coordinator.current_tier_minutes is None
    assert coordinator.update_interval == timedelta(
        minutes=entry.options.get(CONF_REFRESH_INTERVAL, 30)
    )
