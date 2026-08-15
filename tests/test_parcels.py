"""Tests for the pure parcel-mapping helpers.

These need no Home Assistant instance — the whole point of keeping
``parcels.py`` free of I/O is that the carrier-specific mapping (the part you
rewrite per carrier) can be tested as plain functions.
"""
from datetime import datetime, timedelta, timezone

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ampere.const import (
    CAPABILITIES,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DOMAIN,
    KNOWN_CAPABILITIES,
    ParcelStatus,
)
from custom_components.ampere.parcels import (
    apply_delivered_filter,
    build_history,
    map_parcel_status,
    normalize_parcel,
    parse_history_timestamp,
    parse_iso,
    sort_parcels_by_ts,
    to_iso_timestamp,
)

from .payloads import (
    BARCODE,
    PARCEL_TOKEN,
    RECEIVER_NAME,
    TRACKING_LINK,
    delivered_sample,
    out_for_delivery_sample,
    registered_sample,
    sorted_sample,
)

# ---------------------------------------------------------------------------
# map_parcel_status — the confirmed 4-stage, 2-vocabulary vocabulary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            "Pakket is aangemeld maar nog niet ontvangen door Ampère",
            ParcelStatus.REGISTERED,
        ),
        # The "sorted" stage's own band-ordering trap: banner and history-log
        # text genuinely differ for the same real-world event, and *both*
        # must land on the same canonical status.
        ("Pakket is klaar voor bezorging", ParcelStatus.IN_TRANSIT),
        ("Pakket is gesorteerd", ParcelStatus.IN_TRANSIT),
        ("Bezorger is onderweg", ParcelStatus.OUT_FOR_DELIVERY),
        ("Pakket is bezorgd", ParcelStatus.DELIVERED),
    ],
)
def test_map_parcel_status_known(text, expected):
    assert map_parcel_status(text) == expected


def test_map_parcel_status_missing_is_unknown():
    assert map_parcel_status(None) == ParcelStatus.UNKNOWN
    assert map_parcel_status("") == ParcelStatus.UNKNOWN


def test_map_parcel_status_unmapped_is_unknown():
    assert map_parcel_status("Pakket is geteleporteerd") == ParcelStatus.UNKNOWN


def test_unmapped_status_warns_only_once(caplog):
    assert map_parcel_status("Pakket is ontvoerd") == ParcelStatus.UNKNOWN
    assert map_parcel_status("Pakket is ontvoerd") == ParcelStatus.UNKNOWN
    assert caplog.text.count("Pakket is ontvoerd") == 1
    assert "issues/new" in caplog.text


# ---------------------------------------------------------------------------
# timestamp helpers
# ---------------------------------------------------------------------------


def test_parse_iso_handles_z_naive_and_garbage():
    assert parse_iso("2026-04-29T13:12:42Z").tzinfo is not None
    # A naive value is assumed UTC so mixed lists still sort.
    assert parse_iso("2026-04-29T13:12:42").tzinfo == timezone.utc
    assert parse_iso("not-a-date") is None
    assert parse_iso(None) is None


def test_to_iso_timestamp_passes_through_strings_and_none():
    assert to_iso_timestamp("2026-08-12T17:40:00.000Z") == "2026-08-12T17:40:00.000Z"
    assert to_iso_timestamp(None) is None


# ---------------------------------------------------------------------------
# parse_history_timestamp — "Wo 12 aug 21:16"-style, no year
# ---------------------------------------------------------------------------


def test_parse_history_timestamp_converts_amsterdam_wall_clock_to_utc():
    """CEST (UTC+2) in August — the page's time is local, not UTC."""
    now = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
    parsed = parse_history_timestamp("Wo 12 aug 21:16", now=now)
    assert parsed == "2026-08-12T19:16:00+00:00"


def test_parse_history_timestamp_rolls_back_a_year_if_implied_future():
    """No year on the page: assume current year, unless that's tomorrow+."""
    now = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)
    parsed = parse_history_timestamp("Ma 28 dec 10:00", now=now)
    assert parsed.startswith("2025-12-28")


def test_parse_history_timestamp_handles_garbage_and_none():
    assert parse_history_timestamp(None) is None
    assert parse_history_timestamp("") is None
    assert parse_history_timestamp("not a timestamp") is None
    assert parse_history_timestamp("Wo 12 xyz 21:16") is None  # unknown month


