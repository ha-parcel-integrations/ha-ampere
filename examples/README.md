# Examples

Ready-to-paste Home Assistant snippets for the Ampère integration.

| Folder | Contents |
|---|---|
| [`automations/`](automations/) | YAML automations — copy them into your `automations.yaml` or paste them into the Automation editor in **raw editor** mode. |

The parcel comes from the tracking link you set up the integration with, so
there is nothing to register by hand.

All examples assume a single Ampère entry. Adjust entity IDs to match yours;
with more than one bol.com order tracked, every entity ID carries that
entry's own device name.

## Events used in the examples

The coordinator fires these on the HA event bus:

| Event | When | Payload |
|---|---|---|
| `ampere_parcel_registered` | A new parcel appears in the active list | The full normalised parcel dict |
| `ampere_parcel_status_changed` | A parcel's canonical status changes | Same, plus `old_status` / `new_status` |
| `ampere_parcel_delivered` | A parcel reaches the delivered status | Same (fires *instead of* `status_changed` on that final hop) |
| `ampere_parcel_delivery_time_changed` | A parcel's expected delivery time changes | Same, plus `old_planned_from` / `new_planned_from` / `old_planned_to` / `new_planned_to` |

Every payload also carries the account's `device_id`, which is what device
triggers filter on. Events are suppressed on the first refresh after start-up.
