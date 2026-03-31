# Feature Flags

Feature flags (also called feature toggles) let you change your application's behavior per user without redeploying. The system is built on the [OpenFeature](https://openfeature.dev/) standard and supports boolean, string, integer, float, and JSON flags, multi-condition targeting rules, user segments, percentage rollouts, and prerequisites.

!!! note "Optional dependency"
    Feature flags require the `flags` extra:
    ```bash
    uv add "waygate[flags]"
    # or: pip install "waygate[flags]"
    ```

---

## Overview

A feature flag has:

- **Variations**: the possible values it can return (`on`/`off`, `"dark"`/`"light"`, `10`/`50`, etc.)
- **Targeting**: rules that decide which variation a specific user receives
- **Fallthrough**: the default variation when no rule matches (a fixed value or a percentage rollout)
- **Kill-switch**: `enabled=False` skips all rules and returns the `off_variation` immediately

Evaluation always follows this order:

```
1. Flag disabled?           → off_variation
2. Prerequisite flags?      → off_variation if any prereq fails
3. Individual targets?      → fixed variation for specific user keys
4. Targeting rules?         → first matching rule wins
5. Fallthrough              → fixed variation or percentage bucket
```

---

## Installation and setup

```bash
uv add "waygate[flags]"
```

Call `engine.use_openfeature()` once before your first evaluation, then access the flag client through `engine.flag_client`:

```python
from waygate import make_engine

engine = make_engine()
engine.use_openfeature()   # activates the feature flag subsystem
```

The flag client is a standard OpenFeature client — any OpenFeature-aware code works with it directly.

---

## Your first flag

```python
from waygate import (
    FeatureFlag, FlagType, FlagVariation, RolloutVariation,
    EvaluationContext,
)

# 1. Define and save the flag
await engine.save_flag(
    FeatureFlag(
        key="new-checkout",
        name="New Checkout Flow",
        type=FlagType.BOOLEAN,
        variations=[
            FlagVariation(name="on",  value=True),
            FlagVariation(name="off", value=False),
        ],
        off_variation="off",
        fallthrough=[                       # 20% of users get "on"
            RolloutVariation(variation="on",  weight=20_000),
            RolloutVariation(variation="off", weight=80_000),
        ],
    )
)

# 2. Evaluate it in a route handler
ctx = EvaluationContext(key=user_id)
enabled = await engine.flag_client.get_boolean_value("new-checkout", False, ctx)
```

Rollout weights are integers out of `100_000`. The above gives exactly 20% to `"on"` and 80% to `"off"`. Bucketing is deterministic: the same `user_id` always lands in the same bucket.

!!! tip "Seeding flags at startup"
    When pre-loading flags in a lifespan or startup function, pass `audit=False` so these programmatic writes do not appear in the audit log:
    ```python
    @asynccontextmanager
    async def lifespan(_):
        await engine.save_flag(FeatureFlag(key="new-checkout", ...), audit=False)
        yield
    ```
    Flags created or updated through the dashboard, REST API, or CLI always audit regardless of this parameter.

---

## Flag types

| Type | Method | Python type |
|---|---|---|
| `FlagType.BOOLEAN` | `get_boolean_value` | `bool` |
| `FlagType.STRING`  | `get_string_value`  | `str` |
| `FlagType.INTEGER` | `get_integer_value` | `int` |
| `FlagType.FLOAT`   | `get_float_value`   | `float` |
| `FlagType.JSON`    | `get_object_value`  | `dict` / `list` |

All evaluation methods share the same signature: `(flag_key, default_value, context)`.

```python
# String flag
theme = await engine.flag_client.get_string_value("ui-theme", "light", ctx)

# Integer flag
page_size = await engine.flag_client.get_integer_value("page-size", 10, ctx)

# Float flag
discount = await engine.flag_client.get_float_value("discount-rate", 0.0, ctx)

# JSON flag — returns a dict
config = await engine.flag_client.get_object_value("feature-config", {}, ctx)
```

---

## Evaluation context

`EvaluationContext` identifies who is making the request. The `key` field is required; use a stable user or session identifier. Everything else is optional:

```python
ctx = EvaluationContext(
    key=user.id,           # required — used for individual targeting + rollout bucketing
    kind="user",           # optional — defaults to "user"
    email=user.email,      # accessible in rules as the "email" attribute
    ip=request.client.host,
    country=user.country,
    app_version="2.3.1",
    attributes={           # any extra attributes your rules need
        "plan": user.plan,
        "role": user.role,
    },
)
```

Named fields (`email`, `ip`, `country`, `app_version`) are accessible in targeting rules by the same names. Custom attributes go in `attributes`.

---

## Targeting rules

