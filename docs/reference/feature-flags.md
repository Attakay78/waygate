# Feature Flags Reference

API reference for the feature flag system.

!!! note "Optional dependency"
    ```bash
    uv add "waygate[flags]"
    ```

---

## Engine methods

### `engine.use_openfeature()`

Activate the feature flag subsystem. Call once before any flag evaluation or flag/segment CRUD.

```python
engine = make_engine()
engine.use_openfeature()
```

---

### `engine.flag_client`

OpenFeature-compatible async flag client. Available after `use_openfeature()`.

```python
value = await engine.flag_client.get_boolean_value(flag_key, default, context)
value = await engine.flag_client.get_string_value(flag_key, default, context)
value = await engine.flag_client.get_integer_value(flag_key, default, context)
value = await engine.flag_client.get_float_value(flag_key, default, context)
value = await engine.flag_client.get_object_value(flag_key, default, context)
```

| Parameter | Type | Description |
|---|---|---|
| `flag_key` | `str` | The flag's unique key |
| `default` | `Any` | Returned when the flag is not found or an error occurs |
| `context` | `EvaluationContext` | Per-request context for targeting |

---

### `engine.sync.flag_client`

Thread-safe synchronous version for `def` (non-async) route handlers.

```python
enabled = engine.sync.flag_client.get_boolean_value("my-flag", False, ctx)
```

Accepts `EvaluationContext` objects or plain dicts (`{"targeting_key": user_id, ...}`).

---

### `await engine.save_flag(flag, *, actor, platform, action, audit)`

Create or replace a flag. Writes an audit log entry unless `audit=False`.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `flag` | `FeatureFlag` | required | Flag to persist |
| `actor` | `str` | `"system"` | Identity recorded in the audit log |
| `platform` | `str` | `""` | Surface recorded in the audit log (`"dashboard"`, `"cli"`, etc.) |
| `action` | `str \| None` | `None` | Override the audit action string. Defaults to `flag_created` or `flag_updated` based on whether the flag already existed |
| `audit` | `bool` | `True` | Set to `False` to skip writing an audit log entry. Use this for startup seeds and programmatic initialization |

```python
# Normal save — audited
await engine.save_flag(FeatureFlag(key="my-flag", ...))

# Startup seed — no audit entry
await engine.save_flag(FeatureFlag(key="my-flag", ...), audit=False)
```

---

### `await engine.get_flag(key)`

Return the `FeatureFlag` for `key`, or `None` if not found.

---

### `await engine.list_flags()`

Return all flags as a list.

---

### `await engine.delete_flag(key, *, actor, platform, audit)`

Delete a flag. Writes an audit log entry unless `audit=False`.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `key` | `str` | required | Key of the flag to delete |
| `actor` | `str` | `"system"` | Identity recorded in the audit log |
| `platform` | `str` | `""` | Surface recorded in the audit log |
| `audit` | `bool` | `True` | Set to `False` to skip the audit entry |

---

### `await engine.save_segment(segment, *, actor, platform, audit)`

Create or replace a segment. Writes an audit log entry unless `audit=False`.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `segment` | `Segment` | required | Segment to persist |
| `actor` | `str` | `"system"` | Identity recorded in the audit log |
| `platform` | `str` | `""` | Surface recorded in the audit log |
| `audit` | `bool` | `True` | Set to `False` to skip writing an audit log entry. Use this for startup seeds and programmatic initialization |

```python
# Startup seed — no audit entry
await engine.save_segment(Segment(key="beta-users", ...), audit=False)
```

---

### `await engine.get_segment(key)`

Return the `Segment` for `key`, or `None`.

---

### `await engine.list_segments()`

Return all segments as a list.

---

### `await engine.delete_segment(key, *, actor, platform, audit)`

Delete a segment. Writes an audit log entry unless `audit=False`.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `key` | `str` | required | Key of the segment to delete |
| `actor` | `str` | `"system"` | Identity recorded in the audit log |
| `platform` | `str` | `""` | Surface recorded in the audit log |
| `audit` | `bool` | `True` | Set to `False` to skip the audit entry |

---

## Models

### `FeatureFlag`

Definition of a feature flag.

```python
class FeatureFlag(BaseModel):
    key: str
    name: str
    description: str = ""
    type: FlagType
    tags: list[str] = []

    variations: list[FlagVariation]
    off_variation: str
    fallthrough: str | list[RolloutVariation]

    enabled: bool = True
    prerequisites: list[Prerequisite] = []
    targets: dict[str, list[str]] = {}
    rules: list[TargetingRule] = []
    scheduled_changes: list[ScheduledChange] = []

    status: FlagStatus = FlagStatus.ACTIVE
    temporary: bool = True
    maintainer: str | None = None
    created_at: datetime
    updated_at: datetime
    created_by: str = "system"
```

