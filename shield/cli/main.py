"""api-shield CLI — ``shield`` command.

Configuration is loaded in priority order (highest wins):
  1. Environment variables
  2. Config file specified with ``--config``
  3. Auto-discovered ``.shield`` file (walks up from current directory)
  4. Built-in defaults

Environment variables
---------------------
SHIELD_BACKEND      memory | file | redis | custom  (default: memory)
SHIELD_FILE_PATH    path to JSON file               (default: shield-state.json)
SHIELD_REDIS_URL    Redis connection URL            (default: redis://localhost:6379/0)
SHIELD_CUSTOM_PATH  dotted path to a zero-arg factory when SHIELD_BACKEND=custom
                    (e.g. myapp.backends:make_backend)
SHIELD_ENV          current environment             (default: production)

Custom backends
---------------
Set SHIELD_BACKEND=custom and point SHIELD_CUSTOM_PATH to a zero-arg factory:
    SHIELD_BACKEND=custom SHIELD_CUSTOM_PATH=myapp.backends:make_backend shield status
The factory is imported and called with no arguments.

Usage examples:
    shield status
    shield status /api/payments
    shield --config /etc/myapp/.shield status
    shield --config .env.production disable /api/payments --reason "migration"
    shield enable /api/payments
    shield disable /api/payments --reason "migration" --until 2h
    shield maintenance /api/payments --reason "DB swap" --start 2025-03-10T02:00Z \\
        --end 2025-03-10T04:00Z
    shield schedule /api/payments --start 2025-03-10T02:00Z --end 2025-03-10T04:00Z
    shield log
    shield log --route /api/payments --limit 50
"""

from __future__ import annotations

import getpass
from datetime import UTC, datetime, timedelta

import anyio
import typer
from rich import box
from rich.console import Console
from rich.table import Table

from shield.core.config import make_engine as _cfg_make_engine
from shield.core.exceptions import RouteProtectedException


def _current_user() -> str:
    """Return the logged-in OS username, falling back to 'cli'."""
    try:
        return getpass.getuser()
    except Exception:
        return "cli"


# Resolved once at import time so every CLI invocation uses the real OS user.
_CLI_ACTOR: str = _current_user()

cli = typer.Typer(
    name="shield",
    help="api-shield — route lifecycle management CLI",
    no_args_is_help=True,
)
console = Console()
err_console = Console(stderr=True)

# ---------------------------------------------------------------------------
# Global --config option (captured by the app callback, used by every command)
# ---------------------------------------------------------------------------

# Module-level slot populated by @app.callback before any command runs.
_config_file: str | None = None


@cli.callback()
def _global_options(
    config: str | None = typer.Option(
        None,
        "--config",
        "-c",
        help=(
            "Path to a KEY=value config file. "
            "Defaults to auto-discovering a .shield file in the current "
            "or any parent directory."
        ),
        show_default=False,
        metavar="FILE",
    ),
) -> None:
    """api-shield — route lifecycle management CLI."""
    global _config_file
    _config_file = config


def _make_engine():
    """Construct the engine, forwarding the active --config file."""
    return _cfg_make_engine(config_file=_config_file)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


def _parse_route(route: str) -> tuple[str | None, str, str]:
    """Parse a route argument into ``(method, path, storage_key)``.

    Accepted formats:
    - ``/payments``         → method=None, path="/payments", key="/payments"
    - ``GET:/payments``     → method="GET", path="/payments", key="GET:/payments"

    The returned *key* is the exact string used as the backend state key.
    """
    if ":" in route and not route.startswith("/"):
        raw_method, _, path = route.partition(":")
        method = raw_method.upper()
        if method not in _VALID_METHODS:
            raise typer.BadParameter(
                f"Unknown HTTP method {raw_method!r}. "
                f"Valid: {', '.join(sorted(_VALID_METHODS))}"
            )
        if not path.startswith("/"):
            raise typer.BadParameter(
                f"Path must start with '/'. Got: {path!r}"
            )
        return method, path, f"{method}:{path}"
    if not route.startswith("/"):
        raise typer.BadParameter(
            f"Route must start with '/' or use METHOD:/path format. Got: {route!r}"
        )
    return None, route, route


