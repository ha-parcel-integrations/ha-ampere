"""Ampère guest tracking-session API client.

Auth model: a **one-time link exchange**, not a username/password login.
The user's tracking link from bol.com's shipping e-mail is
followed once — by :func:`async_exchange_tracking_link`, called from the
config flow / options flow's add-parcel step / reauth flow, never from here
— and the resulting ``tnt_sessions`` cookie value plus the resolved
parcel-token are what get replayed on every later poll.
:class:`AmpReApiClient` only ever sees that already-exchanged pair (plus the
mail link itself, kept only to populate the canonical ``url`` field — see
``const.py``'s comment on ``CONF_TRACKING_LINK``); it never re-runs the
exchange itself.

One guest session is still minted for exactly one parcel-token — that part
of the auth model is real and does not change — but Ampère integration
*entries* are not limited to one parcel each: a hub holds a list of these
exchanged sessions (``const.CONF_PARCELS``), one :class:`AmpReApiClient` per
parcel, and the coordinator fetches all of them each poll. ``async_get_parcels``
returning a *list* (of at most one item per client) is exactly what lets the
coordinator treat this the same way it would a genuinely multi-parcel
account API — concatenate every client's list.

Contract the coordinator relies on:

* ``async_get_parcels`` raises :class:`AmpReAuthError` when the stored
  cookie is rejected — either an HTTP 401/403, *or* a `200` that
  server-renders the "open the link again from your e-mail" state, which is
  a semantic auth failure inside an HTTP success, not an outage (see
  :func:`_looks_like_error_state`) — and :class:`AmpReApiError` for every
  other non-success,
* ``aiohttp.ClientError`` propagates untouched — ``DataUpdateCoordinator``
  already wraps it into ``UpdateFailed``.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import aiohttp

from .const import PARCEL_URL, PROGRESS_URL
from .parcels import NEW_ISSUE_URL

_LOGGER = logging.getLogger(__name__)

_PARCEL_TOKEN_RE = re.compile(r"/nl/parcel/([^/?#]+)")

# The error page's own rendered data-test-ids (tracking.md's auth-chain
# section) — NOT its text. An earlier version of this check searched the
# body for the Dutch error copy ("open de link opnieuw", "kunnen deze
# bezorging niet meer laten zien") as a substring, which was a false
# positive on *every* request: that copy is bundled as i18n translation
# data in the page's Next.js payload regardless of which state actually
# renders, confirmed 2026-08-13 by finding it verbatim inside a genuinely
# successful page that also carried a real status-text/barcode-value. The
# error page's actual content markers are reliable in a way its text never
# was — they are absent from a real success page and present only when the
# error branch genuinely rendered.
_ERROR_STATE_TEST_IDS = ("error-title", "error-message")

# Regex-based scrape, not an HTML parser: the confirmed fields are simple
# leaf elements identified by a `data-test-id` attribute (tracking.md's SSR
# page table), and adding a parsing dependency for that is not worth it. This
# is fragile to a frontend redesign by design/necessity — see the shape
# warnings below, which exist precisely to catch that.
def _extract_first(html: str, test_id: str) -> str | None:
    """Return the first element's text content for a given ``data-test-id``."""
    match = re.search(
        rf'data-test-id="{re.escape(test_id)}"[^>]*>\s*([^<]*?)\s*<', html
    )
    if not match:
        return None
    text = match.group(1).strip()
    return text or None


def _extract_all(html: str, test_id: str) -> list[str]:
    """Return every element's text content for a repeated ``data-test-id``.

    Used for ``history-entry-status``, which appears once per history-log
    row, newest first (index 0) per tracking.md.
    """
    return [
        text.strip()
        for text in re.findall(
            rf'data-test-id="{re.escape(test_id)}"[^>]*>\s*([^<]*?)\s*<', html
        )
        if text.strip()
    ]


def _extract_test_ids(html: str) -> set[str]:
    """Every ``data-test-id`` value present.

    Safe to log — attribute names only, never the text they wrap, which
    could carry an address.
    """
    return set(re.findall(r'data-test-id="([^"]+)"', html))


def _looks_like_error_state(html: str) -> bool:
    """Whether a `200` response is actually the "reauth needed" error page.

    Checked against the page's rendered data-test-ids (see
    ``_ERROR_STATE_TEST_IDS`` above for why), not its text.
    """
    return bool(_extract_test_ids(html) & set(_ERROR_STATE_TEST_IDS))


class AmpReApiError(Exception):
    """Raised when an Ampère request fails for a non-auth reason."""

    def __init__(self, detail: str) -> None:
        """Store the detail that triggered the error."""
        super().__init__(f"Ampère request failed: {detail}")
        self.detail = detail


