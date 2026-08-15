"""Ampère parcel tracker custom component for Home Assistant."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AmpReApiClient
from .const import (
    CONF_COOKIE,
    CONF_PARCEL_TOKEN,
    CONF_PARCELS,
    CONF_TRACKING_LINK,
    PLATFORMS,
)
from .coordinator import AmpReCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass
class AmpReData:
    """Runtime data attached to an Ampère config entry."""

    clients: list[AmpReApiClient]
    coordinator: AmpReCoordinator


type AmpReConfigEntry = ConfigEntry[AmpReData]


async def async_setup_entry(hass: HomeAssistant, entry: AmpReConfigEntry) -> bool:
    """Set up Ampère from a config entry.

    Unlike a username/password carrier, there is no login call here: every
    one-time link exchange already happened before now — in the config flow,
    the options flow's add-parcel step, or a reauth flow — so ``entry.data``
    already holds one already-exchanged ``tnt_sessions`` cookie + parcel-token
    + tracking-link per tracked parcel (``CONF_PARCELS``). One
    :class:`AmpReApiClient` is built per entry, each sending its own cookie
    explicitly as a per-request cookie on every call (see api.py), so no
    dedicated per-entry cookie-jar session is needed — the HA-managed shared
    session is safe to reuse across every parcel and every Ampère hub.
    """
    session = async_get_clientsession(hass)
    clients = [
        AmpReApiClient(
            parcel[CONF_COOKIE],
            parcel[CONF_PARCEL_TOKEN],
            parcel[CONF_TRACKING_LINK],
            session,
        )
        for parcel in entry.data.get(CONF_PARCELS, [])
    ]
    coordinator = AmpReCoordinator(hass, clients, entry)

    # Attach runtime_data *before* the first refresh, not after: if that
    # refresh is what raises ConfigEntryAuthFailed (a parcel added earlier
    # whose session already died, surfacing only now on reload — see
    # coordinator.py), the reauth flow reads coordinator.failed_parcel_token
    # via entry.runtime_data to target the one broken parcel. Setting this
    # only after a successful refresh would leave runtime_data unset on
    # exactly the path that needs it, forcing reauth's "ask the user to pick"
    # fallback even when the failing parcel is already known.
    entry.runtime_data = AmpReData(clients=clients, coordinator=coordinator)

    # Fetch initial data here, before forwarding to platforms. Raising
    # ConfigEntryNotReady from a forwarded platform is too late for HA to
    # catch cleanly (it logs a warning and half-sets-up the entry); doing the
    # first refresh here lets a transient failure fail the whole entry so HA
    # retries it with backoff. A rejected/expired cookie instead raises
    # ConfigEntryAuthFailed from inside the coordinator, starting reauth.
    await coordinator.async_config_entry_first_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # No entry.add_update_listener: the options flow calls
    # async_schedule_reload itself. Combining an update listener with a
    # reload-on-update flow is deprecated and becomes an error in HA
    # 2026.12+.
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AmpReConfigEntry) -> bool:
    """Unload an Ampère config entry.

    No session to close: the shared HA-managed session's lifecycle is owned
    by Home Assistant core, not this entry.
    """
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
