"""Coordinator for the Ampère parcel tracker integration.

Fetching and event firing only — the parcel mapping lives in :mod:`.parcels`,
shared verbatim with the account-less variant.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .api import AmpReApiClient, AmpReAuthError
from .const import (
    CONF_BARCODE,
    CONF_INCLUDE_HISTORY,
    CONF_PARCEL_TOKEN,
    CONF_PARCELS,
    CONF_REFRESH_INTERVAL,
    DEFAULT_INCLUDE_HISTORY,
    DEFAULT_REFRESH_INTERVAL,
    DOMAIN,
    HOT_INTERVAL_MINUTES,
    HOT_LOOKAHEAD_HOURS,
    MID_INTERVAL_MINUTES,
    QUIET_WINDOW_END_HOUR,
    QUIET_WINDOW_START_HOUR,
    REFRESH_INTERVAL_AUTO,
    STAGGER_MINUTES,
    ParcelStatus,
)
from .parcels import apply_delivered_filter, normalize_parcel, sort_parcels_by_ts

_LOGGER = logging.getLogger(__name__)


def _refresh_setting(entry: ConfigEntry) -> str | int:
    """Return the raw configured refresh setting — ``"auto"`` or a minute count."""
    return entry.options.get(CONF_REFRESH_INTERVAL, DEFAULT_REFRESH_INTERVAL)


def _refresh_interval(entry: ConfigEntry) -> timedelta:
    """Return the coordinator's *initial* (or option-update-retuned) interval.

    For a fixed setting this is the final word. For ``"auto"`` it is only a
    starting point — the hot cadence — since ``_async_update_data``
    recomputes it every refresh via ``_next_update_interval``.
    """
    setting = _refresh_setting(entry)
    if setting == REFRESH_INTERVAL_AUTO:
        return timedelta(minutes=HOT_INTERVAL_MINUTES)
    return timedelta(minutes=int(setting))


def _stagger_minutes(entry_id: str) -> int:
    """Deterministic per-install offset, stable across restarts."""
    digest = hashlib.sha256(entry_id.encode()).hexdigest()
    return int(digest, 16) % STAGGER_MINUTES


def _in_quiet_window(moment: datetime) -> bool:
    """Whether ``moment`` (local time) falls in the no-polling window."""
    return QUIET_WINDOW_START_HOUR <= moment.hour < QUIET_WINDOW_END_HOUR


def _next_anchor(now: datetime) -> datetime:
    """Return the next of the two daily anchors (00:00 / 06:00 local)."""
    six_today = now.replace(
        hour=QUIET_WINDOW_END_HOUR, minute=0, second=0, microsecond=0
    )
    if now < six_today:
        return six_today
    midnight_tomorrow = (now + timedelta(days=1)).replace(
        hour=QUIET_WINDOW_START_HOUR, minute=0, second=0, microsecond=0
    )
    return midnight_tomorrow


def _hottest_tier_minutes(active_parcels: list[dict], now: datetime) -> int | None:
    """Tier for the barcode-based model (dynamic-polling.md Section 2.1).

    ``None`` means "stop polling entirely" — nothing is tracked, or every
    tracked parcel is already delivered (already filtered out of
    ``active_parcels`` by the caller).
    """
    if not active_parcels:
        return None

    for parcel in active_parcels:
        if parcel["status"] != ParcelStatus.OUT_FOR_DELIVERY:
            continue
        planned_from = parcel.get("planned_from")
        if not planned_from:
            return HOT_INTERVAL_MINUTES
        planned_dt = dt_util.parse_datetime(planned_from)
        if planned_dt is None:
            return HOT_INTERVAL_MINUTES
        if dt_util.as_utc(now) >= dt_util.as_utc(planned_dt) - timedelta(
            hours=HOT_LOOKAHEAD_HOURS
        ):
            return HOT_INTERVAL_MINUTES

    return MID_INTERVAL_MINUTES


def _next_update_interval(
    now: datetime, tier_minutes: int | None, entry_id: str
) -> timedelta | None:
    """Turn a tier into the coordinator's next ``update_interval``.

    ``None`` fully suspends scheduling (``DataUpdateCoordinator`` honours
    this natively). Otherwise, clamp the naive next-due time forward to the
    next anchor whenever it would land inside the quiet window — including
    when ``now`` itself is already inside it (an anchor poll computing its
    own follow-up).
    """
    if tier_minutes is None:
        return None

    if _in_quiet_window(now):
        return _next_anchor(now) - now

    stagger = timedelta(minutes=_stagger_minutes(entry_id))
    candidate = now + timedelta(minutes=tier_minutes) + stagger
    if _in_quiet_window(candidate):
        return _next_anchor(now) - now
    return candidate - now


class AmpReCoordinator(DataUpdateCoordinator[list[dict]]):
    """Polls every tracked parcel's session and publishes the canonical lists.

    ``coordinator.data`` is the active (not-yet-delivered) parcels,
    ``self.delivered`` the rest. One hub can hold several parcels — each its
    own already-exchanged :class:`AmpReApiClient` (own cookie, own
    parcel-token) — since Ampère has no shared account credential to fetch a
    real inbox with. See ``api.py``'s module docstring.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        clients: list[AmpReApiClient],
        entry: ConfigEntry,
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            # Passing config_entry makes self.config_entry available on the
            # base class, which every helper below relies on.
            config_entry=entry,
            name=DOMAIN,
            update_interval=_refresh_interval(entry),
        )
        self._clients = clients
        self.delivered: list[dict] = []
        # barcode -> last seen ParcelStatus / (planned_from, planned_to).
        # ``None`` on the first refresh so events are suppressed for parcels
        # that already existed when the integration started — otherwise every
        # restart would flood users with "registered" notifications.
        self._known_state: dict[str, ParcelStatus] | None = None
        self._known_delivery_times: (
            dict[str, tuple[str | None, str | None]] | None
        ) = None
        # Cached device id, attached to every fired event so device-trigger
        # automations can filter to this account's device.
        self._cached_device_id: str | None = None
        # Timestamp of the last successful poll (diagnostic sensor).
        self.last_success_time: datetime | None = None
        # Set right before raising ConfigEntryAuthFailed below — the reauth
        # flow reads this (via entry.runtime_data.coordinator) to target the
        # one parcel that actually needs a fresh link, without re-testing
        # every session itself. Best-effort: unset if the very first refresh
        # (before entry.runtime_data exists) is what failed — the reauth
        # flow falls back to asking in that case. See CLAUDE.md.
        self.failed_parcel_token: str | None = None
        # Tier last computed by _hottest_tier_minutes when the refresh
        # setting is "auto" — surfaced in diagnostics. None when polling at a
        # fixed interval instead, or while auto polling is fully suspended.
        self._current_tier_minutes: int | None = None

    @property
    def current_tier_minutes(self) -> int | None:
        """Tier minutes computed on the last "auto" refresh (diagnostics only)."""
        return self._current_tier_minutes

    def _device_id(self) -> str | None:
        """Resolve (and cache) this entry's device id for event payloads."""
        if self._cached_device_id is not None:
            return self._cached_device_id
        registry = dr.async_get(self.hass)
        device = next(
            iter(
                dr.async_entries_for_config_entry(registry, self.config_entry.entry_id)
            ),
            None,
        )
        if device is not None:
            self._cached_device_id = device.id
        return self._cached_device_id

    @property
    def _include_history(self) -> bool:
        """Whether the opt-in per-parcel history option is enabled."""
        return bool(
            self.config_entry.options.get(
                CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY
            )
        )

    def _cache_barcodes(self, raws: list[dict]) -> None:
        """Persist each parcel's real tracking code onto its CONF_PARCELS entry.

        Only a genuine ``raw["barcode"]`` counts — never the parcel-token
        fallback :func:`.parcels.normalize_parcel` substitutes when the
        scrape comes up empty, since caching that would defeat the point:
        this cache exists so config_flow.py's ``_label_for`` can show the
        carrier's own code even with no live coordinator data (e.g. mid
        auth-failure, exactly when a user is most likely to open the
        remove-parcel picker), which the parcel-token fallback already
        covers on its own.
        """
        parcels = [dict(item) for item in self.config_entry.data.get(CONF_PARCELS, [])]
        changed = False
        for raw in raws:
            barcode = raw.get("barcode")
            token = raw.get("parcel_token")
            if not barcode or not token:
                continue
            for item in parcels:
                if (
                    item.get(CONF_PARCEL_TOKEN) == token
                    and item.get(CONF_BARCODE) != barcode
                ):
                    item[CONF_BARCODE] = barcode
                    changed = True
        if changed:
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data={**self.config_entry.data, CONF_PARCELS: parcels},
            )

    async def _async_update_data(self) -> list[dict]:
        """Fetch every tracked parcel's session and split active vs delivered.

        ``aiohttp.ClientError`` and ``AmpReApiError`` are deliberately not
        caught — ``DataUpdateCoordinator`` turns them into ``UpdateFailed``
        with backoff. An expired session needs special handling, because
        retrying it forever would never recover — but one dead session must
        not blank out every other still-working parcel's data: every client
        is tried, and only once all of them have run does a lone auth
        failure turn into ``ConfigEntryAuthFailed`` (which discards this
        cycle's result entirely, per ``DataUpdateCoordinator``'s own
        contract — the *previous* cycle's data for every parcel, including
        the still-good ones, stays visible and un-stale until reauth fixes
        the broken one).
        """
        raws: list[dict] = []
        failing_tokens: list[str] = []
        last_auth_error: AmpReAuthError | None = None
        for client in self._clients:
            try:
                raws.extend(await client.async_get_parcels())
            except AmpReAuthError as err:
                failing_tokens.append(client.parcel_token)
                last_auth_error = err

        # Cache every parcel that *did* answer, even if another one failed —
        # a partial failure must not stall barcode caching for the parcels
        # that are still fine, since the picker (config_flow.py's
        # _label_for) is most likely to be opened during exactly this kind
        # of failure.
        self._cache_barcodes(raws)

        if last_auth_error is not None:
            # Only the first is targeted — HA allows one reauth flow per
            # entry at a time anyway, so a second simultaneous failure just
            # surfaces its own reauth prompt on the very next poll after the
            # first is fixed and the entry reloads.
            self.failed_parcel_token = failing_tokens[0]
            if len(failing_tokens) > 1:
                _LOGGER.warning(
                    "Ampère: %d tracked parcels' sessions expired at once — "
                    "reconnect them one at a time via reauth",
                    len(failing_tokens),
                )
            raise ConfigEntryAuthFailed(
                "Ampère session expired for one or more tracked parcels"
            ) from last_auth_error

        include_history = self._include_history
        normalized = [
            normalize_parcel(raw, include_history=include_history) for raw in raws
        ]
        active = [parcel for parcel in normalized if not parcel["delivered"]]
        delivered = [parcel for parcel in normalized if parcel["delivered"]]

        self.delivered = apply_delivered_filter(
            sort_parcels_by_ts(delivered, "delivered_at", descending=True),
            self.config_entry,
        )
        normalized_active = sort_parcels_by_ts(active, "planned_from")

        # Incoming = active + delivered, combined so the transition to
        # delivered is visible in one set.
        incoming = normalized_active + self.delivered
        self._fire_change_events(incoming)
        self._known_state = {
            parcel["barcode"]: parcel["status"]
            for parcel in incoming
            if parcel.get("barcode")
        }
        self._known_delivery_times = {
            parcel["barcode"]: (parcel.get("planned_from"), parcel.get("planned_to"))
            for parcel in incoming
            if parcel.get("barcode")
        }

        self.last_success_time = datetime.now(timezone.utc)

        setting = _refresh_setting(self.config_entry)
        if setting == REFRESH_INTERVAL_AUTO:
            now = dt_util.now()
            self._current_tier_minutes = _hottest_tier_minutes(normalized_active, now)
            self.update_interval = _next_update_interval(
                now, self._current_tier_minutes, self.config_entry.entry_id
            )
        else:
            self._current_tier_minutes = None
            self.update_interval = timedelta(minutes=int(setting))

        return normalized_active

    def _fire_change_events(self, parcels: list[dict]) -> None:
        """Fire registered / status-changed / delivered / delivery-time events.

        Silent on the very first refresh — we cannot know which parcels are
        genuinely new versus already present before HA started.

        The event contract, identical across the suite:

        * every payload is the full normalised parcel plus ``device_id``;
        * the hop **to** ``delivered`` fires only ``_parcel_delivered``, never
          also ``_parcel_status_changed``;
        * a barcode first seen already-delivered fires nothing;
        * ``registered`` only fires for a new, not-yet-delivered barcode;
        * an ETA going ``value → null`` is intentionally silent — the carrier
          just lost the window, which is not worth waking someone up for.
        """
        if self._known_state is None:
            return

        known_times = self._known_delivery_times or {}
        device_id = self._device_id()

        for parcel in parcels:
            barcode = parcel.get("barcode")
            if not barcode:
                continue
            new_status = parcel["status"]
            if barcode not in self._known_state:
                if new_status != ParcelStatus.DELIVERED:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_registered",
                        {**parcel, "device_id": device_id},
                    )
                continue

            if self._known_state[barcode] != new_status:
                if new_status == ParcelStatus.DELIVERED:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_delivered",
                        {**parcel, "device_id": device_id},
                    )
                else:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_status_changed",
                        {
                            **parcel,
                            "device_id": device_id,
                            "old_status": self._known_state[barcode],
                            "new_status": new_status,
                        },
                    )

            old_from, old_to = known_times.get(barcode, (None, None))
            new_from = parcel.get("planned_from")
            new_to = parcel.get("planned_to")
            from_changed = new_from is not None and new_from != old_from
            to_changed = new_to is not None and new_to != old_to
            if from_changed or to_changed:
                self.hass.bus.async_fire(
                    f"{DOMAIN}_parcel_delivery_time_changed",
                    {
                        **parcel,
                        "device_id": device_id,
                        "old_planned_from": old_from,
                        "new_planned_from": new_from,
                        "old_planned_to": old_to,
                        "new_planned_to": new_to,
                    },
                )