class AmpReAuthError(AmpReApiError):
    """Raised when Ampère rejects the stored session cookie.

    Distinct from :class:`AmpReApiError` on purpose: only this one may
    trigger Home Assistant's reauth flow.
    """


# One-shot shape-logging, pre-1.0 convention (CONVENTIONS.md § Pre-1.0
# releases): reconstructed-from-prose scraping and an endpoint whose full
# shape was never seen both need a WARNING the first time reality diverges
# from what research captured, not a silent guess.
_page_shape_warned = False
_progress_shape_warned = False


def _warn_unrecognised_page_shape(test_ids: set[str]) -> None:
    global _page_shape_warned
    if _page_shape_warned:
        return
    _page_shape_warned = True
    _LOGGER.warning(
        "Ampère's parcel page matched none of the known status/barcode "
        "fields — its markup may have changed since this integration was "
        "written. Help us fix it: open an issue and paste this line: %s\n"
        "  data-test-id values seen: %s",
        NEW_ISSUE_URL,
        sorted(test_ids),
    )


def _warn_unrecognised_progress_shape(keys: set[str]) -> None:
    global _progress_shape_warned
    if _progress_shape_warned:
        return
    _progress_shape_warned = True
    _LOGGER.warning(
        "Ampère's /api/progress returned field(s) beyond 'deliveryWindow' "
        "for the first time — it may now carry live status/position. Help "
        "us map it: open an issue and paste this line: %s\n"
        "  extra keys seen: %s",
        NEW_ISSUE_URL,
        sorted(keys),
    )


async def async_exchange_tracking_link(
    session: aiohttp.ClientSession, tracking_link: str
) -> tuple[str, str]:
    """Follow a bol.com/Ampère tracking e-mail link once.

    Returns ``(cookie_value, parcel_token)`` — the two things worth keeping
    from the whole redirect chain (``link.bol.com/t/...`` -> ``/nl/key/...``
    -> ``/nl/parcel/<parcel-token>``, setting ``tnt_sessions`` along the
    way). Called from the config flow and the reauth flow; a regular poll
    never calls this again — the returned pair is what
    :class:`AmpReApiClient` is constructed with afterwards.

    A 4xx anywhere in the chain means the link itself was rejected (already
    used up, malformed, expired) and raises :class:`AmpReAuthError` so the
    config flow can show "invalid link" rather than "cannot connect".
    """
    try:
        async with session.get(tracking_link, allow_redirects=True) as response:
            status = response.status
            final_url = response.url
            # The exchange is proven by the final URL shape and the cookie
            # jar, not by this page's body — but the body must still be
            # drained so the connection is released back to the pool.
            await response.read()
    except aiohttp.ClientError:
        raise

    if status in (400, 401, 403, 404, 410):
        raise AmpReAuthError(f"HTTP {status} following tracking link")
    if status != 200:
        raise AmpReApiError(f"HTTP {status} following tracking link")

    match = _PARCEL_TOKEN_RE.search(str(final_url))
    if not match:
        raise AmpReApiError(f"unexpected redirect target: {final_url}")
    parcel_token = match.group(1)

    cookies = session.cookie_jar.filter_cookies(final_url)
    morsel = cookies.get("tnt_sessions")
    if morsel is None:
        raise AmpReApiError("tracking link exchange did not set a session cookie")

    return morsel.value, parcel_token