# ---------------------------------------------------------------------------
# build_history
# ---------------------------------------------------------------------------


def test_build_history_is_oldest_to_newest():
    """The scrape is newest-first; the canonical contract is oldest-first."""
    entries = build_history(delivered_sample())
    assert [e["raw_status"] for e in entries] == [
        "Pakket is aangemeld maar nog niet ontvangen door Ampère",
        "Pakket is gesorteerd",
        "Bezorger is onderweg",
        "Pakket is bezorgd",
    ]
    assert entries[-1]["status"] == ParcelStatus.DELIVERED
    assert all(e["timestamp"] is not None for e in entries)


def test_build_history_warns_once_on_length_mismatch(caplog):
    raw = delivered_sample()
    raw["history_times"] = raw["history_times"][:1]  # now shorter than statuses
    entries = build_history(raw)
    assert len(entries) == 1
    assert "statuses=4 times=1" in caplog.text
    # A second mismatch must not log again.
    caplog.clear()
    build_history(raw)
    assert caplog.text == ""


# ---------------------------------------------------------------------------
# normalize_parcel — the canonical contract
# ---------------------------------------------------------------------------

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "planned_from",
    "planned_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
]


def test_normalize_publishes_exactly_the_canonical_keys():
    """The aggregator and cross-carrier dashboards depend on this key set."""
    assert list(normalize_parcel(delivered_sample())) == CANONICAL_KEYS


def test_capabilities_are_known_values():
    """A typo here would silently misreport this carrier on the docs site."""
    assert CAPABILITIES <= KNOWN_CAPABILITIES


def test_capabilities_match_what_normalize_parcel_actually_returns():
    """Every declared CAPABILITIES entry must come true somewhere in a sample."""
    delivered = normalize_parcel(delivered_sample())
    active = normalize_parcel(out_for_delivery_sample())

    if "weight" in CAPABILITIES:
        assert delivered["weight"] is not None
    if "dimensions" in CAPABILITIES:
        assert delivered["dimensions"] is not None
    if "delivery_window" in CAPABILITIES:
        assert active["planned_from"] is not None or active["planned_to"] is not None
    if "pickup_point" in CAPABILITIES:
        assert delivered["pickup_point"] is not None
    if "url" in CAPABILITIES:
        assert delivered["url"] is not None
    if "history" in CAPABILITIES:
        with_history = normalize_parcel(delivered_sample(), include_history=True)
        assert with_history["history"] is not None


def test_normalize_registered_parcel_uses_history_when_banner_absent():
    """The "announced" stage was only ever captured in the history log."""
    parcel = normalize_parcel(registered_sample())
    assert parcel["status"] == ParcelStatus.REGISTERED
    assert (
        parcel["raw_status"]
        == "Pakket is aangemeld maar nog niet ontvangen door Ampère"
    )
    assert parcel["delivered"] is False


def test_normalize_prefers_history_over_banner_text():
    """tracking.md's decision: history-log wins when the two disagree.

    The "sorted" stage is exactly the band-ordering trap the research
    called out — banner says "klaar voor bezorging", history says
    "gesorteerd" for the very same event, and both must map to
    IN_TRANSIT, but the newest *history* string is what raw_status reports.
    """
    parcel = normalize_parcel(sorted_sample())
    assert parcel["status"] == ParcelStatus.IN_TRANSIT
    assert parcel["raw_status"] == "Pakket is gesorteerd"


