"""Canonical parcel shape, status mapping and list helpers.

Everything in this module is a **pure function** — no I/O, no Home Assistant
objects beyond the config entry's options. That is deliberate: it keeps the
carrier-specific mapping apart from the coordinator (nearly identical
everywhere), and it makes the mapping trivially unit-testable without
spinning up HA.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    HISTORY_MAX_EVENTS,
    ParcelStatus,
)

_AMSTERDAM = ZoneInfo("Europe/Amsterdam")

_LOGGER = logging.getLogger(__name__)

# Where users report a status we do not map yet, or a payload shape research
# never saw. Also reused by api.py's shape-logging warnings.
#
# The ``?template=`` parameter matters: without it the link opens a blank
# form, and the report comes back missing the version and the log line we
# need.
NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-ampere/issues/new"
    "?template=unrecognised_status.yml"
)

# Both status-text vocabularies the SSR page carries (all four stages
# confirmed live end-to-end on one real parcel, 2026-08-12) — the page-top
# banner (`status-text`) and the history-log (`history-entry-status`) are
# worded *differently* for the same event at the "sorted" stage, so both
# strings are mapped rather than picking one vocabulary and hoping the
# other agrees.
#
# normalize_parcel() prefers the newest history-log entry over the banner
# when both are available — the history entries are the more complete
# record — so this map just has to cover whichever one actually shows up.
_STATUS_MAP: dict[str, ParcelStatus] = {
    "Pakket is aangemeld maar nog niet ontvangen door Ampère": ParcelStatus.REGISTERED,
    "Pakket is klaar voor bezorging": ParcelStatus.IN_TRANSIT,
    "Pakket is gesorteerd": ParcelStatus.IN_TRANSIT,
    "Bezorger is onderweg": ParcelStatus.OUT_FOR_DELIVERY,
    "Pakket is bezorgd": ParcelStatus.DELIVERED,
}

# Status strings we have already warned about, so each unmapped one is logged
# only once per HA session instead of on every poll.
_unmapped_statuses_logged: set[str] = set()
_history_mismatch_warned = False

# Dutch 3-letter month abbreviations, as used in history-entry-time's
# "Wo 12 aug 21:16" format — confirmed live 2026-08-13 on a delivered parcel.
# No day-of-week table: the weekday token is ignored, the day/month/time
# triple is enough to build a date and cheaper than validating consistency
# against a name we'd otherwise discard.
_DUTCH_MONTHS = {
    "jan": 1, "feb": 2, "mrt": 3, "apr": 4, "mei": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "okt": 10, "nov": 11, "dec": 12,
}
_HISTORY_TIME_RE = re.compile(
    r"^[A-Za-z]{2}\s+(\d{1,2})\s+([A-Za-z]{3})\s+(\d{1,2}):(\d{2})$"
)


def _warn_unmapped_status(text: str) -> None:
    """Log an unmapped Ampère status text once, with a copy-paste issue link."""
    if text in _unmapped_statuses_logged:
        return
    _unmapped_statuses_logged.add(text)
    _LOGGER.warning(
        "Unrecognised Ampère status — help us map it. Open an issue "
        "and paste this line: %s\n  status=%r → reported as 'unknown'",
        NEW_ISSUE_URL,
        text,
    )


def _warn_history_mismatch(statuses: int, times: int) -> None:
    """One-shot notice that the status/time arrays scraped different lengths.

    Expected to always match — one `history-entry-time` per
    `history-entry-status`, same row. A mismatch means the page shape moved
    under us in a way the regex scrape didn't anticipate; entries beyond the
    shorter list are dropped rather than guessed at.
    """
    global _history_mismatch_warned
    if _history_mismatch_warned:
        return
    _history_mismatch_warned = True
    _LOGGER.warning(
        "Ampère parcel history: scraped %d status entries but %d timestamps "
        "— the page shape may have changed. Help us fix it: open an issue "
        "and paste this line: %s\n  statuses=%d times=%d",
        statuses,
        times,
        NEW_ISSUE_URL,
        statuses,
        times,
    )


def parse_history_timestamp(
    text: str | None, *, now: datetime | None = None
) -> str | None:
    """Parse Ampère's "Wo 12 aug 21:16"-style history timestamp.

    No year on the page, so this assumes the current year and rolls back one
    year if that would place the event more than a day in the future — the
    only ambiguous case is polling in early January about a late-December
    event. Returned as a UTC ISO string; the page's times are Europe/Amsterdam
    wall-clock (NL-only carrier), not UTC, so this localises before
    converting rather than tagging the naive value as UTC directly (which
    would be off by the CET/CEST offset).
    """
    if not text:
        return None
    match = _HISTORY_TIME_RE.match(text.strip())
    if not match:
        return None
    day, month_abbr, hour, minute = match.groups()
    month = _DUTCH_MONTHS.get(month_abbr.lower())
    if month is None:
        return None
    now = now or datetime.now(timezone.utc)
    now_local = now.astimezone(_AMSTERDAM)
    try:
        candidate = datetime(
            now_local.year, month, int(day), int(hour), int(minute),
            tzinfo=_AMSTERDAM,
        )
    except ValueError:
        return None
    if candidate > now_local + timedelta(days=1):
        candidate = candidate.replace(year=now_local.year - 1)
    return candidate.astimezone(timezone.utc).isoformat()


def build_history(raw: dict, *, max_events: int = HISTORY_MAX_EVENTS) -> list[dict]:
    """Build the canonical ``history`` list from Ampère's history-log entries.

    Each entry is ``{timestamp, status, raw_status}`` — identical across all
    suite carriers. ``history_status_texts``/``history_times`` are scraped
    newest-first (index 0 = most recent); the canonical contract sorts
    oldest→newest, so this reverses before capping to ``max_events``. A
    length mismatch between the two arrays (page shape moved) drops the
    excess from the longer one rather than guessing, and warns once.
    """
    statuses = raw.get("history_status_texts") or []
    times = raw.get("history_times") or []
    if len(statuses) != len(times):
        _warn_history_mismatch(len(statuses), len(times))
    entries = [
        {
            "timestamp": parse_history_timestamp(time_text),
            "status": map_parcel_status(status_text),
            "raw_status": status_text,
        }
        for status_text, time_text in zip(statuses, times)
    ]
    return list(reversed(entries))[-max_events:]


def map_parcel_status(text: str | None) -> ParcelStatus:
    """Map an Ampère status text to a canonical :class:`ParcelStatus`.

    ``None`` (nothing scraped at all — see api.py's shape warning for that
    case) reports ``unknown`` silently; unrecognised text warns once.
    """
    if not text:
        return ParcelStatus.UNKNOWN
    mapped = _STATUS_MAP.get(text)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(text)
    return ParcelStatus.UNKNOWN


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure.

    Naive values are treated as UTC so a list always sorts without crashing on
    a mixed set.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def to_iso_timestamp(value: Any) -> str | None:
    """Return an ISO 8601 string for an API timestamp, or ``None``.

    ``/api/progress``'s ``deliveryWindow.from``/``.to`` are ISO strings
    already (the captured example); this just guards against a
    missing/non-string value rather than doing any real conversion.
    """
    if value is None:
        return None
    return str(value)


def _current_status_text(raw: dict) -> str | None:
    """Return the status text normalize_parcel() should map — history over banner.

    The history-log entries are the more complete record and pair with a
    timestamp, so prefer the newest one (index 0 — confirmed "newest entry
    first" ordering) over the page-top banner. Falls back to the banner
    when the history log could not be scraped at all.
    """
    history = raw.get("history_status_texts") or []
    if history:
        return history[0]
    return raw.get("banner_status_text")


def normalize_parcel(raw: dict, *, include_history: bool = False) -> dict:
    """Return a carrier-agnostic parcel dict with the payload under ``raw``.

    Ampère-specific notes:

    * ``status``/``raw_status`` read the history-log over the banner (see
      :func:`_current_status_text`) — the two are worded differently for the
      same event at the "sorted" stage.
    * ``barcode`` falls back to the internal parcel-token when the
      `barcode-value` scrape comes back empty, so a barcode-less parcel
      still gets a stable identifier — the coordinator keys its
      registered/status-changed/delivered event firing on this field, and a
      ``None`` here would silently drop every event for that parcel. Per
      ampere.md's open questions, the barcode itself is display-only — it is
      not confirmed to be a lookup key at all, so nothing here treats it as
      one.
    * ``delivered_at``/``history`` come from ``history-entry-time``, parsed by
      :func:`parse_history_timestamp` — confirmed present 2026-08-13 on a
      delivered parcel; an earlier version of this doc said no per-event
      timestamp existed anywhere, true only of the "aangemeld" stage that was
      first captured at. ``delivered_at`` is the newest history entry's
      timestamp when ``delivered`` (history is newest-first, matching
      :func:`_current_status_text`); ``history`` itself is only built when
      ``include_history`` is set, same opt-in as every other carrier.
    * ``receiver`` reads ``receiver-name`` — an earlier version aliased this
      field to a delivery-address scrape that used a `data-test-id` which
      never existed on the page (`delivery-address-city`), so it was always
      ``None`` regardless of stage.
    * ``weight``/``dimensions``/``pickup_point`` are always ``None`` — not
      part of Ampère's payload (home-delivery-only, no locker network
      observed).
    """
    status_text = _current_status_text(raw)
    status = map_parcel_status(status_text)
    delivered = status is ParcelStatus.DELIVERED

    window = raw.get("delivery_window") or {}
    planned_from = to_iso_timestamp(window.get("from"))
    planned_to = to_iso_timestamp(window.get("to"))

    barcode = raw.get("barcode") or raw.get("parcel_token")

    history_times = raw.get("history_times") or []
    delivered_at = (
        parse_history_timestamp(history_times[0])
        if delivered and history_times
        else None
    )

    return {
        "carrier": "Ampère",
        "barcode": barcode,
        "sender": raw.get("company_name") or "bol.com",
        "receiver": raw.get("receiver_name") or None,
        "status": status,
        "raw_status": status_text,
        "delivered": delivered,
        "delivered_at": delivered_at,
        "planned_from": None if delivered else planned_from,
        "planned_to": None if delivered else planned_to,
        "pickup": False,
        "pickup_point": None,
        "url": raw.get("url"),
        "weight": None,
        "dimensions": None,
        "history": build_history(raw) if include_history else None,
        "raw": raw,
    }


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalised parcels sorted by the ISO timestamp at ``key_field``.

    The suite's sort contract: incoming/outgoing ascending on ``planned_from``,
    delivered descending on ``delivered_at``. Parcels whose value is missing or
    unparseable always sort to the end, regardless of ``descending``.
    """
    with_ts: list[tuple[datetime, dict]] = []
    without_ts: list[dict] = []
    for parcel in parcels:
        parsed = parse_iso(parcel.get(key_field))
        if parsed is None:
            without_ts.append(parcel)
        else:
            with_ts.append((parsed, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [parcel for _, parcel in with_ts] + without_ts


def apply_delivered_filter(parcels: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list per the entry's retention option.

    ``parcels`` must already be sorted newest-first. ``days`` keeps deliveries
    from the last N days (an unparseable ``delivered_at`` is kept rather than
    silently dropped); the ``parcels`` type keeps the N most recent. Parcels
    stay *tracked* either way — this only controls what the delivered sensor
    shows.
    """
    options = entry.options
    filter_type = options.get(
        CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
    )
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(timezone.utc) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