def _run_mutation(async_fn: object) -> None:
    """Run *async_fn* via anyio, showing a clear error on protected routes.

    Drop-in replacement for ``anyio.run(async_fn)`` in mutation commands.
    Catches ``RouteProtectedException`` and exits with a user-friendly message
    instead of an unhandled traceback.

    The engine is expected to be used inside *async_fn* as an async context
    manager (``async with engine:``) so that backend startup/shutdown lifecycle
    hooks are called correctly for any backend type.
    """
    try:
        anyio.run(async_fn)  # type: ignore[arg-type]
    except RouteProtectedException as exc:
        err_console.print(
            f"[red]Error:[/red] {exc}\n"
            "  [dim]Remove @force_active from the route decorator to allow "
            "lifecycle changes.[/dim]"
        )
        raise typer.Exit(code=1)


async def _require_registered(engine: object, key: str) -> None:
    """Exit with an error if *key* is not registered in the backend.

    Routes are registered when the server starts — both decorated routes
    (via their ``__shield_meta__``) and plain undecorated routes (registered
    as ACTIVE).  If a key is absent it means the path does not exist in the
    application at all, so the mutation is rejected to prevent silent typos.

    The check does NOT apply to the read-only ``shield status`` command —
    that always returns whatever state exists (or ACTIVE as a default).
    """
    exists = await engine.route_exists(key)  # type: ignore[attr-defined]
    if not exists:
        err_console.print(
            f"[red]Error:[/red] Route [bold]{key!r}[/bold] is not registered.\n"
            "  Only routes that exist in the running application can be managed.\n"
            "  Run [dim]shield status[/dim] to see all registered routes."
        )
        raise typer.Exit(code=1)


def _parse_until(until: str) -> datetime:
    """Parse a relative duration string like '2h', '30m', '1d' into a datetime."""
    unit = until[-1].lower()
    try:
        value = int(until[:-1])
    except ValueError:
        raise typer.BadParameter(f"Invalid duration: {until!r}. Use e.g. 2h, 30m, 1d")
    deltas = {
        "m": timedelta(minutes=value),
        "h": timedelta(hours=value),
        "d": timedelta(days=value),
    }
    if unit not in deltas:
        raise typer.BadParameter(f"Unknown time unit {unit!r}. Use m, h, or d")
    return datetime.now(UTC) + deltas[unit]


