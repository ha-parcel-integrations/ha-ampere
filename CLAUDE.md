# Working in this repository

Home Assistant custom integration for **Ampère** parcel tracking.
Distributed via HACS; not part of HA core. One carrier in the
[ha-parcel-integrations](https://github.com/ha-parcel-integrations) suite,
**generated from ha-carrier-template** — everything outside *Carrier-specific
notes* is suite-wide; when in doubt check the template or a sibling repo.
No DTO layer.

## Shared conventions — fetch when relevant

Suite-wide rules live in
[`.github/CONVENTIONS.md`](https://github.com/ha-parcel-integrations/.github/blob/main/CONVENTIONS.md)
and are **not** repeated here. Don't fetch it every session — fetch it **before**
you act in one of these areas:

| Before you … | Fetch `CONVENTIONS.md` § |
|---|---|
| touch entities, sensors, config/options flow, coordinator, diagnostics, translations | *Home Assistant developer docs* (its table points on to the canonical HA page — don't rely on memory) |
| add/rename a parcel field, a `ParcelStatus`, or a bus event; change the sort/first-refresh; touch unmapped-status logging | *Parcel contract* — exact key set, units, sort, events + suppression; `test_parcels.py::test_normalize_publishes_exactly_the_canonical_keys` guards the key set |
| change which optional field this carrier populates vs. always returns `None` | Update `const.py`'s `CAPABILITIES` in the same commit — it feeds the comparison table on the docs site, so a field that starts (or stops) coming back non-null and isn't reflected there is a wrong claim on the website, not just a stale comment |
| ship anything while below 1.0.0 (unconfirmed data) | *Pre-1.0 releases* — one-shot WARNINGs for every guessed shape/code |
| consider "fixing" a lint/pattern the skill flags (poll interval, inline client, sync requests) | *Deliberate skill divergences* — likely intentional, don't re-flag |
| commit, bump, tag, release, or write release notes; add a feature without a test | *Workflow / Commits / Versioning / Testing* |

**Suite-wide tripwires, kept inline on purpose:**
- **First refresh in `__init__.py`, before `async_forward_entry_setups`** — from
  a forwarded platform HA can't catch `ConfigEntryNotReady` and half-sets-up the
  entry. Runtime-only; tests don't catch a regression.
- **Setup stale-entity sweep is scoped to `domain == "sensor"` and skips
  `non_parcel_unique_ids`** — else it deletes the refresh button / the
  summary+diagnostic sensors. Add a new non-parcel sensor's unique_id to the set.
- **Per-parcel sensors are removed by the summary sensor** via
  `entity_registry.async_remove` (self-removal races and leaves ghosts).

## Carrier-specific notes

**No `awaiting_pickup` sensor, deliberately.** Ampère is home-delivery-only
with no locker network, so `pickup`/`pickup_point` stay `False`/`None` in
`parcels.py` by construction. Structural, not a gap — see
`.github/CONVENTIONS.md`'s pickup-point convention.

Ampère is bol.com's own last-mile delivery brand (NL only). Full wire
mechanics live in the private `carrier-research/ampere/api/` — this section
is only the *integration*-side decisions that follow from them.

**Auth model: a one-time link exchange, not a login.** Setup itself asks
nothing (see below) — it's *adding a parcel*, including the first, that asks
for the full tracking link from bol.com's shipping e-mail
(`https://link.bol.com/t/<mail-token>?notificationId=...`), follows it once
via `api.async_exchange_tracking_link()`, and stores the
resulting `tnt_sessions` cookie value, the resolved parcel-token, **and the
original link itself** — one `{cookie, parcel_token, tracking_link}` item per
tracked parcel, see below. The link is kept deliberately, unlike an earlier
draft of this integration: `PARCEL_URL` requires the `tnt_sessions` cookie,
which is httponly and scoped to this integration's own aiohttp session, never
present in the user's browser — clicking `PARCEL_URL` from Home Assistant
would just show the "open the link again from your e-mail" error state. The
mail link is the only thing that actually opens the parcel in a real browser
(re-running the redirect chain and setting a fresh cookie there), so it —
not `PARCEL_URL` — is what populates the canonical `url` field (`api.py`).
The mail link is safely reusable — each open just re-authenticates, though it
mints a fresh, unrelated parcel-token for the same physical parcel every time
(confirmed live 2026-08-15, matching barcodes across opens a day apart) — see
`ampere.md`'s Open questions and the reauth section below, which relies on
this. Every poll sends the stored cookie explicitly as a **per-request**
cookie (`cookies={"tnt_sessions": ...}`), not via the session's own cookie
jar, so there is no dedicated per-entry cookie-jar session: the HA-managed
shared session is reused across parcels safely.

**Single hub, several parcels — `const.CONF_PARCELS`.**
`single_config_entry: true` in the manifest, `async_step_user` takes no input
at all and creates the hub immediately with an empty `CONF_PARCELS` list
(`unique_id = DOMAIN`, belt-and-braces alongside the manifest flag). Ampère
has no shared account credential — each parcel needs its own one-time link
exchange — but that only changes *how* a parcel gets added, not whether the
hub itself needs one to exist. Every parcel, including the first, is added
the same way:
through the options flow's `add_parcel` step (a menu entry, since it's a real
async step with its own errors, not a plain text field like GLS's) — there is
no separate first-parcel code path to keep in sync with it.
`entry.data` is `{CONF_PARCELS: [{cookie, parcel_token, tracking_link}, ...]}`,
a list, not a scalar pair. `remove_parcel` is the symmetric removal step.
Duplicate-checking (`_find_entry_tracking_parcel`) scans every Ampère entry of
this domain rather than assuming the single one directly, so it stays correct
without edits if `single_config_entry` is ever relaxed — but in practice there
is, and should be, only one Ampère hub: unlike GLS (scoped to one postcode) or
an account-based carrier (scoped to one login), an Ampère parcel is already
independently credentialed regardless of which hub it's filed under, so a
second hub would have no purpose a "track another parcel" click doesn't
already serve.

**A dead session is recovered automatically, before the user ever sees a
reauth prompt — added 2026-09-03.** A user reported the reauth flow feeling
"heel irritant" when it recurred often, since every occurrence needed a
manual click even though nothing the user could provide was actually
missing — the stored link is confirmed safely reusable, so there was nothing
to *ask* for. `coordinator._async_update_data` now calls
`AmpReApiClient.async_reexchange()` (api.py) the moment a client raises
`AmpReAuthError`, which re-runs the exchange against that client's own
stored `CONF_TRACKING_LINK` and mutates the client's cookie/parcel-token in
place; on success the coordinator moves the matching `CONF_PARCELS` entry
onto the fresh pair and retries the fetch, all within the same poll — no
`ConfigEntryAuthFailed`, no notification, nothing for the user to do.
`ConfigEntryAuthFailed` (and thus the reauth flow below) now only fires when
that automatic re-exchange itself fails, i.e. the *link*, not just the
session, is genuinely dead. One dead session must still not blank out every
other still-working parcel's data mid-recovery — see the docstring for why
every client is tried before a real failure is raised.

**Reauth targets one parcel, not the whole hub — and reuses its own stored
link, resolved 2026-08-15.** Only one parcel's session may die while the
rest of the hub is still fine, so reauth has to work out *which* tracked
parcel is affected: `coordinator.failed_parcel_token` (set by the poll that
triggered `ConfigEntryAuthFailed`, via `entry.runtime_data.coordinator`)
names it when available; a single-parcel hub has no ambiguity either way. If
neither applies — most likely the very first refresh after setup is what
failed, so no `runtime_data` exists yet — `reauth_confirm`'s form adds a
parcel picker instead of guessing, and does **not** live-test every stored
session to figure it out itself (see `coordinator.py`'s `_async_update_data`
docstring for why: one dead session must not blank out the others' data,
only the eventual `ConfigEntryAuthFailed` after every client has been tried
does).

Once the target parcel is known, `reauth_confirm` re-exchanges *that
parcel's own* `CONF_TRACKING_LINK` itself — it never asks the user to paste
a link. An earlier version did ask, then checked the exchanged link's
*resulting* parcel-token against the target, aborting `wrong_parcel` on a
mismatch. That check could never pass on a legitimate reauth: live testing
confirmed a mail link is safely reusable, but every re-open mints a *fresh,
unrelated* parcel-token for the same physical parcel (matching barcodes
across opens a day apart), so the "did it come back to the parcel we
expected" comparison was comparing two token values that are never equal by
design. Since the flow already holds each parcel's own link, there is
nothing to verify — reusing it can't accidentally rebind a different
parcel, so `wrong_parcel` is gone entirely, replaced by a `no_parcels` abort
for the (only theoretically reachable) case where the target parcel was
removed via `remove_parcel` while the reauth flow was still pending on it.
A parcel whose *stored* link has itself gone stale has no recovery path
here — remove and re-add it with a fresh link instead. **Do not
"simplify" this back to whole-entry reauth** — that would force re-linking
every still-working parcel just because one session expired.

**Status source: the SSR page, never `/api/progress`.** Live-confirmed
2026-08-12 across all four real stages (announced → sorted → out for
delivery → delivered) that `/api/progress` only ever returns
`deliveryWindow`. `parcels.py`'s `_STATUS_MAP` covers *both* the page-top
banner (`status-text`) and the history-log (`history-entry-status`)
wordings, since the two disagree at the "sorted" stage — the newest
history-log entry is what `normalize_parcel` actually reads
(`_current_status_text`), falling back to the banner only if the history
log did not scrape.

**`history-entry-time` carries a real per-event timestamp — resolved
2026-08-13.** An earlier version shipped `delivered_at` and `include_history`
always `None`: nothing on the SSR page appeared to expose when a status
change happened, only the *planned* ETA window from `/api/progress`. That was
true only of the "aangemeld" stage first captured — a live replay of a
delivered parcel showed `history-entry-time` populated (`"Wo 12 aug 21:16"`,
no year, Dutch month abbreviation, Europe/Amsterdam wall-clock).
`parcels.py`'s `parse_history_timestamp` parses it (assumes the current year,
rolls back one on an implied-future date — the only ambiguous case is
January about a December event) and `build_history` turns the paired
`history-entry-status`/`history-entry-time` arrays into the canonical
`{timestamp, status, raw_status}` shape, oldest→newest. `delivered_at` is the
newest entry's timestamp when `delivered`. The delivered-parcels "days"
retention filter now actually expires entries for this carrier, which it
never did while `delivered_at` was always unparseable.

**`receiver` reads `receiver-name`, not a delivery-address scrape.** An
earlier version aliased `receiver` to a `delivery-address-city` scrape that
never matched anything (that `data-test-id` doesn't exist on the page — an
earlier guess never verified against a real capture), so `receiver` was
silently `None` regardless of stage. Fixed to read the real `receiver-name`
field, found in the same replay.

**Barcode is display-only, not a lookup key.** `normalize_parcel` falls
back to the internal `parcel_token` when the `barcode-value` scrape comes
back empty, purely so the coordinator's event-firing (keyed on `barcode`)
never silently drops a parcel that scraped everything else fine.

**Scraping is regex-based, not an HTML parser.** `api.py`'s
`_extract_first`/`_extract_all` match `data-test-id="..."` leaf elements
directly — no new dependency for what are simple label spans on the page.
Fragile to a frontend redesign by necessity; `_looks_like_error_state()`
and the shape-logging warnings (`_warn_unrecognised_page_shape`,
`_warn_unrecognised_progress_shape`) exist precisely to catch that early
via real user reports rather than a silent wrong parse.

**API mechanics go in `carrier-research/ampere/api/`, NOT here and not in a
local `docs/api/`.** See CONVENTIONS.md.

## Options and reloads

The options flow is one sectioned form (`data_entry_flow.section`); changes apply
without a restart. Two models, **do not mix them**:
- **Account-less carriers** (the default) apply changes live: an update listener
  retunes `coordinator.update_interval` and calls `async_request_refresh()`, so
  added/removed parcel sensors appear immediately.
- **Account-based carriers** call `async_schedule_reload` on submit and register
  **no** update listener. Combining a listener with a reload-on-update flow is
  deprecated, an error in HA 2026.12+.

## Dynamic polling

There is no user-facing polling interval — this is a deliberate suite-wide
choice, not a gap. `coordinator.py` recomputes `update_interval` at the end of
every refresh:

- **Quiet window:** no polling 00:00–06:00 local time, except two daily
  anchors (~00:00 and ~06:00) for overnight / end-of-day catch-up.
- **Tiers while polling:** *hot* (15 min) when a tracked, not-yet-delivered
  parcel is `out_for_delivery` within an hour of its `planned_from` (or has no
  `planned_from` at all); *mid* (45 min) for anything else still in flight.
- **Full stop:** `update_interval = None` when nothing is tracked or every
  tracked parcel is delivered. Resumes the moment a parcel is added back, via
  the options-flow update listener above.
- **Stagger:** a small, stable per-install offset (hash of the config entry
  id) is added to every computed interval so installs don't all hit an anchor
  or tier boundary at the same second.

## Module layout

| File | Carrier-specific? |
|---|---|
| `api.py` (HTTP client, error types) | **yes** |
| `const.py` (domain, URLs, `ParcelStatus`, option keys) | partly (URLs) |
| `parcels.py` (status map, `normalize_parcel`, history, sort, filters — pure, no I/O) | partly (`_STATUS_MAP`, `normalize_parcel`) |
| `coordinator.py` (fetch, cache, event firing) | mostly not |
| `config_flow.py` | partly (code validation) |
| `sensor.py` / `button.py` / `calendar.py` / `device_trigger.py` | no |
| `diagnostics.py` | partly (`TO_REDACT`) |
| `services.py` (`track_parcel` / `untrack_parcel`, account-less only) | no |

`parcels.py` is deliberately free of I/O and HA objects so the per-carrier part
stays unit-testable without Home Assistant. Config: `ConfigEntry.runtime_data`
(typed, no `hass.data`), `PARALLEL_UPDATES = 0`, coordinator takes
`config_entry=entry`. `aiohttp.ClientError` is caught **per parcel** in the gather
loop (one bad parcel doesn't fail the poll) but **not** around the whole update
(the coordinator wraps that). Entities: `has_entity_name` + `translation_key`,
`icons.json`, translated units, `_attr_attribution`, `_unrecorded_attributes` on
anything with a parcel list or `raw`. Over-redact diagnostics — they get pasted
into public issues.

## Running tests

```
python -m pytest tests/ --cov=custom_components.ampere
```

Coverage must stay **above 95%** (silver `test-coverage` rule). Run before
committing. A code change updates the README + this file + `docs/` in the same
commit; the API reference lives in the `api/` subfolder of this carrier's
directory under the private `carrier-research/ampere/`, never in this repo.
