"""Tests for the Ampère API client."""
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from yarl import URL

from custom_components.ampere.api import (
    AmpReApiClient,
    AmpReApiError,
    AmpReAuthError,
    async_exchange_tracking_link,
)

from .payloads import BARCODE, PARCEL_TOKEN

COOKIE = "%7B%22abc123opaquetoken%22%3A%22keytoken%22%7D"
TRACKING_LINK = "https://link.bol.com/t/mailtoken?notificationId=1234"
PARCEL_URL = f"https://bol.prd.amperebezorgt.nl/nl/parcel/{PARCEL_TOKEN}"


def _page_html(
    *,
    status_text: str | None = "Bezorger is onderweg",
    history: list[str] | None = None,
    history_times: list[str] | None = None,
    barcode: str | None = BARCODE,
    receiver_name: str | None = "Jane Doe",
) -> str:
    history = history if history is not None else ["Bezorger is onderweg"]
    if history_times is None:
        history_times = ["Wo 12 aug 16:36"] * len(history)
    parts = ["<html><body>"]
    if status_text is not None:
        parts.append(f'<div data-test-id="delivery-status-text">'
                      f'<span data-test-id="status-text">{status_text}</span>'
                      f"</div>")
    for i, (text, time_text) in enumerate(zip(history, history_times)):
        parts.append(
            f'<div data-test-id="history-entry-{i}">'
            f'<span data-test-id="history-entry-status">{text}</span>'
            f'<span data-test-id="history-entry-time">{time_text}</span></div>'
        )
    if barcode is not None:
        parts.append(f'<span data-test-id="barcode-value">{barcode}</span>')
    parts.append('<span data-test-id="company-name-value">bol</span>')
    if receiver_name is not None:
        parts.append(f'<span data-test-id="receiver-name">{receiver_name}</span>')
    parts.append("</body></html>")
    return "".join(parts)


def _error_page_html() -> str:
    """A genuine error-state render — its own data-test-ids, not just text.

    api.py's _looks_like_error_state() used to search for the Dutch error
    copy as a plain substring, which was a false positive on every request
    (that copy is bundled i18n data on every page, success included — see
    api.py's _ERROR_STATE_TEST_IDS comment). It now checks for these markers
    instead.
    """
    return (
        "<html><body>"
        '<span data-test-id="expired-key-icon"></span>'
        '<h1 data-test-id="error-title">Open de link opnieuw uit je e-mail</h1>'
        '<p data-test-id="error-message">We kunnen deze bezorging niet meer '
        "laten zien.</p>"
        "</body></html>"
    )


def _response(status: int, *, text: str | None = None, json_body=None) -> MagicMock:
    response = AsyncMock()
    response.status = status
    if text is not None:
        response.text = AsyncMock(return_value=text)
    if json_body is not None or json_body == {}:
        response.json = AsyncMock(return_value=json_body)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _session_returning(*responses: MagicMock) -> MagicMock:
    """A session whose .get() yields each response in turn, cycling the last."""
    session = MagicMock()
    calls = list(responses)

    def _get(*args, **kwargs):
        return calls.pop(0) if len(calls) > 1 else calls[0]

    session.get = MagicMock(side_effect=_get)
    return session


def _client(session: MagicMock, cookie: str = COOKIE) -> AmpReApiClient:
    return AmpReApiClient(cookie, PARCEL_TOKEN, TRACKING_LINK, session)


# ---------------------------------------------------------------------------
# async_exchange_tracking_link
# ---------------------------------------------------------------------------


async def test_exchange_returns_cookie_and_parcel_token():
    session = MagicMock()
    response = AsyncMock()
    response.status = 200
    response.url = URL(PARCEL_URL)
    response.read = AsyncMock(return_value=b"")
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session.get = MagicMock(return_value=ctx)

    morsel = MagicMock()
    morsel.value = "cookie-value"
    session.cookie_jar.filter_cookies = MagicMock(return_value={"tnt_sessions": morsel})

    cookie, parcel_token = await async_exchange_tracking_link(session, TRACKING_LINK)
    assert cookie == "cookie-value"
    assert parcel_token == PARCEL_TOKEN
    assert session.get.call_args[0][0] == TRACKING_LINK
    assert session.get.call_args[1]["allow_redirects"] is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410])
