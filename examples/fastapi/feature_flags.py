"""FastAPI — Feature Flags Example.

Demonstrates the full feature-flag API powered by OpenFeature:

  * Boolean / string / integer / float / JSON flag types
  * Async evaluation  (``await engine.flag_client.get_boolean_value(...)``)
  * Sync evaluation   (``engine.sync.flag_client.get_boolean_value(...)``)
  * EvaluationContext — per-request targeting based on user attributes
  * Individual targeting — specific users always get a fixed variation
  * Targeting rules — serve variations based on plan, country, app_version
  * Percentage rollout (fallthrough) — gradual feature release
  * Kill-switch — disable a flag globally without redeploying
  * Live event stream — watch evaluations in real time

Prerequisites:
    pip install waygate[flags]
    # or:
    uv pip install "waygate[flags]"

Run:
    uv run uvicorn examples.fastapi.feature_flags:app --reload

Then visit:
    http://localhost:8000/docs           — Swagger UI
    http://localhost:8000/waygate/        — admin dashboard (login: admin / secret)
    http://localhost:8000/waygate/flags   — flag management UI

Exercise the endpoints:
    # Boolean flag — new checkout flow (async route)
    curl "http://localhost:8000/checkout?user_id=user_123"

    # Boolean flag — new checkout flow (sync/def route)
    curl "http://localhost:8000/checkout/sync?user_id=user_123"

    # String flag — UI theme selection
    curl "http://localhost:8000/theme?user_id=beta_user_1"

    # Integer flag — max results per page
    curl "http://localhost:8000/search?user_id=pro_user_1&plan=pro"

    # Float flag — discount rate for a country segment
    curl "http://localhost:8000/pricing?user_id=uk_user_1&country=GB"

    # JSON flag — feature configuration bundle
    curl "http://localhost:8000/config?user_id=user_123"

    # Targeting: individual user always gets the beta variation
    curl "http://localhost:8000/checkout?user_id=beta_tester_1"

    # Live event stream (SSE) — watch evaluations happen in real time
    curl -N "http://localhost:8000/waygate/api/flags/stream"

CLI — manage flags without redeploying:
    waygate login admin          # password: secret
    waygate flags list
    waygate flags get new-checkout
    waygate flags disable new-checkout   # kill-switch
    waygate flags enable new-checkout    # restore
    waygate flags stream                 # tail live evaluations
    waygate flags stream new-checkout    # filter to one flag
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request

from waygate import (
    EvaluationContext,
    FeatureFlag,
    FlagType,
    FlagVariation,
    Operator,
    RolloutVariation,
    RuleClause,
    TargetingRule,
    make_engine,
)
from waygate.fastapi import (
    WaygateAdmin,
    WaygateMiddleware,
    WaygateRouter,
    apply_waygate_to_openapi,
)

# ---------------------------------------------------------------------------
# Engine setup
# ---------------------------------------------------------------------------

engine = make_engine()
engine.use_openfeature()

router = WaygateRouter(engine=engine)


# ---------------------------------------------------------------------------
# Seed flags at startup
# ---------------------------------------------------------------------------


async def _seed_flags() -> None:
    """Register all feature flags.

    In production you would persist flags to a shared backend (Redis, file)
    or manage them via the dashboard / REST API.  This function is for
    demonstration only — flags created here exist only in memory.
    """

    # ------------------------------------------------------------------
    # 1. Boolean flag — new checkout flow
    #
    #    Individual targeting: beta_tester_1 always sees the new flow.
    #    Fallthrough: 20% of remaining users get "on", 80% get "off".
    # ------------------------------------------------------------------
    await engine.save_flag(
        FeatureFlag(
            key="new-checkout",
            name="New Checkout Flow",
            description="Gradual rollout of the redesigned checkout experience.",
            type=FlagType.BOOLEAN,
            variations=[
                FlagVariation(name="on", value=True, description="New flow enabled"),
                FlagVariation(name="off", value=False, description="Legacy flow"),
            ],
            off_variation="off",
            # 20 % rollout — weights out of 100_000
            fallthrough=[
                RolloutVariation(variation="on", weight=20_000),
                RolloutVariation(variation="off", weight=80_000),
            ],
            targets={"on": ["beta_tester_1", "beta_tester_2"]},
        ),
        audit=False,
    )

    # ------------------------------------------------------------------
    # 2. String flag — UI theme
    #
    #    Rule: users whose email ends with "@acme.com" always get "dark".
    #    Fallthrough: everyone else gets "light".
    # ------------------------------------------------------------------
    await engine.save_flag(
        FeatureFlag(
            key="ui-theme",
            name="UI Theme",
            description="Default UI theme served to users.",
            type=FlagType.STRING,
            variations=[
                FlagVariation(name="light", value="light"),
                FlagVariation(name="dark", value="dark"),
                FlagVariation(name="system", value="system"),
            ],
            off_variation="light",
            fallthrough="light",
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
        ),
        audit=False,
    )

    # ------------------------------------------------------------------
    # 3. Integer flag — search results per page
    #
    #    Rule: "pro" and "enterprise" plans get 50 results.
    #    Fallthrough: free-tier users get 10.
    # ------------------------------------------------------------------
    await engine.save_flag(
        FeatureFlag(
            key="search-page-size",
            name="Search Page Size",
            description="Max results returned per search request.",
            type=FlagType.INTEGER,
            variations=[
                FlagVariation(name="small", value=10, description="Free tier"),
                FlagVariation(name="large", value=50, description="Pro / enterprise"),
            ],
            off_variation="small",
            fallthrough="small",
            rules=[
                TargetingRule(
                    description="Paid plans → large page size",
                    clauses=[
                        RuleClause(
                            attribute="plan",
                            operator=Operator.IN,
                            values=["pro", "enterprise"],
                        )
                    ],
                    variation="large",
                )
            ],
        ),
        audit=False,
    )

    # ------------------------------------------------------------------
    # 4. Float flag — regional discount rate
    #
    #    Rule: GB users get a 15 % discount.
    #    Rule: EU users get a 10 % discount.
    #    Fallthrough: no discount (0.0).
    # ------------------------------------------------------------------
    await engine.save_flag(
        FeatureFlag(
            key="discount-rate",
            name="Regional Discount Rate",
            description="Fractional discount applied at checkout (0.0 = none, 0.15 = 15%).",
            type=FlagType.FLOAT,
            variations=[
                FlagVariation(name="none", value=0.0),
                FlagVariation(name="eu", value=0.10),
                FlagVariation(name="gb", value=0.15),
            ],
            off_variation="none",
            fallthrough="none",
            rules=[
                TargetingRule(
                    description="GB → 15% discount",
                    clauses=[RuleClause(attribute="country", operator=Operator.IS, values=["GB"])],
                    variation="gb",
                ),
                TargetingRule(
                    description="EU → 10% discount",
                    clauses=[
                        RuleClause(
                            attribute="country",
                            operator=Operator.IN,
                            values=["DE", "FR", "NL", "SE", "PL"],
                        )
                    ],
                    variation="eu",
                ),
            ],
        ),
        audit=False,
    )

    # ------------------------------------------------------------------
    # 5. JSON flag — feature configuration bundle
    #
    #    Returns a structured dict with multiple settings in one round-trip.
    #    Useful for feature bundles that require several related values.
    # ------------------------------------------------------------------
    await engine.save_flag(
        FeatureFlag(
            key="feature-config",
            name="Feature Configuration Bundle",
            description="Combined config object for the new dashboard experience.",
            type=FlagType.JSON,
            variations=[
                FlagVariation(
                    name="v2",
                    value={
                        "sidebar": True,
                        "analytics": True,
                        "export_formats": ["csv", "xlsx", "json"],
                        "max_widgets": 20,
                    },
                    description="Full v2 dashboard",
                ),
                FlagVariation(
                    name="v1",
                    value={
                        "sidebar": False,
                        "analytics": False,
                        "export_formats": ["csv"],
                        "max_widgets": 5,
                    },
                    description="Legacy v1 dashboard",
                ),
            ],
            off_variation="v1",
            fallthrough="v1",
        ),
        audit=False,
    )


# ---------------------------------------------------------------------------
# Routes — async (def async)
# ---------------------------------------------------------------------------


@router.get("/checkout")
async def checkout(request: Request, user_id: str = "anonymous"):
    """Async route: evaluate the boolean ``new-checkout`` flag.

    Pass ``?user_id=beta_tester_1`` to see individual targeting in action.
    The flag is on a 20 % rollout for everyone else.
    """
    ctx = EvaluationContext(key=user_id)
    enabled = await engine.flag_client.get_boolean_value("new-checkout", False, ctx)
    return {
        "user_id": user_id,
        "new_checkout": enabled,
        "flow": "v2" if enabled else "v1",
    }


@router.get("/theme")
async def theme(request: Request, user_id: str = "anonymous", email: str = ""):
    """Async route: evaluate the string ``ui-theme`` flag.

    Pass ``?email=you@acme.com`` to trigger the corporate-user rule.
    """
    ctx = EvaluationContext(key=user_id, email=email or None)
    selected_theme = await engine.flag_client.get_string_value("ui-theme", "light", ctx)
    return {"user_id": user_id, "theme": selected_theme}


@router.get("/search")
async def search(request: Request, user_id: str = "anonymous", plan: str = "free"):
    """Async route: evaluate the integer ``search-page-size`` flag.

    Pass ``?plan=pro`` or ``?plan=enterprise`` to get the larger page size.
    """
    ctx = EvaluationContext(key=user_id, attributes={"plan": plan})
    page_size = await engine.flag_client.get_integer_value("search-page-size", 10, ctx)
    return {"user_id": user_id, "plan": plan, "page_size": page_size, "results": []}


@router.get("/pricing")
async def pricing(request: Request, user_id: str = "anonymous", country: str = "US"):
    """Async route: evaluate the float ``discount-rate`` flag.

    Pass ``?country=GB`` (15 %) or ``?country=DE`` (10 %).
    """
    ctx = EvaluationContext(key=user_id, country=country)
    discount = await engine.flag_client.get_float_value("discount-rate", 0.0, ctx)
    return {
        "user_id": user_id,
        "country": country,
        "discount_rate": discount,
        "price_usd": round(100.0 * (1 - discount), 2),
    }


@router.get("/config")
async def config(request: Request, user_id: str = "anonymous"):
    """Async route: evaluate the JSON ``feature-config`` flag.

    Returns the entire configuration bundle in a single evaluation call.
    """
    ctx = EvaluationContext(key=user_id)
    cfg: Any = await engine.flag_client.get_object_value(
        "feature-config", {"sidebar": False, "analytics": False}, ctx
    )
    return {"user_id": user_id, "config": cfg}


# ---------------------------------------------------------------------------
# Routes — sync (def, no async)
# ---------------------------------------------------------------------------
# FastAPI runs plain ``def`` handlers in a thread pool.
# ``engine.sync.flag_client`` provides a thread-safe synchronous facade over
# the same OpenFeature client — no asyncio bridge needed because flag
# evaluation is pure Python with no I/O.
# ---------------------------------------------------------------------------


@router.get("/checkout/sync")
def checkout_sync(request: Request, user_id: str = "anonymous"):
    """Sync route: evaluate the ``new-checkout`` flag from a ``def`` handler.

    Identical result to ``GET /checkout`` — use whichever matches your handler style.
    """
    enabled = engine.sync.flag_client.get_boolean_value(
        "new-checkout", False, {"targeting_key": user_id}
    )
    return {
        "user_id": user_id,
        "new_checkout": enabled,
        "flow": "v2" if enabled else "v1",
        "evaluated_in": "sync",
    }


@router.get("/search/sync")
def search_sync(request: Request, user_id: str = "anonymous", plan: str = "free"):
    """Sync route: evaluate the ``search-page-size`` flag from a ``def`` handler."""
    page_size = engine.sync.flag_client.get_integer_value(
        "search-page-size", 10, {"targeting_key": user_id, "plan": plan}
    )
    return {
        "user_id": user_id,
        "plan": plan,
        "page_size": page_size,
        "evaluated_in": "sync",
    }


# ---------------------------------------------------------------------------
# App assembly
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(_: FastAPI):
    await _seed_flags()
    yield


app = FastAPI(
    title="waygate — Feature Flags Example",
    description=(
        "Demonstrates boolean, string, integer, float, and JSON flags with "
        "targeting rules, rollouts, kill-switches, and live event streaming.\n\n"
        "Requires `waygate[flags]` (`pip install waygate[flags]`)."
    ),
    lifespan=lifespan,
)

app.add_middleware(WaygateMiddleware, engine=engine)
app.include_router(router)
apply_waygate_to_openapi(app, engine)

app.mount(
    "/waygate",
    WaygateAdmin(
        engine=engine,
        auth=("admin", "secret"),
        prefix="/waygate",
        # enable_flags is auto-detected from engine.use_openfeature() — no
        # need to set it explicitly.  Set to True/False to override.
    ),
)