def test_normalize_out_for_delivery_parcel_has_window():
    parcel = normalize_parcel(out_for_delivery_sample())
    assert parcel["status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["delivered"] is False
    assert parcel["planned_from"] == "2026-08-12T17:40:00.000Z"
    assert parcel["planned_to"] == "2026-08-12T20:00:00.000Z"


def test_normalize_delivered_parcel():
    parcel = normalize_parcel(delivered_sample())
    assert parcel["carrier"] == "Ampère"
    assert parcel["barcode"] == BARCODE
    assert parcel["sender"] == "bol"
    assert parcel["receiver"] == RECEIVER_NAME
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["raw_status"] == "Pakket is bezorgd"
    assert parcel["delivered"] is True
    # The newest history entry's timestamp — real per-event timestamps,
    # confirmed live 2026-08-13 (see parcels.py's build_history docstring).
    assert parcel["delivered_at"] is not None
    # A delivered parcel drops its ETA — the window is meaningless once it
    # has arrived.
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None
    # The original mail link, not the cookie-gated PARCEL_URL — a browser
    # without the integration's own session cookie can't open the latter.
    assert parcel["url"] == TRACKING_LINK
    assert parcel["weight"] is None
    assert parcel["dimensions"] is None
    assert parcel["pickup"] is False
    assert parcel["pickup_point"] is None
    assert parcel["history"] is None  # opt-in only — not requested here


def test_normalize_history_populates_when_requested():
    """include_history=True now yields real, timestamped entries."""
    parcel = normalize_parcel(delivered_sample(), include_history=True)
    assert parcel["history"] is not None
    assert len(parcel["history"]) == 4
    assert parcel["history"][-1]["status"] == ParcelStatus.DELIVERED
    assert parcel["history"][-1]["timestamp"] == parcel["delivered_at"]


def test_normalize_delivered_at_none_when_no_history_times():
    """A parcel with no scraped history array is still a valid parcel dict."""
    raw = delivered_sample()
    raw["history_times"] = []
    parcel = normalize_parcel(raw)
    assert parcel["delivered_at"] is None


def test_normalize_barcode_falls_back_to_parcel_token():
    """ampere.md's open question: the barcode may not even be a lookup key,
    and is not guaranteed to scrape cleanly — but the coordinator keys its
    event firing on ``barcode``, so it must never come back empty while a
    parcel-token is known."""
    raw = delivered_sample()
    raw["barcode"] = None
    parcel = normalize_parcel(raw)
    assert parcel["barcode"] == PARCEL_TOKEN


def test_normalize_unrecognised_shape_is_unknown():
    """A completely empty raw dict (the scrape found nothing) is still a full,
    valid parcel dict — never a crash."""
    parcel = normalize_parcel({"parcel_token": PARCEL_TOKEN})
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["delivered"] is False
    assert parcel["raw_status"] is None
    assert parcel["barcode"] == PARCEL_TOKEN


def test_normalize_keeps_raw_payload():
    raw = delivered_sample()
    assert normalize_parcel(raw)["raw"] is raw


# ---------------------------------------------------------------------------
# sort_parcels_by_ts
# ---------------------------------------------------------------------------


def test_sort_parcels_ascending_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "planned_from": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "planned_from": None},
        {"barcode": "c", "planned_from": "2026-05-01T10:00:00Z"},
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")]
    assert ordered == ["c", "a", "b"]


def test_sort_parcels_descending_still_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "delivered_at": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "delivered_at": "nonsense"},
        {"barcode": "c", "delivered_at": "2026-05-01T10:00:00Z"},
    ]
    ordered = [
        p["barcode"]
        for p in sort_parcels_by_ts(parcels, "delivered_at", descending=True)
    ]
    assert ordered == ["a", "c", "b"]


# ---------------------------------------------------------------------------
# apply_delivered_filter
# ---------------------------------------------------------------------------


def _entry(filter_type: str, amount: int) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_DELIVERED_FILTER_TYPE: filter_type,
            CONF_DELIVERED_FILTER_AMOUNT: amount,
        },
        unique_id=DOMAIN,
    )


def _delivered_pair() -> list[dict]:
    now = datetime.now(timezone.utc)
    return [
        {"barcode": "RECENT", "delivered_at": (now - timedelta(days=1)).isoformat()},
        {"barcode": "OLD", "delivered_at": (now - timedelta(days=30)).isoformat()},
    ]


def test_delivered_filter_by_days():
    kept = apply_delivered_filter(_delivered_pair(), _entry("days", 7))
    assert [p["barcode"] for p in kept] == ["RECENT"]


def test_delivered_filter_by_count():
    parcels = _delivered_pair()
    assert apply_delivered_filter(parcels, _entry("parcels", 1)) == parcels[:1]


def test_delivered_filter_keeps_unparseable_timestamp():
    """Better to show a parcel with a broken date than to silently drop it —
    this is also Ampère's actual, permanent state, since delivered_at is
    always None (see test_normalize_delivered_parcel)."""
    parcels = [{"barcode": "WEIRD", "delivered_at": "nonsense"}]
    assert apply_delivered_filter(parcels, _entry("days", 7)) == parcels