Targeting rules serve a specific variation to users who match certain conditions.

### Attribute-based rule

```python
from waygate import TargetingRule, RuleClause, Operator

FeatureFlag(
    key="ui-theme",
    ...
    rules=[
        TargetingRule(
            description="Corporate users → dark theme",
            clauses=[
                RuleClause(
                    attribute="email",
                    operator=Operator.ENDS_WITH,
                    values=["@acme.com"],
                )
            ],
            variation="dark",
        )
    ],
)
```

### Multiple clauses (AND logic)

All clauses within a rule must match (AND). Multiple values within one clause are OR-ed.

```python
TargetingRule(
    description="GB Pro users → full discount",
    clauses=[
        RuleClause(attribute="country", operator=Operator.IS,  values=["GB"]),
        RuleClause(attribute="plan",    operator=Operator.IN,  values=["pro", "enterprise"]),
    ],
    variation="full",
)
```

### Negation

Flip the result of any clause with `negate=True`:

```python
RuleClause(attribute="plan", operator=Operator.IS, values=["free"], negate=True)
# matches any user NOT on the free plan
```

### Available operators

| Category | Operators |
|---|---|
| Equality | `IS`, `IS_NOT` |
| String | `CONTAINS`, `NOT_CONTAINS`, `STARTS_WITH`, `ENDS_WITH`, `MATCHES`, `NOT_MATCHES` |
| Numeric | `GT`, `GTE`, `LT`, `LTE` |
| Date | `BEFORE`, `AFTER` (ISO-8601 string comparison) |
| Collection | `IN`, `NOT_IN` |
| Segment | `IN_SEGMENT`, `NOT_IN_SEGMENT` |
| Semver | `SEMVER_EQ`, `SEMVER_LT`, `SEMVER_GT` |

---

## Individual targeting

Override rules for specific users by listing their context keys in `targets`. Individual targets are evaluated after prerequisites but before rules, and always win.

```python
FeatureFlag(
    key="new-checkout",
    ...
    targets={
        "on": ["beta_tester_1", "beta_tester_2"],   # these users always get "on"
        "off": ["opted_out_user"],                   # this user always gets "off"
    },
)
```

---

## Segments

A segment is a named, reusable group of users. Define it once and reference it in any flag's targeting rules with `Operator.IN_SEGMENT`.

### Creating a segment

```python
from waygate import Segment, SegmentRule, RuleClause, Operator

# Explicit include list
await engine.save_segment(Segment(
    key="beta-users",
    name="Beta Users",
    included=["user_123", "user_456", "user_789"],
))

# Attribute-based rules (any matching rule → user is in the segment)
await engine.save_segment(Segment(
    key="enterprise-plan",
    name="Enterprise Plan",
    rules=[
        SegmentRule(clauses=[
            RuleClause(attribute="plan", operator=Operator.IS, values=["enterprise"]),
        ]),
    ],
))

# Exclude specific users even if they match a rule
await engine.save_segment(Segment(
    key="paid-users",
    name="Paid Users",
    rules=[
        SegmentRule(clauses=[
            RuleClause(attribute="plan", operator=Operator.IN, values=["pro", "enterprise"]),
        ]),
    ],
    excluded=["test_account", "demo_user"],  # always excluded, overrides rules
))
```

Pass `audit=False` when seeding segments at startup, same as with flags.

### Segment evaluation order

For a given context key `k`:

1. `k` in `excluded` → **not** in segment
2. `k` in `included` → in segment
3. Any `rules` entry matches → in segment
4. Otherwise → not in segment

!!! important "Segment key ≠ user key"
    The segment **key** (e.g. `"beta-users"`) is the segment's identifier. To make a user with `user_id="alice"` part of this segment, add `"alice"` to `included` — or add a segment rule that matches her attributes. Simply naming the segment `"alice"` does not put her in it.

### Using a segment in a flag rule

```python
TargetingRule(
    description="Beta users get the new flow",
    clauses=[
        RuleClause(
            attribute="key",          # evaluates ctx.key against the segment
            operator=Operator.IN_SEGMENT,
            values=["beta-users"],    # segment key to reference
        )
    ],
    variation="on",
)
```

### Managing segments from the dashboard

Open the **Segments** page (`/waygate/segments`) and click a segment key or **Edit** to:

- Add or remove users from the **Included** and **Excluded** lists
- Add **targeting rules** — attribute-based conditions evaluated when a user isn't in the explicit lists

### Managing segments from the CLI