async def test_exchange_raises_auth_error_on_rejected_link(status):
    session = MagicMock()
    response = AsyncMock()
    response.status = status
    response.url = URL(TRACKING_LINK)
    response.read = AsyncMock(return_value=b"")
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session.get = MagicMock(return_value=ctx)

    with pytest.raises(AmpReAuthError):
        await async_exchange_tracking_link(session, TRACKING_LINK)


async def test_exchange_raises_api_error_on_outage():
    session = MagicMock()
    response = AsyncMock()
    response.status = 500
    response.url = URL(TRACKING_LINK)
    response.read = AsyncMock(return_value=b"")
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session.get = MagicMock(return_value=ctx)

    with pytest.raises(AmpReApiError) as err:
        await async_exchange_tracking_link(session, TRACKING_LINK)
    assert not isinstance(err.value, AmpReAuthError)


async def test_exchange_raises_on_unexpected_redirect_target():
    session = MagicMock()
    response = AsyncMock()
    response.status = 200
    response.url = URL("https://bol.prd.amperebezorgt.nl/nl/somewhere-else")
    response.read = AsyncMock(return_value=b"")
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session.get = MagicMock(return_value=ctx)

    with pytest.raises(AmpReApiError):
        await async_exchange_tracking_link(session, TRACKING_LINK)


async def test_exchange_raises_when_no_cookie_was_set():
    session = MagicMock()
    response = AsyncMock()
    response.status = 200
    response.url = URL(PARCEL_URL)
    response.read = AsyncMock(return_value=b"")
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session.get = MagicMock(return_value=ctx)
    session.cookie_jar.filter_cookies = MagicMock(return_value={})

    with pytest.raises(AmpReApiError):
        await async_exchange_tracking_link(session, TRACKING_LINK)


async def test_exchange_propagates_network_error():
    session = MagicMock()
    session.get = MagicMock(side_effect=aiohttp.ClientError("boom"))
    with pytest.raises(aiohttp.ClientError):
        await async_exchange_tracking_link(session, TRACKING_LINK)


# ---------------------------------------------------------------------------
# async_get_parcels — the SSR scrape + best-effort /api/progress
# ---------------------------------------------------------------------------


def test_client_exposes_its_parcel_token():
    """Read by the coordinator to attribute a session failure to one client
    among several, in a multi-parcel hub."""
    client = _client(MagicMock())
    assert client.parcel_token == PARCEL_TOKEN


async def test_get_parcels_scrapes_the_page():
    session = _session_returning(
        _response(200, text=_page_html()),
        _response(200, json_body={"data": {"deliveryWindow": {"from": "a", "to": "b"}}}),
    )
    parcels = await _client(session).async_get_parcels()
    assert len(parcels) == 1
    raw = parcels[0]
    assert raw["barcode"] == BARCODE
    assert raw["banner_status_text"] == "Bezorger is onderweg"
    assert raw["history_status_texts"] == ["Bezorger is onderweg"]
    assert raw["history_times"] == ["Wo 12 aug 16:36"]
    assert raw["company_name"] == "bol"
    assert raw["receiver_name"] == "Jane Doe"
    assert raw["delivery_window"] == {"from": "a", "to": "b"}
    # The original mail link, not the cookie-gated PARCEL_URL.
    assert raw["url"] == TRACKING_LINK
    assert raw["parcel_token"] == PARCEL_TOKEN


async def test_get_parcels_reads_multiple_history_entries_newest_first():
    session = _session_returning(
        _response(
            200,
            text=_page_html(
                history=["Pakket is gesorteerd", "Pakket is aangemeld maar nog niet ontvangen door Ampère"]
            ),
        ),
        _response(200, json_body={"data": {}}),
    )
    raw = (await _client(session).async_get_parcels())[0]
    assert raw["history_status_texts"] == [
        "Pakket is gesorteerd",
        "Pakket is aangemeld maar nog niet ontvangen door Ampère",
    ]


async def test_get_parcels_raises_auth_error_on_401():
    session = _session_returning(_response(401, text=""))
    with pytest.raises(AmpReAuthError):
        await _client(session).async_get_parcels()


async def test_get_parcels_raises_auth_error_on_in_body_error_state():
    """A 200 that server-renders the "open the link again" page is a
    semantic auth failure, not an outage — tracking.md's auth-chain note."""
    session = _session_returning(_response(200, text=_error_page_html()))
    with pytest.raises(AmpReAuthError):
        await _client(session).async_get_parcels()


