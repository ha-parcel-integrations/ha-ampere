# Ampère Parcel Tracker

[![Release](https://img.shields.io/github/v/release/ha-parcel-integrations/ha-ampere.svg)](https://github.com/ha-parcel-integrations/ha-ampere/releases)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> 💬 Questions or feedback? Join the discussion on the [Home Assistant community](https://community.home-assistant.io/t/packages-postnl-dhl-nl-dpd-and-gls-parcel-integration/112433/).

A custom Home Assistant integration that tracks parcels delivered by **Ampère**, bol.com's own last-mile delivery brand in the Netherlands. There is no Ampère account to log into — adding the integration creates a single hub with nothing to track yet; each parcel is then added from that hub's **Configure** menu by pasting its own tracking link from bol.com's shipping-confirmation e-mail, once per parcel.

Part of the [ha-parcel-integrations](https://github.com/ha-parcel-integrations) family: it publishes the same canonical parcel format, statuses and events as the other carrier integrations, so it plugs straight into the [Parcel Aggregator](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) and cross-carrier automations.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Options](#options)
- [Removal](#removal)
- [Sensors](#sensors)
- [Parcel status reference](#parcel-status-reference)
- [Events](#events)
- [Examples](#examples)
- [Debugging](#debugging)
- [Troubleshooting](#troubleshooting)
- [Known limitations](#known-limitations)
- [Related integrations](#related-integrations)
- [Disclaimer](#disclaimer)
- [Contributing](#contributing)
- [License](#license)

## Features

- Follows one Ampère (bol.com) parcel end to end: announced → sorted → out for delivery → delivered
- Canonical status (`registered` / `in_transit` / `out_for_delivery` / `delivered` / …), the carrier's own status text, and the expected delivery window
- Summary sensors and a read-only **Deliveries** calendar entry, for parity with every other carrier in the family
- Events + device triggers for no-code automations (parcel registered, status changed, delivered, delivery time changed)
- Re-authentication support if the tracking session expires — the integration reconnects using the link the parcel was originally added with, nothing needs to be pasted in again
- Manual refresh button and a diagnostic last-update sensor

Track more than one bol.com order from the same hub: use **Track a parcel** in the hub's **Configure** menu for each order's own tracking link. Only one Ampère hub can exist at all — there's no "add the integration again."

## Requirements

- Home Assistant 2024.12 or newer
- The tracking link from a bol.com shipping-confirmation e-mail for a parcel delivered by Ampère (starts with `link.bol.com/t/...`) — no Ampère account, username or password involved

## Installation

### HACS (recommended)

1. In HACS, choose the three-dot menu → **Custom repositories**.
2. Add `https://github.com/ha-parcel-integrations/ha-ampere` as an **Integration**.
3. Install **Ampère** and restart Home Assistant.

### Manual

Copy `custom_components/ampere` into your `config/custom_components/` folder and restart Home Assistant.

## Configuration

1. Go to **Settings → Devices & Services → Add Integration → Ampère**. This creates the Ampère hub — there's nothing to fill in, since there's no account and adding a parcel needs its own step regardless.
2. Open the hub's **Configure** menu and choose **Track a parcel**.
3. Open the shipping-confirmation e-mail bol.com sent for the parcel, and paste its tracking link (the one starting with `link.bol.com/t/...`) into the form. Submit — the integration follows the link once to set up tracking.
4. Repeat step 2 for any other parcel you want to track — they all live under the same hub. Only one Ampère hub can exist at all, so there's no "add the integration again."

### Setup parameters

| Field | Description |
|---|---|
| Tracking link | The full link from bol.com's shipping-confirmation e-mail. Followed once when a parcel is added ("Track a parcel"); the link itself is kept and reused automatically if the session ever needs reconnecting, since it's also the only thing that opens the parcel in a browser (see the parcel's link/attribution). |

## Options

Open **Configure** on the integration entry:

| Section | Option | Default | Description |
|---|---|---|---|
| Delivered parcels | Filter by / amount | last 7 days | How long a delivered parcel stays visible on the delivered sensor. |
| Parcel history | Include status history | off | Adds a `history` attribute per parcel with every status change and when it happened. Off by default, same as every carrier in this family — it's a large attribute. |
| Polling | Refresh every | 30 min | How often Ampère is checked. Slower is gentler on their tracking site. |

## Removal

Standard HA removal applies: **Settings → Devices & Services → Ampère → ⋮ → Delete**. Nothing is stored on bol.com's or Ampère's side by removing the integration.

## Sensors

| Entity | Description |
|---|---|
| `sensor.<device>_incoming_parcels` | `1` while the parcel has not been delivered yet, full details under the `parcels` attribute |
| `sensor.<device>_parcel_<barcode>` | The tracked parcel; state is the canonical status, attributes carry the full normalised parcel |
| `sensor.<device>_next_delivery` | The expected delivery moment, while known |
| `sensor.<device>_delivered_parcels` | The parcel once delivered (see the retention option) |
| `sensor.<device>_last_successful_update` | Diagnostic: when Ampère was last polled successfully |

`<device>` is the single Ampère hub device — every tracked parcel's sensors live on it, not one device per parcel. The parcel moves from its per-parcel sensor to the delivered sensor automatically once delivered.

A **`calendar.<device>_deliveries`** entity shows expected delivery dates for
active parcels — read-only, no extra API calls.

A **`button.<device>_refresh`** entity forces an immediate poll, without waiting
for the next scheduled interval.

## Parcel status reference

The `status` field is the carrier-agnostic enum shared by the whole integration family. Ampère's confirmed vocabulary maps onto four of these:

| Status | Meaning | Ampère's own text (banner / history log) |
|---|---|---|
| `registered` | Announced, not yet received by Ampère | *"Pakket is aangemeld maar nog niet ontvangen door Ampère"* |
| `in_transit` | Sorted, ready for delivery | *"Pakket is klaar voor bezorging"* / *"Pakket is gesorteerd"* |
| `out_for_delivery` | With the courier today | *"Bezorger is onderweg"* |
| `delivered` | Delivered | *"Pakket is bezorgd"* |
| `at_pickup_point` | Not used — Ampère is home delivery only | — |
| `returning` | Not observed yet | — |
| `problem` | Not observed yet | — |
| `unknown` | A status text we have not mapped yet | — |

Ampère's tracking page carries **two** wordings for the same event — a page-top banner and a history-log entry — which do not always agree (e.g. at the "sorted" stage). The integration reads the history log, and the carrier's own text is always available as `raw_status`.

## Events

The integration fires these on the event bus (also available as device triggers on the Ampère device):

| Event | When |
|---|---|
| `ampere_parcel_registered` | The parcel first appears, not yet delivered |
| `ampere_parcel_status_changed` | The canonical status changes (`old_status` / `new_status` in the payload), except the final hop to delivered |
| `ampere_parcel_delivered` | The parcel is delivered |
| `ampere_parcel_delivery_time_changed` | The expected delivery window changes |

Every payload is the full normalised parcel plus the device's `device_id`. Events are suppressed on the first refresh after start-up.

## Examples

Ready-to-paste automations live in [`examples/`](examples/).

### Community Lovelace cards

Third-party cards that work with this integration's sensors:

- [jonisnet/hki-parcels-card](https://github.com/jonisnet/hki-parcels-card)
- [klaptafel/ha-package-tracker-card](https://github.com/klaptafel/ha-package-tracker-card)

## Debugging

```yaml
logger:
  logs:
    custom_components.ampere: debug
```

## Troubleshooting

- **Setup says the tracking link was rejected** — open the original bol.com e-mail and copy the link again; it may have been mistyped.
- **The integration asks to reconnect** — the ~60-day tracking session expired. Confirming the reauthentication step is enough: the integration reuses the link the parcel was originally added with, nothing needs to be pasted in again. If that link has itself stopped working, remove the parcel and add it back with a fresh one from a new e-mail.
- **A status logs "Unrecognised Ampère status"** — please [open an issue](https://github.com/ha-parcel-integrations/ha-ampere/issues/new) with the logged line so the mapping can be extended.

## Known limitations

Ampère exposes no weight, dimensions or pickup-point data — it's a home-delivery-only brand with no locker/point network. There is no delivery address either (only who signed for it, via the **receiver** attribute) — only the recipient's own postcode/city are shown on the tracking page, not usable as canonical `address`-shaped data.

If your Home Assistant log shows a warning asking for a diagnostics report — an unrecognised status, or an unexpected page/history shape — please [open an issue](https://github.com/ha-parcel-integrations/ha-ampere/issues/new) with the logged line.

## Related integrations

This integration is part of [**ha-parcel-integrations**](https://github.com/ha-parcel-integrations) — a family of
parcel-carrier integrations that all publish the same canonical parcel format,
statuses and events.

- [**Parcel Aggregator**](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) rolls every installed carrier
  up into one set of sensors.
- Browse [the organisation](https://github.com/ha-parcel-integrations) for the current list of supported carriers.

## Disclaimer

This integration uses the same server-rendered tracking page a browser sees after opening bol.com's own emailed tracking link. It is not affiliated with, endorsed by, or supported by bol.com or Ampère. Be gentle with the polling interval.

## Contributing

Pull requests and issues are welcome. Please open an issue before
submitting a large change.

## License

[MIT](LICENSE)