class AmpReApiClient:
    """Client for an already-exchanged Ampère guest tracking session.

    Scoped to exactly one parcel-token, same as the underlying guest session
    itself — a multi-parcel Ampère hub composes several of these (one per
    tracked parcel) rather than this class growing a list internally. See
    the module docstring and ``coordinator.py``.
    """

    def __init__(
        self,
        cookie: str,
        parcel_token: str,
        tracking_link: str,
        session: aiohttp.ClientSession,
    ) -> None:
        """Initialise the client with the exchanged session and a session.

        No dedicated cookie-jar session is needed here (unlike a
        username/password carrier): the cookie is sent explicitly as a
        per-request cookie on every call below, so the HA-managed shared
        session is safe to reuse across multiple Ampère parcels/accounts —
        nothing relies on the session's own jar for auth.

        ``tracking_link`` is the original emailed link, kept only to
        populate the canonical ``url`` field — see ``const.py``'s comment on
        ``CONF_TRACKING_LINK`` for why ``PARCEL_URL`` itself cannot be used
        for that.
        """
        self._cookie = cookie
        self._parcel_token = parcel_token
        self._tracking_link = tracking_link
        self._session = session

    @property
    def parcel_token(self) -> str:
        """The parcel-token this client's session is scoped to.

        Read by the coordinator to identify which parcel a session failure
        belongs to (``coordinator.failed_parcel_token``), since a
        multi-parcel hub's poll can have one client fail while others
        succeed.
        """
        return self._parcel_token

    @property
    def cookie(self) -> str:
        """The current ``tnt_sessions`` cookie value, post-recovery included.

        Read by the coordinator after :meth:`async_reexchange` to persist
        the fresh credential onto the matching ``CONF_PARCELS`` entry.
        """
        return self._cookie

    async def async_reexchange(self) -> str:
        """Silently redo this client's own tracking-link exchange in place.

        Called by the coordinator the moment a session dies, before ever
        raising :class:`AmpReAuthError` up to Home Assistant's reauth flow —
        the tracking link is confirmed safely reusable (module docstring),
        so a dead *session* alone is recoverable without interrupting
        anyone. Mutates this client's cookie and parcel-token in place and
        returns the new parcel-token, so the caller can move the matching
        ``CONF_PARCELS`` entry (keyed by the *old* token) onto it.

        Raises :class:`AmpReAuthError` (or :class:`AmpReApiError` /
        ``aiohttp.ClientError``) when the *link itself*, not just the
        session, is dead — that case has nothing left to retry
        automatically and still needs a human via the normal reauth flow.
        """
        cookie, parcel_token = await async_exchange_tracking_link(
            self._session, self._tracking_link
        )
        self._cookie = cookie
        self._parcel_token = parcel_token
        return parcel_token

    async def async_get_parcels(self) -> list[dict[str, Any]]:
        """Return this session's single tracked parcel as a raw payload dict.

        Always a list of at most one item — see the module/class docstrings.
        """
        html = await self._async_get_parcel_page()
        window = await self._async_get_delivery_window()

        barcode = _extract_first(html, "barcode-value")
        banner_status_text = _extract_first(html, "status-text")
        history_status_texts = _extract_all(html, "history-entry-status")

        if not barcode and not banner_status_text and not history_status_texts:
            _warn_unrecognised_page_shape(_extract_test_ids(html))

        raw: dict[str, Any] = {
            "parcel_token": self._parcel_token,
            "barcode": barcode,
            "company_name": _extract_first(html, "company-name-value"),
            "receiver_name": _extract_first(html, "receiver-name"),
            "banner_status_text": banner_status_text,
            "history_status_texts": history_status_texts,
            # Per-event timestamps, e.g. "Wo 12 aug 21:16" — no year, parsed
            # by parcels.py's _parse_history_timestamp. Confirmed present
            # 2026-08-13 on a delivered parcel; an earlier version of this
            # doc said no timestamp existed on the page at all, which was
            # true only of the "aangemeld" stage this was first captured at.
            "history_times": _extract_all(html, "history-entry-time"),
            "delivery_window": window,
            # The mail link, not PARCEL_URL — see const.py's comment on
            # CONF_TRACKING_LINK. Whether it is safely re-clickable is
            # unconfirmed (ampere.md's Open questions); shipped anyway,
            # best-effort, since a link that might stop working on a second
            # click still beats one that is known not to work at all.
            "url": self._tracking_link,
        }
        return [raw]

    async def _async_get_parcel_page(self) -> str:
        """Fetch the SSR tracking page, raising on any auth failure."""
        url = PARCEL_URL.format(parcel_token=self._parcel_token)
        async with self._session.get(
            url, cookies={"tnt_sessions": self._cookie}
        ) as response:
            if response.status in (401, 403):
                raise AmpReAuthError(f"HTTP {response.status}")
            if response.status != 200:
                raise AmpReApiError(f"HTTP {response.status}")
            html = await response.text()

        if _looks_like_error_state(html):
            # A 200 that server-renders "open the link again from your
            # e-mail" — a semantic auth failure inside an HTTP success, not a
            # bare wall (tracking.md's auth-chain section).
            raise AmpReAuthError("session cookie rejected (in-body error state)")
        return html

    async def _async_get_delivery_window(self) -> dict[str, Any]:
        """Best-effort fetch of /api/progress's ``deliveryWindow``.

        Confirmed (2026-08-12, 4/4 real-parcel checks across the full
        announced -> delivered path) to never carry status, only
        ``deliveryWindow`` — so any failure here must never fail the whole
        poll; the SSR page already has everything the status map needs.
        """
        url = PROGRESS_URL.format(parcel_token=self._parcel_token)
        try:
            async with self._session.get(
                url, cookies={"tnt_sessions": self._cookie}
            ) as response:
                if response.status != 200:
                    return {}
                payload = await response.json(content_type=None)
        except (aiohttp.ClientError, ValueError):
            return {}

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return {}

        extra_keys = set(data) - {"deliveryWindow"}
        if extra_keys:
            _warn_unrecognised_progress_shape(extra_keys)

        window = data.get("deliveryWindow")
        return window if isinstance(window, dict) else {}
