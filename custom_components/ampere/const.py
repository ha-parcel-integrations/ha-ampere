"""Constants for the Ampère parcel tracker integration."""
from enum import StrEnum

from homeassistant.const import Platform

DOMAIN = "ampere"


class ParcelStatus(StrEnum):
    """Carrier-agnostic parcel status.

    **Do not extend or rename these members.** Every integration in the parcel
    suite publishes exactly this vocabulary on the ``status`` field of each
    normalised parcel, so cross-carrier automations and the aggregator can
    target ``status: out_for_delivery`` regardless of carrier. Listed in
    roughly the order a parcel moves through.
    """

    REGISTERED = "registered"               # Sender announced the parcel; not handed over yet
    IN_TRANSIT = "in_transit"               # In the carrier's network
    OUT_FOR_DELIVERY = "out_for_delivery"   # On a delivery vehicle today
    AT_PICKUP_POINT = "at_pickup_point"     # Ready to collect at a pickup location
    DELIVERED = "delivered"                 # Handed over
    RETURNING = "returning"                 # Failed delivery, going back to sender
    PROBLEM = "problem"                     # Carrier reports an exception/issue
    UNKNOWN = "unknown"                     # Raw status we have not mapped yet


PLATFORMS = [Platform.BUTTON, Platform.CALENDAR, Platform.SENSOR]

# Every optional key the parcel contract defines. CAPABILITIES below must be a
# subset of this — it exists so a typo in CAPABILITIES fails a test instead of
# silently dropping a carrier off a table on the docs site.
KNOWN_CAPABILITIES = frozenset(
    {"weight", "dimensions", "delivery_window", "pickup_point", "url", "history"}
)

# Ampère exposes no weight/dimensions and has no pickup-point concept (it is
# a home-delivery-only last-mile brand, no locker/point network observed) —
# see carrier-research/ampere.md. "history" WAS left out on the same reasoning
# ("no per-event timestamp anywhere captured") until 2026-08-13, when a real
# delivered parcel showed `history-entry-time` populated — that reasoning
# held only for the "aangemeld" stage this was first captured at. See
# parcels.py's build_history().
CAPABILITIES = frozenset({"delivery_window", "url", "history"})

# Ampère's guest tracking session, live-confirmed 2026-08-12 (see
# carrier-research/api/ampere/tracking.md, private repo, for the full
# write-up):
#
# * Auth is a one-time link exchange, not a login call — the emailed
#   ``https://link.bol.com/t/<mail-token>?notificationId=...`` link 302s to
#   ``/nl/key/<opaque-key-token>``, which 303s to ``PARCEL_URL`` while
#   setting the ``tnt_sessions`` cookie (httponly, secure, ~60 day Max-Age).
#   The cookie value and the resolved parcel-token are what polling replays.
# * ``PARCEL_URL`` is the fully server-rendered tracking page — the *only*
#   confirmed source of status text (a banner and a history log, worded
#   differently for the same event; see parcels.py's _STATUS_MAP). A `200`
#   with no/rejected cookie still renders — as an in-body Dutch error state,
#   not an HTTP-level wall, see api.py's _looks_like_error_state().
# * ``PROGRESS_URL`` was checked at all four real stages of one parcel
#   followed end to end and never once carried status, only
#   ``deliveryWindow`` — kept only for that ETA window, and its failure must
#   never fail a poll (the SSR page alone is sufficient for status).
BASE_URL = "https://bol.prd.amperebezorgt.nl"
PARCEL_URL = BASE_URL + "/nl/parcel/{parcel_token}"
PROGRESS_URL = BASE_URL + "/api/progress?sid={parcel_token}"

# Persisted per tracked parcel, inside CONF_PARCELS (see below) — cookie and
# parcel-token from the one-time link exchange (config flow / add-parcel /
# reauth), plus the *original* mail link itself.
#
# The mail link is worth keeping this time, unlike an earlier draft of this
# integration: PARCEL_URL requires the ``tnt_sessions`` cookie, which is
# httponly and scoped to this integration's own aiohttp session — never
# present in the user's actual browser. Clicking PARCEL_URL from Home
# Assistant would just show the "open the link again from your e-mail" error
# state (tracking.md's auth-chain section). The mail link is the only thing
# that actually opens the parcel in a real browser (re-running the redirect
# chain and setting a fresh cookie there), so it — not PARCEL_URL — is what
# populates the canonical ``url`` field. See api.py.
#
# Confirmed live 2026-08-15: the mail link is safely reusable — each open
# just re-authenticates rather than erroring — though it mints a fresh,
# unrelated parcel-token for the same physical parcel every time (matching
# barcodes across opens a day apart). config_flow.py's reauth_confirm relies
# on this: it re-exchanges a parcel's own stored link itself instead of
# asking the user to paste one in again.
CONF_COOKIE = "cookie"
CONF_PARCEL_TOKEN = "parcel_token"
CONF_TRACKING_LINK = "tracking_link"

