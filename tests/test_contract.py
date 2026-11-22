"""The parcel contract's checks, run over every normalised sample."""

from datetime import datetime

import pytest

from custom_components.ampere.const import ParcelStatus
from custom_components.ampere.parcels import normalize_parcel, parse_iso

from .payloads import (
    delivered_sample,
    out_for_delivery_sample,
    registered_sample,
    sorted_sample,
)

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


def _with_window(start, end) -> dict:
    sample = out_for_delivery_sample()
    sample["delivery_window"] = {"from": start, "to": end}
    return sample


def _delivered_with_window() -> dict:
    sample = delivered_sample()
    sample["delivery_window"] = {
        "from": "2099-04-29T13:00:00Z",
        "to": "2099-04-29T15:00:00Z",
    }
    return sample


SAMPLES = {
    "delivered": delivered_sample,
    "delivered_with_window": _delivered_with_window,
    "registered": registered_sample,
    "sorted": sorted_sample,
    "out_for_delivery": out_for_delivery_sample,
    "from_only": lambda: _with_window("2099-04-29T13:00:00Z", None),
    "to_only": lambda: _with_window(None, "2099-04-29T15:00:00Z"),
}


@pytest.fixture(params=SAMPLES)
def parcel(request) -> dict:
    return normalize_parcel(SAMPLES[request.param]())


def test_keys_and_order(parcel):
    assert list(parcel) == CANONICAL_KEYS


def test_delivered_follows_status(parcel):
    assert parcel["delivered"] is (parcel["status"] is ParcelStatus.DELIVERED)


def test_delivered_at_only_when_delivered(parcel):
    if not parcel["delivered"]:
        assert parcel["delivered_at"] is None


def test_planned_window_is_ordered_and_anchored(parcel):
    if parcel["planned_to"] is not None:
        assert parcel["planned_from"] is not None
        assert parse_iso(parcel["planned_to"]) > parse_iso(parcel["planned_from"])


def test_planned_is_empty_when_the_parcel_is_done(parcel):
    if parcel["status"] is ParcelStatus.DELIVERED:
        assert parcel["planned_from"] is None
        assert parcel["planned_to"] is None


def test_pickup_is_a_bool_and_the_point_needs_it(parcel):
    assert isinstance(parcel["pickup"], bool)
    if not parcel["pickup"]:
        assert parcel["pickup_point"] is None


def test_a_delivered_parcel_drops_a_leftover_window():
    parcel = normalize_parcel(_delivered_with_window())
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None


def test_a_to_only_window_is_anchored_on_that_moment():
    parcel = normalize_parcel(_with_window(None, "2099-04-29T15:00:00Z"))
    assert parcel["planned_from"] == "2099-04-29T15:00:00Z"
    assert parcel["planned_to"] is not None


def test_a_moment_ends_at_the_end_of_that_amsterdam_day():
    parcel = normalize_parcel(_with_window("2099-04-29T13:00:00Z", None))
    end = datetime.fromisoformat(parcel["planned_to"])
    assert (end.hour, end.minute, end.second, end.microsecond) == (23, 59, 59, 0)
    assert end.utcoffset().total_seconds() == 7200
    assert end.date() == datetime(2099, 4, 29).date()


def test_an_unmapped_newest_entry_falls_back_to_history(caplog):
    sample = out_for_delivery_sample()
    sample["history_status_texts"][0] = "Pakket is verdwaald in 2099"
    parcel = normalize_parcel(sample)
    assert parcel["status"] is ParcelStatus.IN_TRANSIT
    assert parcel["raw_status"] == "Pakket is verdwaald in 2099"
    assert caplog.text.count("Pakket is verdwaald in 2099") == 1


def test_an_unmapped_entry_without_history_is_unknown():
    sample = registered_sample()
    sample["history_status_texts"] = ["Pakket is verdwaald in 2100"]
    assert normalize_parcel(sample)["status"] is ParcelStatus.UNKNOWN