| Field | Description |
|---|---|
| `key` | Unique identifier used in code: `get_boolean_value("my-flag", ...)` |
| `name` | Human-readable display name |
| `type` | `FlagType.BOOLEAN`, `STRING`, `INTEGER`, `FLOAT`, or `JSON` |
| `variations` | All possible values; must contain at least two |
| `off_variation` | Variation served when `enabled=False` |
| `fallthrough` | Default when no rule matches: a variation name (`str`) or a percentage rollout (`list[RolloutVariation]`) |
| `enabled` | Kill-switch. `False` means all requests get `off_variation` |
| `prerequisites` | Flags that must pass before this flag's rules run |
| `targets` | Individual targeting: `{"on": ["user_1", "user_2"]}` |
| `rules` | Targeting rules evaluated top-to-bottom; first match wins |

---

### `FlagType`

```python
class FlagType(StrEnum):
    BOOLEAN = "boolean"
    STRING  = "string"
    INTEGER = "integer"
    FLOAT   = "float"
    JSON    = "json"
```

---

### `FlagVariation`

One possible value a flag can return.

```python
class FlagVariation(BaseModel):
    name: str          # e.g. "on", "off", "control", "variant_a"
    value: bool | str | int | float | dict | list
    description: str = ""
```

---

### `RolloutVariation`

One bucket in a percentage rollout (used in `fallthrough` or `TargetingRule.rollout`).

```python
class RolloutVariation(BaseModel):
    variation: str    # references FlagVariation.name
    weight: int       # share of traffic, out of 100_000 total
```

Weights in a rollout list must sum to `100_000`. Examples:

| Percentage | Weight |
|---|---|
| 10% | `10_000` |
| 25% | `25_000` |
| 33.33% | `33_333` |
| 50% | `50_000` |

---

### `TargetingRule`

A rule that matches clauses and serves a variation.

```python
class TargetingRule(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    description: str = ""
    clauses: list[RuleClause] = []
    variation: str | None = None          # mutually exclusive with rollout
    rollout: list[RolloutVariation] | None = None
    track_events: bool = False
```

Clauses within a rule are AND-ed. Rules are evaluated top-to-bottom; first match wins.

---

### `RuleClause`

A single condition within a targeting rule.

```python
class RuleClause(BaseModel):
    attribute: str        # context attribute to inspect, e.g. "plan", "country", "email"
    operator: Operator    # comparison to apply
    values: list[Any]     # one or more values; multiple values use OR logic
    negate: bool = False  # invert the result
```

---

### `Operator`

All supported targeting operators.

```python
class Operator(StrEnum):
    # Equality
    IS         = "is"
    IS_NOT     = "is_not"
    # String
    CONTAINS   = "contains"
    NOT_CONTAINS  = "not_contains"
    STARTS_WITH   = "starts_with"
    ENDS_WITH     = "ends_with"
    MATCHES       = "matches"        # Python regex
    NOT_MATCHES   = "not_matches"
    # Numeric
    GT  = "gt"
    GTE = "gte"
    LT  = "lt"
    LTE = "lte"
    # Date (ISO-8601 lexicographic)
    BEFORE = "before"
    AFTER  = "after"
    # Collection
    IN     = "in"
    NOT_IN = "not_in"
    # Segment
    IN_SEGMENT     = "in_segment"
    NOT_IN_SEGMENT = "not_in_segment"
    # Semantic version (requires `packaging`)
    SEMVER_EQ = "semver_eq"
    SEMVER_LT = "semver_lt"
    SEMVER_GT = "semver_gt"
```

---

### `Prerequisite`

A flag that must evaluate to a specific variation before the dependent flag runs.

```python
class Prerequisite(BaseModel):
    flag_key: str    # key of the prerequisite flag
    variation: str   # variation the prerequisite must return
```

If the prerequisite returns any other variation, the dependent flag serves `off_variation` with reason `PREREQUISITE_FAIL`.

---

### `Segment`

A reusable group of users for flag targeting.

```python
class Segment(BaseModel):
    key: str
    name: str
    description: str = ""
    included: list[str] = []     # context keys always in the segment
    excluded: list[str] = []     # context keys always excluded (overrides rules + included)
    rules: list[SegmentRule] = []
    tags: list[str] = []
    created_at: datetime
    updated_at: datetime
```

