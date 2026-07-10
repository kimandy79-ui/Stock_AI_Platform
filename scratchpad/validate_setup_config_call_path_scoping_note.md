# Scoping note — `validate_setup_config` is never called on any write path

**Status:** scoping only, nothing implemented. Backlog, same tier as the P2.5
dead-key class of finding.
**Date:** 2026-07-10.
**Raised by:** P2.5 orchestration wiring, which added a double-credit rejection to
`validate_setup_config` and then discovered the method has no production callers.

---

## The observation

`ConfigService.validate_setup_config` and `ConfigService.validate_risk_label_config`
have **zero callers outside the test suite**. Full-codebase search across
`app/` and `tools/`:

```
validate_setup_config       -> 0 production callers (5 doc-comment mentions, 20 test calls)
validate_risk_label_config  -> 0 production callers (0 mentions, 5 test calls)
```

A config can therefore become active in prod without its payload ever being
validated. Everything `validate_setup_config` checks — `scoring_weights` summing
to 1.0, the AD-22.23 `rvol_is_hard` pullback rejection, and now the P2.5
double-credit rejection — is enforced **only when a human or a test calls the
validator directly**.

## Why: the authoring surface it would guard does not exist

This is not a forgotten call. The config-authoring API the validator belongs in
front of was never implemented on `ConfigService`. `app/dashboard/action_service.py`
declares a `_ConfigServiceLike` Protocol and codes against it, but three of its
five methods don't exist on the real class:

| Protocol method | On real `ConfigService`? |
|---|---|
| `create_setup_config_version` | **missing** |
| `get_setup_config` | **missing** |
| `list_setup_configs` | **missing** |
| `activate_setup_config` | exists — but with a *different signature* |
| `validate_setup_config` | exists, uncalled |

So `DashboardActionService.clone_setup_config` → `create_setup_config_version`
would raise `AttributeError` against the real `ConfigService`; it passes today only
because tests inject a fake satisfying the Protocol. `action_service.py:130-135`
already documents the sibling drift for `activate_setup_config` ("actually raises
TypeError against the real class today. That's a pre-existing bug, out of scope").

The two paths that *do* write `setup_configs` today:

1. `seed_default_setup_configs` / `seed_preset_setup_configs` — insert
   `default_configs` payloads directly, no validation. Arguably fine: the payloads
   are literals in-repo, and `test_config_service.py` already asserts every seeded
   default and preset passes the validator.
2. `activate_setup_config(config_id, db_role, setup_type)` — validates `db_role`
   and `setup_type` only. It takes a `config_id`, never the payload, so it *cannot*
   validate the JSON without an extra read.

## The question to answer

Is the decoupling deliberate or accidental?

**Case for deliberate.** `validate_setup_config` is documented in-code as a
"creation-time-only check" (`config_service.py:673-676`, added with the AD-22.23
rejection, which explicitly reasoned: "confirmed via full-codebase search that
validate_setup_config has no callers in the seeding or pipeline read paths … so it
cannot retroactively invalidate any currently-active config"). That property is
load-bearing — it's why adding new rejections to the validator is safe and can
never brick an existing prod config on the next run. Wiring it into
`activate_setup_config` would **destroy that property**: every new rule would
retroactively gate re-activation of configs that were legal when authored.

**Case for accidental.** A validator nothing calls is a validator that silently
rots. The P2.5 double-credit rejection is a live example: it protects nothing
today unless someone remembers to call it by hand.

## Options (not chosen — this is scoping)

1. **Document the decoupling and leave it.** Rename to
   `validate_setup_config_payload`, docstring it as an authoring-time helper for
   humans/tests, and note that seeding is covered by
   `test_config_service.py::test_validate_setup_config_every_preset_still_passes`.
   Cheapest; keeps the "new rules can't brick old configs" property.
2. **Call it from `activate_setup_config`.** Requires reading the payload by
   `config_id` first. Gains real enforcement; **loses** the retroactivity-safety
   property above, and would need a grandfathering story for configs already
   active in prod.
3. **Call it from the (missing) creation path.** The principled answer: implement
   `create_setup_config_version` on the real `ConfigService`, validate there, and
   the property is preserved because validation happens at authoring, not
   activation. This is the largest change and overlaps with fixing the
   `_ConfigServiceLike` Protocol drift.

## Recommendation

Option 3 is the right shape, but it is really two tickets: (a) reconcile the
`_ConfigServiceLike` Protocol drift — the dashboard's clone/list/get config
management appears to be non-functional against the real `ConfigService` and this
should be confirmed against a live run before anything else, and (b) implement the
creation path with validation wired in. Option 1 is the correct interim step and
should land regardless, because the current in-code comment already asserts the
"no callers" property as intentional without saying *why* it's desirable.

**Do not do option 2** without a grandfathering plan — it would make the P2.5
double-credit rejection, and the AD-22.23 `rvol_is_hard` rejection, retroactively
block re-activation of any pre-existing config that trips them.

## Related

- `[[p2_5_orchestration_wiring_design_note.md]]` — where this surfaced; the
  scoring-time backstop in `step5_proposal_engine._m14_owns_fundamentals` is
  retained precisely *because* the validator is not a real enforcement point.
- The P2.5 dead-key / dead-config ledger finding — same class: config surface that
  reads as authoritative but is inert.
