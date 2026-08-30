"""Config flow for the Ampère parcel tracker integration.

Single hub, several parcels (const.CONF_PARCELS): ``async_step_user`` takes
no input and creates the hub immediately with an empty parcel list
(``single_config_entry`` in the manifest enforces there is only ever one),
and every parcel — including the first — is added afterwards through the
options flow's ``add_parcel`` step. Adding an Ampère parcel is a real async
step (its own form, its own errors: a one-time link exchange, not a
synchronous code check), so it lives on its own menu entry rather than a
plain text field alongside the settings the way GLS's is. There is no reason
for more than one Ampère hub to exist: unlike GLS (scoped to one postcode) or
an account-based carrier (scoped to one login), an Ampère parcel is already
independently credentialed regardless of which hub it is filed under.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    AmpReApiError,
    AmpReAuthError,
    async_exchange_tracking_link,
)
from .const import (
    CONF_BARCODE,
    CONF_COOKIE,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_PARCEL_TOKEN,
    CONF_PARCELS,
    CONF_REFRESH_INTERVAL,
    CONF_TRACKING_LINK,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DEFAULT_INCLUDE_HISTORY,
    DEFAULT_NEW_REFRESH_INTERVAL,
    DEFAULT_REFRESH_INTERVAL,
    DOMAIN,
    REFRESH_INTERVAL_AUTO,
    REFRESH_INTERVAL_OPTIONS,
)

_LOGGER = logging.getLogger(__name__)

# Setup, and adding a parcel later, both ask for the *full* tracking link
# from bol.com's shipping e-mail
# (``https://link.bol.com/t/<mail-token>?notificationId=...``), not a short
# code — see carrier-research/api/ampere/tracking.md's "Consuming this for a
# build" section. The real validation is the live redirect chain the link is
# put through, so the schema only checks it looks like a string at all.
_LINK_SCHEMA = vol.Schema({vol.Required(CONF_TRACKING_LINK): str})


def _current_parcels(entry: ConfigEntry) -> list[dict[str, str]]:
    """Return a mutable copy of this entry's tracked-parcel credential list.

    Lives in ``entry.data`` (not ``entry.options``) — each item carries a
    bearer-equivalent cookie, the same reasoning GLS uses for its DE
    app-instance-id: this is a credential, not a user preference, and
    ``entry.options`` gets rewritten wholesale by the settings step.
    """
    return [dict(item) for item in entry.data.get(CONF_PARCELS, [])]


def _find_entry_tracking_parcel(
    hass: HomeAssistant, parcel_token: str
) -> ConfigEntry | None:
    """Return the Ampère entry (if any) that already tracks ``parcel_token``.

    ``single_config_entry`` means there is only ever one Ampère hub, so this
    always resolves against that one entry in practice — written as a scan
    over every entry of this domain anyway, rather than assuming the single
    entry directly, so it stays correct without edits if that constraint is
    ever relaxed.
    """
    for entry in hass.config_entries.async_entries(DOMAIN):
        if any(
            p.get(CONF_PARCEL_TOKEN) == parcel_token
            for p in entry.data.get(CONF_PARCELS, [])
        ):
            return entry
    return None


def _label_for(parcel: dict[str, str], coordinator: Any | None) -> str:
    """Return a human-friendly label for a tracked parcel in a selector.

    Prefers ``CONF_BARCODE``, the code cached onto ``parcel`` by
    :meth:`AmpReCoordinator._cache_barcodes` the moment it's first known —
    deliberately not dependent on live coordinator data, since this is most
    likely to be shown *during* an auth failure (browsing the remove-parcel
    picker to figure out which parcel needs reconnecting). Falls back to a
    fresh coordinator lookup for a parcel added since the last cache write,
    then to a masked token when nothing has ever resolved this parcel.
    """
    parcel_token = parcel[CONF_PARCEL_TOKEN]
    cached = parcel.get(CONF_BARCODE)
    if cached:
        return f"{cached} (…{parcel_token[-6:]})"
    if coordinator is not None:
        for entry in list(coordinator.data or []) + list(coordinator.delivered or []):
            if (entry.get("raw") or {}).get("parcel_token") == parcel_token:
                barcode = entry.get("barcode")
                if barcode and barcode != parcel_token:
                    return f"{barcode} (…{parcel_token[-6:]})"
    return f"parcel …{parcel_token[-6:]}"


def _parcel_list_value(parcel: dict[str, str]) -> str:
    """Return the editable list value for a stored parcel.

    A carrier barcode is the user-facing identifier. Before the first poll has
    learned it, retain the original tracking link so saving the form cannot
    accidentally remove an otherwise valid parcel.
    """
    return parcel.get(CONF_BARCODE) or parcel[CONF_TRACKING_LINK]


async def _exchange(hass: HomeAssistant, tracking_link: str) -> tuple[str, str]:
    """Follow a tracking link once and return (cookie, parcel_token).

    Uses the HA-managed shared session: this is a one-shot exchange, not an
    ongoing credential — what gets stored afterwards is the returned
    cookie/token pair (plus the link itself), not this session. Shared by
    the config flow, the options flow's add-parcel step and reauth so all
    three behave identically.
    """
    return await async_exchange_tracking_link(
        async_get_clientsession(hass), tracking_link
    )


def _interval_selector() -> selector.SelectSelector:
    """Return the refresh-interval dropdown selector (options translated via strings)."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[REFRESH_INTERVAL_AUTO]
            + [str(minutes) for minutes in REFRESH_INTERVAL_OPTIONS],
            translation_key=CONF_REFRESH_INTERVAL,
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def _parcel_selector(
    parcels: list[dict], coordinator: Any | None
) -> selector.SelectSelector:
    """Return a single-select over ``parcels``, labelled via :func:`_label_for`."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[
                selector.SelectOptionDict(
                    value=p[CONF_PARCEL_TOKEN],
                    label=_label_for(p, coordinator),
                )
                for p in parcels
            ],
            mode=selector.SelectSelectorMode.LIST,
        )
    )


class AmpReConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the UI-driven configuration flow for the Ampère integration."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> AmpReOptionsFlowHandler:
        """Return the options flow handler."""
        return AmpReOptionsFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the Ampère hub — single instance, no input needed.

        With no account and no postcode there is nothing to ask at setup, and
        no per-parcel credential is needed just to create the hub itself. The
        entry is created immediately with an empty :data:`CONF_PARCELS`;
        every parcel — including the first — is added afterwards through the
        options flow's ``add_parcel`` step (see
        :class:`AmpReOptionsFlowHandler`). ``single_config_entry`` in the
        manifest enforces one hub; ``unique_id = DOMAIN`` is belt-and-braces.
        """
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="Ampère",
            data={CONF_PARCELS: []},
            options={
                CONF_DELIVERED_FILTER_TYPE: DEFAULT_DELIVERED_FILTER_TYPE,
                CONF_DELIVERED_FILTER_AMOUNT: DEFAULT_DELIVERED_FILTER_AMOUNT,
                CONF_REFRESH_INTERVAL: DEFAULT_NEW_REFRESH_INTERVAL,
                CONF_INCLUDE_HISTORY: DEFAULT_INCLUDE_HISTORY,
            },
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauth after one of this hub's parcels' sessions died."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-exchange the affected parcel's own stored link — never a new one.

        Every tracked parcel already carries the tracking link it was added
        with (``CONF_TRACKING_LINK``), confirmed live to be safely reusable —
        replaying it re-authenticates rather than erroring. That also means
        asking the user to paste it again cannot work as a *verification*
        step: re-opening a mail link mints a fresh, unrelated parcel-token
        for the same physical parcel every time (confirmed via matching
        barcodes across opens a day apart), so a "does the resulting token
        match what we expected" check could never pass on a legitimate
        reauth — it always would have hit ``wrong_parcel``, forever. Reusing
        the stored link ourselves sidesteps the check entirely: there is no
        ambiguity to verify, because the link used is already tied to the
        one parcel entry it came from.

        Only *which* parcel needs confirming still needs working out, since
        one parcel's session can die while the rest of the hub is fine:

        * a single-parcel entry has no ambiguity — that one parcel is always
          the target;
        * otherwise, ``coordinator.failed_parcel_token`` (set by the poll
          that actually triggered this reauth) names it, when available;
        * if neither applies — most likely the coordinator was never
          attached because the very first refresh after setup is what
          failed — there is no reliable way to know from here which parcel
          is broken without live-testing every stored session, which this
          deliberately does not do (see CLAUDE.md). The form asks the user
          to pick instead of guessing.

        A parcel whose *stored* link has genuinely gone stale (not just
        session expiry) has no recovery path here — remove and re-add it
        with a fresh link instead.
        """
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        parcels = _current_parcels(entry)
        coordinator = getattr(getattr(entry, "runtime_data", None), "coordinator", None)

        target_token: str | None = None
        if len(parcels) == 1:
            target_token = parcels[0][CONF_PARCEL_TOKEN]
        else:
            failed = getattr(coordinator, "failed_parcel_token", None)
            if failed is not None and any(
                p[CONF_PARCEL_TOKEN] == failed for p in parcels
            ):
                target_token = failed

        ambiguous = target_token is None and len(parcels) > 1
        schema_fields: dict[Any, Any] = {}
        if ambiguous:
            schema_fields[vol.Required(CONF_PARCEL_TOKEN)] = _parcel_selector(
                parcels, coordinator
            )

        if user_input is not None:
            chosen_token = user_input.get(CONF_PARCEL_TOKEN, target_token)
            target = next(
                (p for p in parcels if p[CONF_PARCEL_TOKEN] == chosen_token), None
            )
            if target is None:
                return self.async_abort(reason="no_parcels")
            try:
                cookie, parcel_token = await _exchange(
                    self.hass, target[CONF_TRACKING_LINK]
                )
            except AmpReAuthError:
                errors["base"] = "invalid_link"
            except (AmpReApiError, aiohttp.ClientError):
                errors["base"] = "cannot_connect"
            else:
                updated = [
                    {**p, CONF_COOKIE: cookie, CONF_PARCEL_TOKEN: parcel_token}
                    if p[CONF_PARCEL_TOKEN] == chosen_token
                    else p
                    for p in parcels
                ]
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PARCELS: updated}
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(schema_fields),
            errors=errors,
        )


class AmpReOptionsFlowHandler(OptionsFlow):
    """Route parcel management and integration settings through two menus.

    A list item can be a known carrier barcode or an original tracking link.
    New parcels require a link so its cookie/token credential can be created.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show parcel management separately from integration settings."""
        return self.async_show_menu(
            step_id="init", menu_options=["parcels", "settings"]
        )

    async def async_step_parcels(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle the complete code/link list."""
        errors: dict[str, str] = {}
        parcels = _current_parcels(self.config_entry)
        if user_input is not None:
            existing = {_parcel_list_value(parcel): parcel for parcel in parcels}
            updated: list[dict[str, str]] = []
            seen_tokens: set[str] = set()
            for value in user_input.get("tracking_codes", []):
                value = value.strip()
                if not value:
                    continue
                if value in existing:
                    parcel = existing[value]
                elif value.startswith(("https://", "http://")):
                    try:
                        cookie, parcel_token = await _exchange(self.hass, value)
                    except AmpReAuthError:
                        errors["base"] = "invalid_link"
                        break
                    except (AmpReApiError, aiohttp.ClientError):
                        errors["base"] = "cannot_connect"
                        break
                    if _find_entry_tracking_parcel(self.hass, parcel_token) is not None:
                        errors["base"] = "already_tracked"
                        break
                    parcel = {
                        CONF_COOKIE: cookie,
                        CONF_PARCEL_TOKEN: parcel_token,
                        CONF_TRACKING_LINK: value,
                    }
                else:
                    errors["base"] = "tracking_link_required"
                    break
                if parcel[CONF_PARCEL_TOKEN] not in seen_tokens:
                    updated.append(parcel)
                    seen_tokens.add(parcel[CONF_PARCEL_TOKEN])

            if not errors:
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data={**self.config_entry.data, CONF_PARCELS: updated},
                )
                self.hass.config_entries.async_schedule_reload(
                    self.config_entry.entry_id
                )
                return self.async_abort(reason="parcels_updated")

        schema = vol.Schema(
            {
                vol.Optional("tracking_codes"): selector.TextSelector(
                    selector.TextSelectorConfig(multiple=True)
                )
            }
        )
        return self.async_show_form(
            step_id="parcels",
            data_schema=self.add_suggested_values_to_schema(
                schema,
                {"tracking_codes": [_parcel_list_value(parcel) for parcel in parcels]},
            ),
            errors=errors,
        )

    async def async_step_add_parcel(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Exchange a tracking link and append it to this hub's parcels."""
        errors: dict[str, str] = {}

        if user_input is not None:
            link = user_input[CONF_TRACKING_LINK].strip()
            try:
                cookie, parcel_token = await _exchange(self.hass, link)
            except AmpReAuthError:
                errors["base"] = "invalid_link"
            except (AmpReApiError, aiohttp.ClientError):
                errors["base"] = "cannot_connect"
            else:
                if _find_entry_tracking_parcel(self.hass, parcel_token) is not None:
                    errors["base"] = "already_tracked"
                else:
                    parcels = _current_parcels(self.config_entry)
                    parcels.append(
                        {
                            CONF_COOKIE: cookie,
                            CONF_PARCEL_TOKEN: parcel_token,
                            CONF_TRACKING_LINK: link,
                        }
                    )
                    self.hass.config_entries.async_update_entry(
                        self.config_entry,
                        data={**self.config_entry.data, CONF_PARCELS: parcels},
                    )
                    self.hass.config_entries.async_schedule_reload(
                        self.config_entry.entry_id
                    )
                    return self.async_abort(reason="parcel_added")

        return self.async_show_form(
            step_id="add_parcel", data_schema=_LINK_SCHEMA, errors=errors
        )

    async def async_step_remove_parcel(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Stop tracking one of this hub's parcels."""
        parcels = _current_parcels(self.config_entry)
        if not parcels:
            return self.async_abort(reason="no_parcels")

        if user_input is not None:
            remove_token = user_input[CONF_PARCEL_TOKEN]
            remaining = [p for p in parcels if p[CONF_PARCEL_TOKEN] != remove_token]
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data={**self.config_entry.data, CONF_PARCELS: remaining},
            )
            self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
            return self.async_abort(reason="parcel_removed")

        coordinator = getattr(
            getattr(self.config_entry, "runtime_data", None), "coordinator", None
        )
        schema = vol.Schema(
            {vol.Required(CONF_PARCEL_TOKEN): _parcel_selector(parcels, coordinator)}
        )
        return self.async_show_form(step_id="remove_parcel", data_schema=schema)

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle non-parcel integration settings."""
        if user_input is not None:
            self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
            return self.async_create_entry(
                title="",
                data={
                    CONF_DELIVERED_FILTER_TYPE: user_input[CONF_DELIVERED_FILTER_TYPE],
                    CONF_DELIVERED_FILTER_AMOUNT: int(
                        user_input[CONF_DELIVERED_FILTER_AMOUNT]
                    ),
                    CONF_INCLUDE_HISTORY: bool(user_input[CONF_INCLUDE_HISTORY]),
                    CONF_REFRESH_INTERVAL: (
                        REFRESH_INTERVAL_AUTO
                        if user_input[CONF_REFRESH_INTERVAL] == REFRESH_INTERVAL_AUTO
                        else int(user_input[CONF_REFRESH_INTERVAL])
                    ),
                },
            )

        current = self.config_entry.options
        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DELIVERED_FILTER_TYPE,
                        default=current.get(
                            CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
                        ),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=["days", "parcels"],
                            translation_key=CONF_DELIVERED_FILTER_TYPE,
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    ),
                    vol.Required(
                        CONF_DELIVERED_FILTER_AMOUNT,
                        default=current.get(
                            CONF_DELIVERED_FILTER_AMOUNT,
                            DEFAULT_DELIVERED_FILTER_AMOUNT,
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=1, max=365, step=1, mode=selector.NumberSelectorMode.BOX
                        )
                    ),
                    vol.Required(
                        CONF_INCLUDE_HISTORY,
                        default=current.get(
                            CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY
                        ),
                    ): selector.BooleanSelector(),
                    vol.Required(
                        CONF_REFRESH_INTERVAL,
                        default=str(
                            current.get(CONF_REFRESH_INTERVAL, DEFAULT_REFRESH_INTERVAL)
                        ),
                    ): _interval_selector(),
                }
            ),
        )