# Cached onto each CONF_PARCELS item once a poll actually returns one, so the
# remove-parcel selector (config_flow.py's _label_for) can show the carrier's
# own tracking code instead of the opaque parcel-token even while the
# coordinator has no live data — e.g. mid auth-failure, which is exactly when
# a user is most likely to be looking at that list. See coordinator.py's
# _cache_barcodes.
CONF_BARCODE = "barcode"

# entry.data's top-level shape: {CONF_PARCELS: [{cookie, parcel_token,
# tracking_link, barcode?}, ...]}. A list, not a scalar pair — Ampère has no
# shared account credential the way a bare postcode or nothing would give
# some other carriers, but it is still one hub that can track *several*
# parcels, each added via its own link exchange (config flow's add-parcel
# step). See CLAUDE.md's Carrier-specific notes for the reauth-targeting
# subtlety this creates. ``barcode`` is absent until the first successful
# poll fills it in.
CONF_PARCELS = "parcels"

# Delivered-parcels retention: keep delivered parcels visible for the last N
# days, or keep only the N most recent — identical across the suite.
CONF_DELIVERED_FILTER_TYPE = "delivered_filter_type"
CONF_DELIVERED_FILTER_AMOUNT = "delivered_filter_amount"
DEFAULT_DELIVERED_FILTER_TYPE = "days"
DEFAULT_DELIVERED_FILTER_AMOUNT = 7

# Refresh interval (minutes) controls how often the coordinator polls the
# carrier. Default 30 min keeps the load on a consumer endpoint gentle; the
# minimum is 15 min for the same reason.
#
# Deliberate divergence from the HA Core rule that polling intervals are not
# user-configurable: that rule targets core integrations, and in a HACS parcel
# tracker a tunable cadence is a wanted feature. Generate with
# ``--interval fixed`` instead when the carrier throttles or soft-bans unusual
# traffic.
CONF_REFRESH_INTERVAL = "refresh_interval"
REFRESH_INTERVAL_AUTO = "auto"
REFRESH_INTERVAL_OPTIONS = (15, 30, 60, 120, 240)
DEFAULT_REFRESH_INTERVAL = 30  # minutes — default for entries that predate "auto"
# New config entries default to "auto" (dynamic-polling rollout, 2026-08-30);
# an existing entry keeps whatever it already has, numeric or "auto".
DEFAULT_NEW_REFRESH_INTERVAL = REFRESH_INTERVAL_AUTO

# Dynamic, status-driven polling — selected via "auto" above. See
# carrier-research/dynamic-polling.md for the full algorithm and reasoning.
#
# Quiet window: no polling between these local hours except the two anchors
# below, for overnight / end-of-day catch-up.
QUIET_WINDOW_START_HOUR = 0
QUIET_WINDOW_END_HOUR = 6

# Cadence while polling is active (minutes). Hot = at least one tracked,
# not-yet-delivered parcel is out_for_delivery within HOT_LOOKAHEAD_HOURS of
# its planned_from (or has no planned_from at all); mid = anything else still
# in flight. This is a barcode-based coordinator (Section 2.1): when every
# tracked parcel is delivered, or nothing is tracked, polling stops entirely
# instead of falling to the mid tier — see coordinator.py's
# ``_hottest_tier_minutes``.
HOT_INTERVAL_MINUTES = 15
MID_INTERVAL_MINUTES = 45
HOT_LOOKAHEAD_HOURS = 1

# Small, stable per-install offset added to every computed interval so
# different installs don't all hit an anchor or tier boundary at the same
# second. Deterministic (hash of the config entry id), not random.
STAGGER_MINUTES = 7

# Per-parcel status history is opt-in and off by default, identical across the
# suite. Keep it off by default: it is a large attribute, and on carriers that
# need a second call per parcel the cost is real.
CONF_INCLUDE_HISTORY = "include_history"
DEFAULT_INCLUDE_HISTORY = False

# Cap each parcel's history to the most recent N events so the attribute stays
# well under HA's ~16 KB state-attribute limit.
HISTORY_MAX_EVENTS = 20