**Evaluation order for context key `k`:**

1. `k` in `excluded` → not in segment
2. `k` in `included` → in segment
3. Any `SegmentRule` matches → in segment
4. Otherwise → not in segment

---

### `SegmentRule`

An attribute-based rule inside a segment. Multiple segment rules are OR-ed: if any rule matches, the user is in the segment.

```python
class SegmentRule(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    description: str = ""
    clauses: list[RuleClause] = []    # all must match (AND logic)
```

---

### `EvaluationContext`

Per-request context for flag targeting and rollout bucketing.

```python
class EvaluationContext(BaseModel):
    key: str                           # required — user/session/org ID
    kind: str = "user"                 # context kind
    email: str | None = None
    ip: str | None = None
    country: str | None = None
    app_version: str | None = None
    attributes: dict[str, Any] = {}    # any additional attributes
```

Named fields (`email`, `ip`, `country`, `app_version`) are accessible in rule clauses by the same names. Items in `attributes` are merged in and accessible by key.

---

### `ResolutionDetails`

Full result of a flag evaluation, surfaced in hooks.

```python
class ResolutionDetails(BaseModel):
    value: Any
    variation: str | None = None
    reason: EvaluationReason
    rule_id: str | None = None            # set when reason == RULE_MATCH
    prerequisite_key: str | None = None   # set when reason == PREREQUISITE_FAIL
    error_message: str | None = None      # set when reason == ERROR
```

---

### `EvaluationReason`

Why a specific value was returned.

| Value | Description |
|---|---|
| `OFF` | Flag is globally disabled. `off_variation` was served. |
| `TARGET_MATCH` | Context key was in the individual targets list. |
| `RULE_MATCH` | A targeting rule matched. `rule_id` is set. |
| `FALLTHROUGH` | No targeting rule matched. Default rule was served. |
| `PREREQUISITE_FAIL` | A prerequisite flag did not return the required variation. |
| `ERROR` | Provider or evaluation error. Default value was returned. |
| `DEFAULT` | Flag not found. SDK default was returned. |

---

---

## REST API

When `WaygateAdmin` is mounted with `engine.use_openfeature()`, these endpoints are registered under the admin path (e.g. `/waygate/api/`):

### Flags

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/flags` | List all flags |
| `POST` | `/api/flags` | Create a flag (full `FeatureFlag` body) |
| `GET` | `/api/flags/{key}` | Get a single flag |
| `PUT` | `/api/flags/{key}` | Replace a flag (full update) |
| `PATCH` | `/api/flags/{key}` | Partial update |
| `DELETE` | `/api/flags/{key}` | Delete a flag |
| `POST` | `/api/flags/{key}/enable` | Enable (kill-switch off) |
| `POST` | `/api/flags/{key}/disable` | Disable (kill-switch on) |
| `POST` | `/api/flags/{key}/evaluate` | Evaluate for a given context |

### Segments

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/segments` | List all segments |
| `POST` | `/api/segments` | Create a segment |
| `GET` | `/api/segments/{key}` | Get a single segment |
| `PUT` | `/api/segments/{key}` | Replace a segment |
| `DELETE` | `/api/segments/{key}` | Delete a segment |

---

## Evaluation algorithm

The evaluator (`FlagEvaluator`) is pure Python with no I/O, unit-testable in isolation.

```python
from waygate import FlagEvaluator

evaluator = FlagEvaluator(segments={"beta": beta_segment})
result = evaluator.evaluate(flag, ctx, all_flags)
print(result.value, result.reason)
```

**Rollout bucketing** uses SHA-1 of `"{flag_key}:{ctx.kind}:{ctx.key}"` modulo `100_000`. The same context always lands in the same bucket; bucketing is stable across restarts and deploys.

**Prerequisite recursion** is limited to depth 10. Circular dependencies are rejected at write time by `engine.save_flag()`.

---

## Dashboard routes

| URL | Page |
|---|---|
| `/waygate/flags` | Flag list with search and status filters |
| `/waygate/flags/{key}` | Flag detail (4 tabs: Overview, Targeting, Variations, Settings) |
| `/waygate/segments` | Segment list |

---

## Example

Example: [`examples/fastapi/feature_flags.py`](https://github.com/Attakay78/waygate/blob/main/examples/fastapi/feature_flags.py)

```bash
uv run uvicorn examples.fastapi.feature_flags:app --reload
```
