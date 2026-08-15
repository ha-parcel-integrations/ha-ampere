"""Sample Ampère raw parcel dicts shared by the test modules.

These mirror the shape api.py's ``AmpReApiClient.async_get_parcels()`` scrapes
off the SSR tracking page — see carrier-research/ampere.md's confirmed
status-vocabulary table (all four stages captured live end-to-end on one
real parcel, 2026-08-12) and api/ampere/tracking.md's SSR field table
(private repo) for the source of every value below.

One Ampère *hub* (config entry) can track several parcels — each its own
guest session from its own tracking-link exchange — so the ``barcode`` /
``parcel_token`` overrides below are used both to simulate that directly and
to exercise the shared coordinator machinery the same way a genuinely
multi-parcel account-based carrier's tests do.
"""
from __future__ import annotations

PARCEL_TOKEN = "abc123opaquetoken"
BARCODE = "AMPBFSD00123456789"
TRACKING_LINK = "https://link.bol.com/t/mailtoken?notificationId=1234"
COOKIE = "cookie-value"
RECEIVER_NAME = "Jane Doe"

# Real captured format (tracking.md, live 2026-08-13): weekday abbreviation,
# day, Dutch 3-letter month, 24h time, no year. Oldest first here so each
# sample's history arrays can just slice from the end backwards, matching
# history_status_texts' own newest-first order below.
_HISTORY_TIMES = [
    "Wo 12 aug 13:00",  # aangemeld
    "Wo 12 aug 15:22",  # gesorteerd
    "Wo 12 aug 16:36",  # onderweg
    "Wo 12 aug 21:16",  # bezorgd
]


def parcel_credential(
    parcel_token: str = PARCEL_TOKEN,
    cookie: str = COOKIE,
    tracking_link: str = TRACKING_LINK,
) -> dict:
    """One item of entry.data's ``CONF_PARCELS`` list."""
    return {
        "cookie": cookie,
        "parcel_token": parcel_token,
        "tracking_link": tracking_link,
    }


def _raw(
    *,
    banner_status_text: str | None,
    history_status_texts: list[str],
    history_times: list[str],
    delivery_window: dict | None = None,
    barcode: str | None = BARCODE,
    parcel_token: str = PARCEL_TOKEN,
    tracking_link: str = TRACKING_LINK,
    receiver_name: str | None = RECEIVER_NAME,
) -> dict:
    return {
        "parcel_token": parcel_token,
        "barcode": barcode,
        "company_name": "bol",
        "receiver_name": receiver_name,
        "banner_status_text": banner_status_text,
        "history_status_texts": history_status_texts,
        "history_times": history_times,
        "delivery_window": delivery_window or {},
        # The original mail link, not the cookie-gated PARCEL_URL — see
        # const.py's comment on CONF_TRACKING_LINK.
        "url": tracking_link,
    }


def registered_sample(
    barcode: str = BARCODE, parcel_token: str = PARCEL_TOKEN
) -> dict:
    """Announced but not yet received by Ampère — history only, no banner yet."""
    return _raw(
        banner_status_text=None,
        history_status_texts=[
            "Pakket is aangemeld maar nog niet ontvangen door Ampère"
        ],
        history_times=[_HISTORY_TIMES[0]],
        barcode=barcode,
        parcel_token=parcel_token,
    )


def sorted_sample(barcode: str = BARCODE, parcel_token: str = PARCEL_TOKEN) -> dict:
    """Sorted — banner and history-log wording genuinely differ here."""
    return _raw(
        banner_status_text="Pakket is klaar voor bezorging",
        history_status_texts=[
            "Pakket is gesorteerd",
            "Pakket is aangemeld maar nog niet ontvangen door Ampère",
        ],
        history_times=[_HISTORY_TIMES[1], _HISTORY_TIMES[0]],
        delivery_window={
            "from": "2026-08-12T15:20:00.000Z",
            "to": "2026-08-12T17:40:00.000Z",
        },
        barcode=barcode,
        parcel_token=parcel_token,
    )


def out_for_delivery_sample(
    barcode: str = BARCODE, parcel_token: str = PARCEL_TOKEN
) -> dict:
    """Onderweg — banner and history-log text match here."""
    return _raw(
        banner_status_text="Bezorger is onderweg",
        history_status_texts=[
            "Bezorger is onderweg",
            "Pakket is gesorteerd",
            "Pakket is aangemeld maar nog niet ontvangen door Ampère",
        ],
        history_times=[_HISTORY_TIMES[2], _HISTORY_TIMES[1], _HISTORY_TIMES[0]],
        delivery_window={
            "from": "2026-08-12T17:40:00.000Z",
            "to": "2026-08-12T20:00:00.000Z",
        },
        barcode=barcode,
        parcel_token=parcel_token,
    )


def delivered_sample(barcode: str = BARCODE, parcel_token: str = PARCEL_TOKEN) -> dict:
    """Bezorgd — the terminal stage."""
    return _raw(
        banner_status_text="Pakket is bezorgd",
        history_status_texts=[
            "Pakket is bezorgd",
            "Bezorger is onderweg",
            "Pakket is gesorteerd",
            "Pakket is aangemeld maar nog niet ontvangen door Ampère",
        ],
        history_times=list(reversed(_HISTORY_TIMES)),
        barcode=barcode,
        parcel_token=parcel_token,
    )