async def test_get_parcels_does_not_flag_a_real_success_page_as_error():
    """The error copy is bundled i18n data on *every* page, success included
    (confirmed live 2026-08-13) — a real success page must never trip this."""
    session = _session_returning(
        _response(
            200,
            text=_page_html(status_text="Pakket is bezorgd")
            + "<script>window.__i18n={"
            '"sessionMissingTitle":"Open de link opnieuw uit je e-mail",'
            '"sessionMissingMessage":"We kunnen deze bezorging niet meer '
            'laten zien."}</script>',
        ),
        _response(200, json_body={"data": {}}),
    )
    raw = (await _client(session).async_get_parcels())[0]
    assert raw["banner_status_text"] == "Pakket is bezorgd"


async def test_get_parcels_raises_on_outage_status():
    session = _session_returning(_response(503, text=""))
    with pytest.raises(AmpReApiError) as err:
        await _client(session).async_get_parcels()
    assert not isinstance(err.value, AmpReAuthError)


async def test_get_parcels_sends_cookie_header():
    session = _session_returning(
        _response(200, text=_page_html()),
        _response(200, json_body={"data": {}}),
    )
    await _client(session).async_get_parcels()
    first_call_kwargs = session.get.call_args_list[0][1]
    assert first_call_kwargs["cookies"] == {"tnt_sessions": COOKIE}


async def test_get_parcels_warns_once_on_unrecognised_page_shape(caplog):
    session = _session_returning(
        _response(200, text="<html><body>nothing recognisable here</body></html>"),
        _response(200, json_body={"data": {}}),
    )
    raw = (await _client(session).async_get_parcels())[0]
    assert raw["barcode"] is None
    assert raw["banner_status_text"] is None
    assert raw["history_status_texts"] == []
    assert "issues/new" in caplog.text


# ---------------------------------------------------------------------------
# /api/progress — best-effort deliveryWindow, must never fail a poll
# ---------------------------------------------------------------------------


async def test_progress_failure_does_not_fail_the_poll():
    session = _session_returning(
        _response(200, text=_page_html()),
        _response(500, text=""),
    )
    raw = (await _client(session).async_get_parcels())[0]
    assert raw["delivery_window"] == {}


async def test_progress_network_error_does_not_fail_the_poll():
    calls = [
        _response(200, text=_page_html()),
    ]

    def _get(*args, **kwargs):
        if calls:
            return calls.pop(0)
        raise aiohttp.ClientError("boom")

    session = MagicMock()
    session.get = MagicMock(side_effect=_get)
    raw = (await _client(session).async_get_parcels())[0]
    assert raw["delivery_window"] == {}


async def test_progress_unparseable_body_does_not_fail_the_poll():
    page = _response(200, text=_page_html())
    progress = AsyncMock()
    progress.status = 200
    progress.json = AsyncMock(side_effect=ValueError("bad json"))
    progress_ctx = MagicMock()
    progress_ctx.__aenter__ = AsyncMock(return_value=progress)
    progress_ctx.__aexit__ = AsyncMock(return_value=False)

    calls = [page, progress_ctx]
    session = MagicMock()
    session.get = MagicMock(side_effect=lambda *a, **k: calls.pop(0))
    raw = (await _client(session).async_get_parcels())[0]
    assert raw["delivery_window"] == {}


async def test_progress_warns_once_on_unrecognised_extra_keys(caplog):
    session = _session_returning(
        _response(200, text=_page_html()),
        _response(
            200,
            json_body={
                "data": {
                    "deliveryWindow": {"from": "a", "to": "b"},
                    "liveStatus": "onderweg",
                }
            },
        ),
    )
    raw = (await _client(session).async_get_parcels())[0]
    assert raw["delivery_window"] == {"from": "a", "to": "b"}
    assert "issues/new" in caplog.text
    assert "liveStatus" in caplog.text


async def test_progress_non_dict_data_is_ignored():
    session = _session_returning(
        _response(200, text=_page_html()),
        _response(200, json_body={"data": "nope"}),
    )
    raw = (await _client(session).async_get_parcels())[0]
    assert raw["delivery_window"] == {}