```bash
# List all segments
waygate segments list

# Inspect a segment
waygate segments get beta-users

# Create a segment
waygate segments create beta_users --name "Beta Users"

# Add users to the included list
waygate segments include beta_users --context-key user_123,user_456

# Remove users via the excluded list
waygate segments exclude beta_users --context-key opted_out_user

# Add an attribute-based targeting rule
waygate segments add-rule beta_users --attribute plan --operator in --values pro,enterprise
waygate segments add-rule beta_users --attribute country --operator is --values GB --description "UK users"

# Remove a rule (use 'waygate segments get' to find rule IDs)
waygate segments remove-rule beta_users --rule-id <uuid>

# Delete a segment
waygate segments delete beta_users
```

---

## Prerequisites

Prerequisites let a flag depend on another flag. The dependent flag only proceeds to its rules if the prerequisite flag evaluates to a specific variation.

```python
from waygate import Prerequisite

FeatureFlag(
    key="advanced-dashboard",
    ...
    prerequisites=[
        Prerequisite(flag_key="auth-v2", variation="enabled"),
        # advanced-dashboard only evaluates if auth-v2 → "enabled"
    ],
)
```

Prerequisites are recursive up to a depth of 10. Circular dependencies are prevented at write time.

---

## Sync evaluation (plain `def` handlers)

FastAPI runs plain `def` route handlers in a thread pool. Use `engine.sync.flag_client` for thread-safe synchronous evaluation without any event loop bridging:

```python
@router.get("/dashboard")
def dashboard(request: Request, user_id: str = "anonymous"):
    enabled = engine.sync.flag_client.get_boolean_value(
        "new-dashboard", False, {"targeting_key": user_id}
    )
    return {"new_dashboard": enabled}
```

---

## Admin dashboard

### Flags page (`/waygate/flags`)

Lists all flags with key, type, status, variations, and fallthrough. Use the search box and type/status filters to narrow the list. Click a flag key to open the detail page.

### Flag detail page

| Tab | Contents |
|---|---|
| **Overview** | Key metrics: evaluation count, rule match rate, fallthrough rate, top variations |
| **Targeting** | Add / remove prerequisite flags; manage individual targets; add / edit / delete targeting rules |
| **Variations** | Add, rename, or remove variations; change the fallthrough and off-variation |
| **Settings** | Edit name, description, tags, maintainer, temporary flag flag, and scheduled changes |

### Segments page (`/waygate/segments`)

Lists all segments with included/excluded/rules counts. Click a segment to open its detail modal, or use the **Edit** button to manage included, excluded, and targeting rules.

---

## CLI reference

### `waygate flags`

```bash
waygate flags list                              # all flags
waygate flags get new-checkout                  # flag detail
waygate flags create new-checkout boolean       # create (interactive prompts follow)
waygate flags enable new-checkout               # enable (kill-switch off)
waygate flags disable new-checkout              # disable (kill-switch on)
waygate flags delete new-checkout               # permanently delete

waygate flags eval new-checkout --user user_123  # evaluate for a user

waygate flags targeting new-checkout            # show targeting rules
waygate flags add-rule new-checkout \
    --variation on \
    --segment beta-users                       # add segment-based rule
waygate flags add-rule new-checkout \
    --variation on \
    --attribute plan --operator in --values pro,enterprise
waygate flags remove-rule new-checkout --rule-id <uuid>

waygate flags add-prereq new-checkout --flag auth-v2 --variation enabled
waygate flags remove-prereq new-checkout --flag auth-v2

waygate flags target new-checkout --variation on --context-key user_123
waygate flags untarget new-checkout --context-key user_123

waygate flags variations new-checkout          # list variations
waygate flags edit new-checkout                # open interactive editor
```

### `waygate segments`

```bash
waygate segments list
waygate segments get beta-users
waygate segments create beta_users --name "Beta Users"
waygate segments include beta_users --context-key user_123,user_456
waygate segments exclude beta_users --context-key opted_out
waygate segments add-rule beta_users --attribute plan --operator in --values pro,enterprise
waygate segments remove-rule beta_users --rule-id <uuid>
waygate segments delete beta_users
```

---

## Full example

Full example at [`examples/fastapi/feature_flags.py`](https://github.com/Attakay78/waygate/blob/main/examples/fastapi/feature_flags.py), covering all five flag types, individual targeting, attribute-based rules, percentage rollouts, and sync and async evaluation.

Run it with:

```bash
uv run uvicorn examples.fastapi.feature_flags:app --reload
```

Then visit:

- `http://localhost:8000/docs` — Swagger UI
- `http://localhost:8000/waygate/flags` — flag management dashboard
- `http://localhost:8000/checkout?user_id=beta_tester_1` — targeted user (always `"on"`)
- `http://localhost:8000/checkout?user_id=anyone_else` — 20% rollout

---

## Next step

[**Reference: Feature Flags →**](../reference/feature-flags.md)