def _parse_dt(value: str) -> datetime:
    """Parse an ISO-8601 datetime string, attaching UTC if no timezone given."""
    for fmt in (
        "%Y-%m-%dT%H:%MZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.replace(tzinfo=UTC)
        except ValueError:
            continue
    # Try stdlib fromisoformat last.
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        raise typer.BadParameter(f"Cannot parse datetime: {value!r}")


def _status_colour(status: str) -> str:
    colours = {
        "active": "green",
        "maintenance": "yellow",
        "disabled": "red",
        "env_gated": "blue",
        "deprecated": "dim",
    }
    return colours.get(status, "white")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@cli.command("status")
def status(
    route: str | None = typer.Argument(
        None,
        help="Route: /path or METHOD:/path (e.g. GET:/payments). Omit for all.",
    ),
) -> None:
    """Show current shield status for all routes (or one route)."""

    async def _run():
        async with _make_engine() as engine:
            if route:
                _, _, key = _parse_route(route)
                state = await engine.get_state(key)
                states = [state]
            else:
                states = await engine.list_states()

            if not states:
                console.print("[dim]No routes registered.[/dim]")
                return

            table = Table(box=box.ROUNDED, show_header=True, header_style="bold")
            table.add_column("Route", style="cyan")
            table.add_column("Status")
            table.add_column("Reason")
            table.add_column("Envs")
            table.add_column("Window end")

            for s in sorted(states, key=lambda x: x.path):
                colour = _status_colour(s.status)
                window_end = (
                    s.window.end.strftime("%Y-%m-%d %H:%M UTC") if s.window else ""
                )
                table.add_row(
                    s.path,
                    f"[{colour}]{s.status.upper()}[/{colour}]",
                    s.reason or "—",
                    ", ".join(s.allowed_envs) if s.allowed_envs else "—",
                    window_end or "—",
                )

            console.print(table)

    anyio.run(_run)


@cli.command("enable")
def enable(
    route: str = typer.Argument(
        ...,
        help="Route: /path or METHOD:/path (e.g. GET:/payments)",
    ),
    reason: str = typer.Option(
        "", "--reason", "-r",
        help="Optional note explaining why the route is being re-enabled "
             "(e.g. 'Migration complete').  Stored in the audit log.",
    ),
) -> None:
    """Enable a route that is in maintenance or disabled state."""

    async def _run():
        _, _, key = _parse_route(route)
        async with _make_engine() as engine:
            await _require_registered(engine, key)
            state = await engine.enable(key, actor=_CLI_ACTOR, reason=reason)
            console.print(
                f"[green]✓[/green] {key} → [green]{state.status.upper()}[/green]"
            )
            if reason:
                console.print(f"  Reason: {reason}")

    _run_mutation(_run)


@cli.command("disable")
def disable_cmd(
    route: str = typer.Argument(
        ...,
        help="Route: /path or METHOD:/path (e.g. GET:/payments)",
    ),
    reason: str = typer.Option("", "--reason", "-r", help="Reason for disabling"),
    until: str | None = typer.Option(
        None, "--until", help="Re-enable after duration (e.g. 2h, 30m, 1d)"
    ),
) -> None:
    """Permanently disable a route (returns 503 to all callers)."""

    async def _run():
        _, _, key = _parse_route(route)
        async with _make_engine() as engine:
            await _require_registered(engine, key)
            state = await engine.disable(key, reason=reason, actor=_CLI_ACTOR)
            console.print(f"[red]✗[/red] {key} → [red]{state.status.upper()}[/red]")
            if reason:
                console.print(f"  Reason: {reason}")

            if until:
                end_dt = _parse_until(until)
                from shield.core.models import MaintenanceWindow

                window = MaintenanceWindow(
                    start=datetime.now(UTC), end=end_dt, reason=reason
                )
                await engine.schedule_maintenance(key, window, actor=_CLI_ACTOR)
                console.print(
                    "  Auto-re-enable scheduled for "
                    f"[cyan]{end_dt.strftime('%Y-%m-%d %H:%M UTC')}[/cyan]"
                )

    _run_mutation(_run)


@cli.command("maintenance")
def maintenance_cmd(
    route: str = typer.Argument(
        ...,
        help="Route: /path (all methods) or METHOD:/path (e.g. GET:/payments)",
    ),
    reason: str = typer.Option("", "--reason", "-r", help="Maintenance reason"),
    start: str | None = typer.Option(None, "--start", help="Window start (ISO-8601)"),
    end: str | None = typer.Option(None, "--end", help="Window end (ISO-8601)"),
) -> None:
    """Put a route into maintenance mode immediately."""

    async def _run():
        from shield.core.models import MaintenanceWindow

        _, _, key = _parse_route(route)
        async with _make_engine() as engine:
            await _require_registered(engine, key)
            window = None
            if start and end:
                window = MaintenanceWindow(
                    start=_parse_dt(start), end=_parse_dt(end), reason=reason
                )
            state = await engine.set_maintenance(
                key, reason=reason, window=window, actor=_CLI_ACTOR
            )
            console.print(
                f"[yellow]⚠[/yellow] {key} → [yellow]{state.status.upper()}[/yellow]"
            )
            if reason:
                console.print(f"  Reason: {reason}")
            if window:
                console.print(
                    f"  Window: {window.start.strftime('%Y-%m-%d %H:%M')} → "
                    f"{window.end.strftime('%Y-%m-%d %H:%M')} UTC"
                )

    _run_mutation(_run)


@cli.command("schedule")
def schedule_cmd(
    route: str = typer.Argument(
        ...,
        help="Route: /path (all methods) or METHOD:/path (e.g. GET:/payments)",
    ),
    start: str = typer.Option(..., "--start", help="Window start (ISO-8601)"),
    end: str = typer.Option(..., "--end", help="Window end (ISO-8601)"),
    reason: str = typer.Option("", "--reason", "-r", help="Maintenance reason"),
) -> None:
    """Schedule a future maintenance window (auto-activates and deactivates)."""

    async def _run():
        from shield.core.models import MaintenanceWindow

        _, _, key = _parse_route(route)
        async with _make_engine() as engine:
            await _require_registered(engine, key)
            window = MaintenanceWindow(
                start=_parse_dt(start), end=_parse_dt(end), reason=reason
            )
            await engine.schedule_maintenance(key, window, actor=_CLI_ACTOR)
            console.print(
                f"[cyan]⏰[/cyan] Scheduled maintenance for [bold]{key}[/bold]"
            )
            console.print(
                f"   Start: [cyan]{window.start.strftime('%Y-%m-%d %H:%M UTC')}[/cyan]"
            )
            console.print(
                f"   End:   [cyan]{window.end.strftime('%Y-%m-%d %H:%M UTC')}[/cyan]"
            )
            if reason:
                console.print(f"   Reason: {reason}")

    _run_mutation(_run)


@cli.command("log")
def log_cmd(
    route: str | None = typer.Option(
        None, "--route", help="Filter by route path"
    ),
    limit: int = typer.Option(20, "--limit", "-n", help="Number of entries to show"),
) -> None:
    """Show the audit log (most recent first)."""

    async def _run():
        async with _make_engine() as engine:
            entries = await engine.get_audit_log(path=route, limit=limit)

            if not entries:
                console.print("[dim]No audit entries found.[/dim]")
                return

            table = Table(box=box.ROUNDED, show_header=True, header_style="bold")
            table.add_column("Timestamp", style="dim")
            table.add_column("Route", style="cyan")
            table.add_column("Action")
            table.add_column("Actor")
            table.add_column("Old Status")
            table.add_column("New Status")
            table.add_column("Reason")

            for e in entries:
                old_colour = _status_colour(e.previous_status)
                new_colour = _status_colour(e.new_status)
                table.add_row(
                    e.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                    e.path,
                    e.action,
                    e.actor,
                    f"[{old_colour}]{e.previous_status}[/{old_colour}]",
                    f"[{new_colour}]{e.new_status}[/{new_colour}]",
                    e.reason or "—",
                )

            console.print(table)

    anyio.run(_run)


# ---------------------------------------------------------------------------
# Global maintenance command group  (shield global ...)
# ---------------------------------------------------------------------------

global_app = typer.Typer(
    name="global",
    help="Manage global maintenance mode (blocks all non-exempt routes).",
    no_args_is_help=True,
)
cli.add_typer(global_app, name="global")


@global_app.command("status")
def global_status() -> None:
    """Show the current global maintenance configuration."""

    async def _run():
        async with _make_engine() as engine:
            cfg = await engine.get_global_maintenance()

            state_str = "[green]OFF[/green]"
            if cfg.enabled:
                state_str = "[yellow]ON[/yellow]"

            console.print(f"\n  Global maintenance: {state_str}")
            if cfg.enabled:
                console.print(f"  Reason            : {cfg.reason or '—'}")
                fa_colour = "red" if cfg.include_force_active else "green"
                fa_text = "yes" if cfg.include_force_active else "no"
                console.print(
                    f"  Include @force_active: [{fa_colour}]{fa_text}[/{fa_colour}]"
                )
                if cfg.exempt_paths:
                    console.print("  Exempt paths      :")
                    for p in cfg.exempt_paths:
                        console.print(f"    • {p}")
                else:
                    console.print("  Exempt paths      : (none)")
            console.print()

    anyio.run(_run)


@global_app.command("enable")
def global_enable(
    reason: str = typer.Option(
        "", "--reason", "-r", help="Reason shown in 503 responses"
    ),
    exempt: list[str] | None = typer.Option(
        None,
        "--exempt",
        "-e",
        help=(
            "Route key to exempt from global maintenance "
            "(repeat for multiple). E.g. --exempt /health --exempt GET:/status"
        ),
    ),
    include_force_active: bool = typer.Option(
        False,
        "--include-force-active/--no-include-force-active",
        help=(
            "When set, @force_active routes are also blocked. "
            "Default: force-active routes remain reachable."
        ),
    ),
) -> None:
    """Enable global maintenance mode — all non-exempt routes return 503."""

    async def _run():
        async with _make_engine() as engine:
            cfg = await engine.enable_global_maintenance(
                reason=reason,
                exempt_paths=list(exempt) if exempt else [],
                include_force_active=include_force_active,
                actor=_CLI_ACTOR,
            )
            console.print(
                "[yellow]⚠[/yellow]  Global maintenance [yellow]ENABLED[/yellow]"
            )
            if cfg.reason:
                console.print(f"   Reason: {cfg.reason}")
            if cfg.exempt_paths:
                console.print(f"   Exempt: {', '.join(cfg.exempt_paths)}")
            if cfg.include_force_active:
                console.print("   [red]@force_active routes are also blocked.[/red]")

    anyio.run(_run)


@global_app.command("disable")
def global_disable() -> None:
    """Disable global maintenance mode, restoring normal per-route state."""

    async def _run():
        async with _make_engine() as engine:
            await engine.disable_global_maintenance(actor=_CLI_ACTOR)
            console.print(
                "[green]✓[/green]  Global maintenance [green]DISABLED[/green]"
            )

    anyio.run(_run)


@global_app.command("exempt-add")
def global_exempt_add(
    route: str = typer.Argument(
        ...,
        help="Route key to add: /path or METHOD:/path (e.g. GET:/health)",
    ),
) -> None:
    """Add a route to the global maintenance exempt list."""

    async def _run():
        async with _make_engine() as engine:
            cfg = await engine.get_global_maintenance()
            key = route if route.startswith("/") or ":" in route else f"/{route}"
            if key not in cfg.exempt_paths:
                updated = await engine.set_global_exempt_paths(
                    cfg.exempt_paths + [key]
                )
                console.print(
                    f"[green]✓[/green] Added [cyan]{key}[/cyan] to exempt list "
                    f"({len(updated.exempt_paths)} total)"
                )
            else:
                console.print(f"[dim]{key} is already in the exempt list.[/dim]")

    anyio.run(_run)


@global_app.command("exempt-remove")
def global_exempt_remove(
    route: str = typer.Argument(
        ...,
        help="Route key to remove: /path or METHOD:/path",
    ),
) -> None:
    """Remove a route from the global maintenance exempt list."""

    async def _run():
        async with _make_engine() as engine:
            cfg = await engine.get_global_maintenance()
            key = route if route.startswith("/") or ":" in route else f"/{route}"
            if key in cfg.exempt_paths:
                remaining = [p for p in cfg.exempt_paths if p != key]
                await engine.set_global_exempt_paths(remaining)
                console.print(
                    f"[green]✓[/green] Removed [cyan]{key}[/cyan] from exempt list"
                )
            else:
                console.print(f"[dim]{key} was not in the exempt list.[/dim]")

    anyio.run(_run)


if __name__ == "__main__":
    cli()
