"""api-shield CLI — ``shield`` command.

The CLI is a **thin HTTP client** that communicates with a running
:func:`~shield.admin.app.ShieldAdmin` instance mounted on your FastAPI
application.  It never touches the backend directly.

Quick start
-----------
1. Mount :class:`~shield.admin.app.ShieldAdmin` on your app::

       admin = ShieldAdmin(engine=engine, auth=("admin", "secret"))
       app.mount("/shield", admin)

2. Point the CLI at your running server::

       shield config set-url http://localhost:8000/shield

3. Authenticate (when auth is configured)::

       shield login admin          # prompts for password
       # or: shield login --username admin --password secret

4. Manage routes::

       shield status
       shield disable /api/payments --reason "migration"
       shield enable /api/payments

Authentication
--------------
Credentials are stored in ``~/.shield/config.json`` with an expiry
timestamp.  After expiry you must re-authenticate with ``shield login``.

Token expiry is controlled server-side via ``ShieldAdmin(token_expiry=…)``.
"""

from __future__ import annotations

import getpass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import anyio
import typer
from rich import box
from rich.console import Console
from rich.table import Table

from shield.cli import config as _cfg
from shield.cli.client import ShieldClient, ShieldClientError, make_client

if TYPE_CHECKING:
    pass

cli = typer.Typer(
    name="shield",
    help="api-shield — route lifecycle management CLI",
    no_args_is_help=True,
)
console = Console()
err_console = Console(stderr=True)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _status_colour(status: str) -> str:
    return {
        "active": "green",
        "maintenance": "yellow",
        "disabled": "red",
        "env_gated": "blue",
        "deprecated": "dim",
    }.get(status, "white")


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
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        raise typer.BadParameter(f"Cannot parse datetime: {value!r}")


_VALID_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


def _parse_route(route: str) -> str:
    """Validate and return the route key (``/path`` or ``METHOD:/path``)."""
    if ":" in route and not route.startswith("/"):
        raw_method, _, path = route.partition(":")
        method = raw_method.upper()
        if method not in _VALID_METHODS:
            raise typer.BadParameter(
                f"Unknown HTTP method {raw_method!r}. Valid: {', '.join(sorted(_VALID_METHODS))}"
            )
        if not path.startswith("/"):
            raise typer.BadParameter(f"Path must start with '/'. Got: {path!r}")
        return f"{method}:{path}"
    if not route.startswith("/"):
        raise typer.BadParameter(
            f"Route must start with '/' or use METHOD:/path format. Got: {route!r}"
        )
    return route


def _run(coro_fn: object) -> None:
    """Run an async function, translating HTTP errors to user-friendly messages."""
    try:
        anyio.run(coro_fn)  # type: ignore[arg-type]
    except ShieldClientError as exc:
        if exc.status_code == 401:
            err_console.print(
                "[red]Error:[/red] Authentication required.\n"
                "  Run: [bold]shield login <username>[/bold]"
            )
        elif exc.status_code == 404:
            err_console.print(f"[red]Error:[/red] {exc}")
        elif exc.status_code == 409 and not exc.ambiguous_matches:
            err_console.print(
                f"[red]Error:[/red] {exc}\n"
                "  [dim]Remove @force_active from the route decorator to "
                "allow lifecycle changes.[/dim]"
            )
        else:
            err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1)
    except SystemExit:
        raise
    except Exception as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1)


_DEFAULT_PER_PAGE = 20


def _paginate(
    items: list[Any],
    page: int,
    per_page: int,
) -> tuple[list[Any], bool, bool, int, int]:
    """Slice *items* for *page*.

    *items* must have been fetched with ``limit = page * per_page + 1`` so
    that we can detect whether a next page exists without knowing the total.

    Returns ``(page_items, has_prev, has_next, first_num, last_num)`` where
    the nums are 1-based display indices.
    """
    start = (page - 1) * per_page
    end = page * per_page
    has_next = len(items) > end
    page_items = items[start : min(end, len(items))]
    return page_items, page > 1, has_next, start + 1, start + len(page_items)


def _print_page_footer(
    page: int,
    per_page: int,
    first_num: int,
    last_num: int,
    has_prev: bool,
    has_next: bool,
) -> None:
    """Print a compact pagination footer beneath a table."""
    parts: list[str] = [f"[dim]Showing {first_num}–{last_num}[/dim]"]
    if has_prev:
        parts.append(f"[dim]← --page {page - 1}[/dim]")
    if has_next:
        parts.append(f"[dim]--page {page + 1} →[/dim]")
    else:
        parts.append("[dim](last page)[/dim]")
    console.print("  " + "  [dim]•[/dim]  ".join(parts))


def _confirm_ambiguous(matches: list[str], action: str) -> list[str]:
    """Prompt the user to confirm applying *action* to all *matches*.

    Returns the list of route keys to operate on, or exits if the user
    declines.
    """
    console.print(f"[yellow]⚠[/yellow]  Route is ambiguous — matches {len(matches)} routes:")
    for m in matches:
        console.print(f"    • [cyan]{m}[/cyan]")
    if not typer.confirm(f"Apply '{action}' to all {len(matches)} routes?", default=False):
        console.print("[dim]Aborted.[/dim]")
        raise typer.Exit(code=0)
    return matches


# ---------------------------------------------------------------------------
# Authentication commands
# ---------------------------------------------------------------------------


@cli.command("login")
def login(
    username: str | None = typer.Argument(
        None,
        help="Username to authenticate with.  Omit to be prompted.",
    ),
    password: str | None = typer.Option(
        None,
        "--password",
        "-p",
        help="Password.  Omit to be prompted securely.",
    ),
) -> None:
    """Authenticate with the Shield admin server and store a token locally."""

    async def _run_login() -> None:
        if not username:
            u = typer.prompt("Username")
        else:
            u = username

        if not password:
            p = getpass.getpass("Password: ")
        else:
            p = password

        client = ShieldClient(base_url=_cfg.require_server_url())
        result = await client.login(u, p)

        _cfg.set_auth(
            token=result["token"],
            username=result["username"],
            expires_at=result["expires_at"],
        )
        expires = result.get("expires_at", "")
        console.print(f"[green]✓[/green] Logged in as [bold]{result['username']}[/bold]")
        if expires:
            console.print(f"  Token expires: [dim]{expires}[/dim]")

    _run(_run_login)


@cli.command("logout")
def logout() -> None:
    """Revoke the stored token and clear local credentials."""

    async def _run_logout() -> None:
        token = _cfg.get_auth_token()
        if token:
            client = make_client()
            await client.logout()
        _cfg.clear_auth()
        console.print("[green]✓[/green] Logged out.")

    _run(_run_logout)


# ---------------------------------------------------------------------------
# Configuration commands
# ---------------------------------------------------------------------------

config_app = typer.Typer(
    name="config",
    help="Manage CLI configuration (server URL, etc.).",
    no_args_is_help=True,
)
cli.add_typer(config_app, name="config")


@config_app.command("set-url")
def config_set_url(
    url: str = typer.Argument(..., help="Base URL of the ShieldAdmin mount point."),
) -> None:
    """Set the Shield admin server URL."""
    _cfg.set_server_url(url)
    console.print(f"[green]✓[/green] Server URL set to [cyan]{url.rstrip('/')}[/cyan]")


@config_app.command("show")
def config_show() -> None:
    """Show the current CLI configuration."""
    server_url = _cfg.get_server_url()
    url_source = _cfg.get_server_url_source()
    username = _cfg.get_auth_username()
    expires_at = _cfg.get_token_expires_at()
    has_valid_token = _cfg.is_authenticated()

    table = Table(box=box.ROUNDED, show_header=False)
    table.add_column("Key", style="dim")
    table.add_column("Value")

    table.add_row("Server URL", f"{server_url}  [dim]({url_source})[/dim]")
    table.add_row("Username", username or "[dim](not logged in)[/dim]")

    if expires_at:
        exp_style = "green" if has_valid_token else "red"
        table.add_row("Token expires", f"[{exp_style}]{expires_at}[/{exp_style}]")
    else:
        table.add_row("Token expires", "[dim](no token)[/dim]")

    table.add_row(
        "Config file",
        str(_cfg.get_config_path()),
    )
    console.print(table)


# ---------------------------------------------------------------------------
# Route commands
# ---------------------------------------------------------------------------


@cli.command("status")
def status(
    route: str | None = typer.Argument(
        None,
        help="Route: /path or METHOD:/path. Omit for all routes.",
    ),
    service: str | None = typer.Option(
        None,
        "--service",
        "-s",
        envvar="SHIELD_SERVICE",
        help="Filter to routes for a specific service. Falls back to SHIELD_SERVICE env var.",
    ),
    page: int = typer.Option(1, "--page", "-p", help="Page number (when listing all routes)."),
    per_page: int = typer.Option(_DEFAULT_PER_PAGE, "--per-page", help="Rows per page."),
) -> None:
    """Show current shield status for all routes (or one route)."""

    async def _run_status() -> None:
        client = make_client()
        if route:
            key = _parse_route(route)
            states = [await client.get_route(key)]
            paginated, has_prev, has_next, first_num, last_num = states, False, False, 1, 1
        else:
            all_states = sorted(await client.list_routes(service=service), key=lambda x: x["path"])
            paginated, has_prev, has_next, first_num, last_num = _paginate(
                all_states, page, per_page
            )

        if not paginated:
            msg = (
                f"No routes registered for service [bold]{service}[/bold]."
                if service
                else "No routes registered."
            )
            console.print(f"[dim]{msg}[/dim]")
            return

        # Show Service column only when listing all services (no filter active).
        show_service = not service and any(s.get("service") for s in paginated)

        table = Table(box=box.ROUNDED, show_header=True, header_style="bold")
        table.add_column("Route", style="cyan")
        if show_service:
            table.add_column("Service", style="dim")
        table.add_column("Status")
        table.add_column("Reason")
        table.add_column("Envs")
        table.add_column("Window end")

        for s in paginated:
            colour = _status_colour(s["status"])
            window = s.get("window") or {}
            window_end = ""
            if window and window.get("end"):
                try:
                    dt = datetime.fromisoformat(window["end"])
                    window_end = dt.strftime("%Y-%m-%d %H:%M UTC")
                except Exception:
                    window_end = window["end"]
            envs = s.get("allowed_envs") or []
            # Strip service prefix from path for display.
            svc = s.get("service") or ""
            raw_path = s["path"]
            display_path = (
                raw_path[len(svc) + 1 :] if svc and raw_path.startswith(f"{svc}:") else raw_path
            )
            row = [display_path]
            if show_service:
                row.append(svc or "—")
            row += [
                f"[{colour}]{s['status'].upper()}[/{colour}]",
                s.get("reason") or "—",
                ", ".join(envs) if envs else "—",
                window_end or "—",
            ]
            table.add_row(*row)

        if service:
            console.print(f"[dim]Service: [bold]{service}[/bold][/dim]")
        console.print(table)
        if not route and (has_prev or has_next):
            _print_page_footer(page, per_page, first_num, last_num, has_prev, has_next)

    _run(_run_status)


@cli.command("services")
def list_services_cmd() -> None:
    """List all services that have registered routes with this Shield Server."""

    async def _run_services() -> None:
        client = make_client()
        try:
            services = await client.list_services()
        except Exception:
            services = []
        if not services:
            console.print("[dim]No services registered.[/dim]")
            return
        table = Table(box=box.ROUNDED, show_header=True, header_style="bold")
        table.add_column("Service", style="cyan")
        for svc in services:
            table.add_row(svc)
        console.print(table)

    _run(_run_services)


@cli.command("current-service")
def current_service_cmd() -> None:
    """Show the active service context (set via SHIELD_SERVICE env var)."""
    import os

    svc = os.environ.get("SHIELD_SERVICE", "")
    if svc:
        console.print(
            f"Active service: [cyan bold]{svc}[/cyan bold]  [dim](from SHIELD_SERVICE)[/dim]"
        )
    else:
        console.print(
            "[dim]No active service set.[/dim]\n"
            "Set one with: [cyan]export SHIELD_SERVICE=<service-name>[/cyan]"
        )


@cli.command("enable")
def enable(
    route: str = typer.Argument(..., help="Route: /path or METHOD:/path"),
    reason: str = typer.Option("", "--reason", "-r", help="Optional note for the audit log."),
    service: str | None = typer.Option(
        None,
        "--service",
        "-s",
        envvar="SHIELD_SERVICE",
        help="Service name (SDK multi-service mode). Falls back to SHIELD_SERVICE env var.",
    ),
) -> None:
    """Enable a route that is in maintenance or disabled state."""

    async def _run_enable() -> None:
        key = f"{service}:{_parse_route(route)}" if service else _parse_route(route)
        client = make_client()
        try:
            keys_to_apply = [key]
            state = await client.enable(key, reason=reason)
            states = [state]
        except ShieldClientError as exc:
            if not exc.ambiguous_matches:
                raise
            keys_to_apply = _confirm_ambiguous(exc.ambiguous_matches, "enable")
            states = [await client.enable(k, reason=reason) for k in keys_to_apply]

        for k, state in zip(keys_to_apply, states):
            console.print(f"[green]✓[/green] {k} → [green]{state['status'].upper()}[/green]")
        if reason:
            console.print(f"  Reason: {reason}")

    _run(_run_enable)


@cli.command("disable")
def disable_cmd(
    route: str = typer.Argument(..., help="Route: /path or METHOD:/path"),
    reason: str = typer.Option("", "--reason", "-r", help="Reason for disabling."),
    until: str | None = typer.Option(
        None, "--until", help="Re-enable after duration (e.g. 2h, 30m, 1d)."
    ),
    service: str | None = typer.Option(
        None,
        "--service",
        "-s",
        envvar="SHIELD_SERVICE",
        help="Service name (SDK multi-service mode). Falls back to SHIELD_SERVICE env var.",
    ),
) -> None:
    """Permanently disable a route (returns 503 to all callers)."""

    async def _run_disable() -> None:
        key = f"{service}:{_parse_route(route)}" if service else _parse_route(route)
        client = make_client()
        try:
            keys_to_apply = [key]
            state = await client.disable(key, reason=reason)
            states = [state]
        except ShieldClientError as exc:
            if not exc.ambiguous_matches:
                raise
            keys_to_apply = _confirm_ambiguous(exc.ambiguous_matches, "disable")
            states = [await client.disable(k, reason=reason) for k in keys_to_apply]

        for k, state in zip(keys_to_apply, states):
            console.print(f"[red]✗[/red] {k} → [red]{state['status'].upper()}[/red]")
        if reason:
            console.print(f"  Reason: {reason}")

        if until:
            end_dt = _parse_until(until)
            for k in keys_to_apply:
                await client.schedule(
                    k,
                    start=datetime.now(UTC).isoformat(),
                    end=end_dt.isoformat(),
                    reason=reason,
                )
            console.print(
                "  Auto-re-enable scheduled for "
                f"[cyan]{end_dt.strftime('%Y-%m-%d %H:%M UTC')}[/cyan]"
            )

    _run(_run_disable)


@cli.command("maintenance")
def maintenance_cmd(
    route: str = typer.Argument(..., help="Route: /path or METHOD:/path"),
    reason: str = typer.Option("", "--reason", "-r", help="Maintenance reason."),
    start: str | None = typer.Option(None, "--start", help="Window start (ISO-8601)."),
    end: str | None = typer.Option(None, "--end", help="Window end (ISO-8601)."),
    service: str | None = typer.Option(
        None,
        "--service",
        "-s",
        envvar="SHIELD_SERVICE",
        help="Service name (SDK multi-service mode). Falls back to SHIELD_SERVICE env var.",
    ),
) -> None:
    """Put a route into maintenance mode immediately."""

    async def _run_maintenance() -> None:
        key = f"{service}:{_parse_route(route)}" if service else _parse_route(route)
        start_iso = _parse_dt(start).isoformat() if start else None
        end_iso = _parse_dt(end).isoformat() if end else None
        client = make_client()
        try:
            keys_to_apply = [key]
            state = await client.maintenance(key, reason=reason, start=start_iso, end=end_iso)
            states = [state]
        except ShieldClientError as exc:
            if not exc.ambiguous_matches:
                raise
            keys_to_apply = _confirm_ambiguous(exc.ambiguous_matches, "maintenance")
            states = [
                await client.maintenance(k, reason=reason, start=start_iso, end=end_iso)
                for k in keys_to_apply
            ]

        for k, state in zip(keys_to_apply, states):
            console.print(f"[yellow]⚠[/yellow] {k} → [yellow]{state['status'].upper()}[/yellow]")
        if reason:
            console.print(f"  Reason: {reason}")

    _run(_run_maintenance)


@cli.command("schedule")
def schedule_cmd(
    route: str = typer.Argument(..., help="Route: /path or METHOD:/path"),
    start: str = typer.Option(..., "--start", help="Window start (ISO-8601)."),
    end: str = typer.Option(..., "--end", help="Window end (ISO-8601)."),
    reason: str = typer.Option("", "--reason", "-r", help="Maintenance reason."),
    service: str | None = typer.Option(
        None,
        "--service",
        "-s",
        envvar="SHIELD_SERVICE",
        help="Service name (SDK multi-service mode). Falls back to SHIELD_SERVICE env var.",
    ),
) -> None:
    """Schedule a future maintenance window (auto-activates and deactivates)."""

    async def _run_schedule() -> None:
        key = f"{service}:{_parse_route(route)}" if service else _parse_route(route)
        start_dt = _parse_dt(start)
        end_dt = _parse_dt(end)
        client = make_client()
        try:
            keys_to_apply = [key]
            await client.schedule(
                key, start=start_dt.isoformat(), end=end_dt.isoformat(), reason=reason
            )
        except ShieldClientError as exc:
            if not exc.ambiguous_matches:
                raise
            keys_to_apply = _confirm_ambiguous(exc.ambiguous_matches, "schedule")
            for k in keys_to_apply:
                await client.schedule(
                    k, start=start_dt.isoformat(), end=end_dt.isoformat(), reason=reason
                )

        for k in keys_to_apply:
            console.print(f"[cyan]⏰[/cyan] Scheduled maintenance for [bold]{k}[/bold]")
        console.print(f"   Start: [cyan]{start_dt.strftime('%Y-%m-%d %H:%M UTC')}[/cyan]")
        console.print(f"   End:   [cyan]{end_dt.strftime('%Y-%m-%d %H:%M UTC')}[/cyan]")
        if reason:
            console.print(f"   Reason: {reason}")

    _run(_run_schedule)


@cli.command("log")
def log_cmd(
    route: str | None = typer.Option(None, "--route", help="Filter by route path."),
    page: int = typer.Option(1, "--page", "-p", help="Page number."),
    per_page: int = typer.Option(_DEFAULT_PER_PAGE, "--per-page", help="Rows per page."),
) -> None:
    """Show the audit log (most recent first)."""

    async def _run_log() -> None:
        fetch_limit = page * per_page + 1
        entries = await make_client().audit_log(route=route, limit=fetch_limit)

        if not entries:
            console.print("[dim]No audit entries found.[/dim]")
            return

        entries, has_prev, has_next, first_num, last_num = _paginate(entries, page, per_page)
        if not entries:
            console.print(f"[dim]No entries on page {page}.[/dim]")
            return

        _rl_action_labels = {
            "rl_policy_set": "set",
            "rl_policy_updated": "update",
            "rl_reset": "reset",
            "rl_policy_deleted": "delete",
        }
        _rl_action_colours = {
            "rl_policy_set": "green",
            "rl_policy_updated": "yellow",
            "rl_reset": "cyan",
            "rl_policy_deleted": "red",
        }

        table = Table(box=box.ROUNDED, show_header=True, header_style="bold")
        table.add_column("Timestamp", style="dim")
        table.add_column("Route", style="cyan")
        table.add_column("Action")
        table.add_column("Actor")
        table.add_column("Platform")
        table.add_column("Status")
        table.add_column("Reason")

        for e in entries:
            ts = e.get("timestamp", "")
            try:
                ts = datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
            action = e["action"]
            if action in _rl_action_labels:
                label = _rl_action_labels[action]
                colour = _rl_action_colours[action]
                status_cell = f"[{colour}]{label}[/{colour}]"
            elif e.get("previous_status") and e.get("new_status"):
                old_c = _status_colour(e["previous_status"])
                new_c = _status_colour(e["new_status"])
                status_cell = (
                    f"[{old_c}]{e['previous_status']}[/{old_c}]"
                    f" → "
                    f"[{new_c}]{e['new_status']}[/{new_c}]"
                )
            else:
                status_cell = "—"
            table.add_row(
                ts,
                e["path"],
                action,
                e.get("actor", "—"),
                e.get("platform", "—"),
                status_cell,
                e.get("reason") or "—",
            )

        console.print(table)
        _print_page_footer(page, per_page, first_num, last_num, has_prev, has_next)

    _run(_run_log)


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

    async def _run_gs() -> None:
        cfg = await make_client().global_status()
        state_str = "[green]OFF[/green]"
        if cfg.get("enabled"):
            state_str = "[yellow]ON[/yellow]"
        console.print(f"\n  Global maintenance: {state_str}")
        if cfg.get("enabled"):
            console.print(f"  Reason            : {cfg.get('reason') or '—'}")
            fa = cfg.get("include_force_active", False)
            fa_colour = "red" if fa else "green"
            fa_text = "yes" if fa else "no"
            console.print(f"  Include @force_active: [{fa_colour}]{fa_text}[/{fa_colour}]")
            exempts = cfg.get("exempt_paths") or []
            if exempts:
                console.print("  Exempt paths      :")
                for p in exempts:
                    console.print(f"    • {p}")
            else:
                console.print("  Exempt paths      : (none)")
        console.print()

    _run(_run_gs)


@global_app.command("enable")
def global_enable(
    reason: str = typer.Option("", "--reason", "-r", help="Reason shown in 503 responses."),
    exempt: list[str] | None = typer.Option(
        None,
        "--exempt",
        "-e",
        help="Route key to exempt (repeat for multiple).",
    ),
    include_force_active: bool = typer.Option(
        False,
        "--include-force-active/--no-include-force-active",
        help="Also block @force_active routes.",
    ),
) -> None:
    """Enable global maintenance mode — all non-exempt routes return 503."""

    async def _run_ge() -> None:
        cfg = await make_client().global_enable(
            reason=reason,
            exempt_paths=list(exempt) if exempt else [],
            include_force_active=include_force_active,
        )
        console.print("[yellow]⚠[/yellow]  Global maintenance [yellow]ENABLED[/yellow]")
        if cfg.get("reason"):
            console.print(f"   Reason: {cfg['reason']}")
        if cfg.get("exempt_paths"):
            console.print(f"   Exempt: {', '.join(cfg['exempt_paths'])}")
        if cfg.get("include_force_active"):
            console.print("   [red]@force_active routes are also blocked.[/red]")

    _run(_run_ge)


@global_app.command("disable")
def global_disable() -> None:
    """Disable global maintenance mode, restoring normal per-route state."""

    async def _run_gd() -> None:
        await make_client().global_disable()
        console.print("[green]✓[/green]  Global maintenance [green]DISABLED[/green]")

    _run(_run_gd)


@global_app.command("exempt-add")
def global_exempt_add(
    route: str = typer.Argument(..., help="Route key: /path or METHOD:/path"),
) -> None:
    """Add a route to the global maintenance exempt list."""

    async def _run_ea() -> None:
        client = make_client()
        cfg = await client.global_status()
        key = route if route.startswith("/") or ":" in route else f"/{route}"
        current = cfg.get("exempt_paths") or []
        if key not in current:
            updated_cfg = await client.global_enable(
                reason=cfg.get("reason", ""),
                exempt_paths=current + [key],
                include_force_active=cfg.get("include_force_active", False),
            )
            console.print(
                f"[green]✓[/green] Added [cyan]{key}[/cyan] to exempt list "
                f"({len(updated_cfg.get('exempt_paths', []))} total)"
            )
        else:
            console.print(f"[dim]{key} is already in the exempt list.[/dim]")

    _run(_run_ea)


@global_app.command("exempt-remove")
def global_exempt_remove(
    route: str = typer.Argument(..., help="Route key: /path or METHOD:/path"),
) -> None:
    """Remove a route from the global maintenance exempt list."""

    async def _run_er() -> None:
        client = make_client()
        cfg = await client.global_status()
        key = route if route.startswith("/") or ":" in route else f"/{route}"
        current = cfg.get("exempt_paths") or []
        if key in current:
            remaining = [p for p in current if p != key]
            await client.global_enable(
                reason=cfg.get("reason", ""),
                exempt_paths=remaining,
                include_force_active=cfg.get("include_force_active", False),
            )
            console.print(f"[green]✓[/green] Removed [cyan]{key}[/cyan] from exempt list")
        else:
            console.print(f"[dim]{key} was not in the exempt list.[/dim]")

    _run(_run_er)


# ---------------------------------------------------------------------------
# Env gate command group  (shield env ...)
# ---------------------------------------------------------------------------

env_app = typer.Typer(
    name="env",
    help="Manage environment-gating for routes.",
    no_args_is_help=True,
)
cli.add_typer(env_app, name="env")


@env_app.command("set")
def env_set(
    route: str = typer.Argument(..., help="Route: /path or METHOD:/path"),
    envs: list[str] = typer.Argument(
        ..., help="Environment names to allow (e.g. dev staging prod)."
    ),
    reason: str = typer.Option("", "--reason", "-r", help="Optional note for the audit log."),
) -> None:
    """Restrict a route to specific environments.

    Only requests where the engine's current_env matches one of the given
    environments will be allowed through.  All other environments receive 403.

    Examples:

    \b
      shield env set /api/debug dev
      shield env set /api/internal dev staging
    """

    async def _run_env_set() -> None:
        key = _parse_route(route)
        client = make_client()
        try:
            keys_to_apply = [key]
            state = await client.env_gate(key, list(envs))
            states = [state]
        except ShieldClientError as exc:
            if not exc.ambiguous_matches:
                raise
            keys_to_apply = _confirm_ambiguous(exc.ambiguous_matches, "env")
            states = [await client.env_gate(k, list(envs)) for k in keys_to_apply]

        for k, state in zip(keys_to_apply, states):
            allowed = ", ".join(state.get("allowed_envs") or [])
            console.print(
                f"[blue]🔒[/blue] {k} → [blue]{state['status'].upper()}[/blue]"
                + (f"  [dim]({allowed})[/dim]" if allowed else "")
            )
        if reason:
            console.print(f"  Reason: {reason}")

    _run(_run_env_set)


@env_app.command("clear")
def env_clear(
    route: str = typer.Argument(..., help="Route: /path or METHOD:/path"),
    reason: str = typer.Option("", "--reason", "-r", help="Optional note for the audit log."),
) -> None:
    """Remove env-gating from a route, restoring it to active status.

    Examples:

    \b
      shield env clear /api/debug
    """

    async def _run_env_clear() -> None:
        key = _parse_route(route)
        client = make_client()
        try:
            keys_to_apply = [key]
            state = await client.enable(key, reason=reason)
            states = [state]
        except ShieldClientError as exc:
            if not exc.ambiguous_matches:
                raise
            keys_to_apply = _confirm_ambiguous(exc.ambiguous_matches, "env clear")
            states = [await client.enable(k, reason=reason) for k in keys_to_apply]

        for k, state in zip(keys_to_apply, states):
            console.print(
                f"[green]✓[/green] {k} → [green]{state['status'].upper()}[/green]"
                "  [dim](env gate removed)[/dim]"
            )
        if reason:
            console.print(f"  Reason: {reason}")

    _run(_run_env_clear)


# ---------------------------------------------------------------------------
# Rate limits command group  (shield rate-limits ...)
# ---------------------------------------------------------------------------

rl_app = typer.Typer(
    name="rate-limits",
    help="Inspect rate limit policies and recent blocked requests.",
    no_args_is_help=True,
)
cli.add_typer(rl_app, name="rate-limits")
cli.add_typer(rl_app, name="rl")


@rl_app.command("list")
def rl_list(
    page: int = typer.Option(1, "--page", "-p", help="Page number."),
    per_page: int = typer.Option(_DEFAULT_PER_PAGE, "--per-page", help="Rows per page."),
) -> None:
    """List all registered rate limit policies."""

    async def _run_rl_list() -> None:
        all_policies = sorted(await make_client().list_rate_limits(), key=lambda x: x["path"])
        if not all_policies:
            console.print("[dim]No rate limit policies registered.[/dim]")
            return

        policies, has_prev, has_next, first_num, last_num = _paginate(all_policies, page, per_page)
        if not policies:
            console.print(f"[dim]No entries on page {page}.[/dim]")
            return

        table = Table(box=box.ROUNDED, show_header=True, header_style="bold")
        table.add_column("Route", style="cyan")
        table.add_column("Method", style="dim")
        table.add_column("Limit")
        table.add_column("Algorithm", style="dim")
        table.add_column("Key Strategy", style="dim")
        table.add_column("Tiers", justify="right")

        for p in policies:
            tiers = len(p.get("tiers") or [])
            table.add_row(
                p["path"],
                p.get("method", "*"),
                f"[magenta]{p['limit']}[/magenta]",
                p.get("algorithm", "—"),
                p.get("key_strategy", "—"),
                str(tiers) if tiers else "—",
            )

        console.print(table)
        if has_prev or has_next:
            _print_page_footer(page, per_page, first_num, last_num, has_prev, has_next)

    _run(_run_rl_list)


@rl_app.command("hits")
def rl_hits(
    route: str | None = typer.Option(None, "--route", "-r", help="Filter by route path."),
    page: int = typer.Option(1, "--page", "-p", help="Page number."),
    per_page: int = typer.Option(_DEFAULT_PER_PAGE, "--per-page", help="Rows per page."),
) -> None:
    """Show recent rate-limited (blocked) requests."""

    async def _run_rl_hits() -> None:
        fetch_limit = page * per_page + 1
        all_hits = await make_client().rate_limit_hits(route=route, limit=fetch_limit)
        if not all_hits:
            console.print("[dim]No rate limit hits found.[/dim]")
            return

        hits, has_prev, has_next, first_num, last_num = _paginate(all_hits, page, per_page)
        if not hits:
            console.print(f"[dim]No entries on page {page}.[/dim]")
            return

        table = Table(box=box.ROUNDED, show_header=True, header_style="bold")
        table.add_column("Time", style="dim")
        table.add_column("Path", style="cyan")
        table.add_column("Limit", style="red")

        for h in hits:
            ts = h.get("timestamp", "")
            try:
                ts = datetime.fromisoformat(ts).strftime("%H:%M:%S")
            except Exception:
                pass
            method = h.get("method", "")
            path = h.get("path", "—")
            path_cell = f"{method} {path}" if method else path
            table.add_row(
                ts,
                path_cell,
                str(h.get("limit", "—")),
            )

        console.print(table)
        _print_page_footer(page, per_page, first_num, last_num, has_prev, has_next)

    _run(_run_rl_hits)


@rl_app.command("reset")
def rl_reset(
    route: str = typer.Argument(..., help="Route: /path or METHOD:/path to reset counters for."),
) -> None:
    """Reset rate limit counters for a route.

    Pass METHOD:/path to reset only a specific method, or /path to reset
    all methods.  Examples:

    \b
      shield rl reset GET:/api/items
      shield rl reset /api/items        (resets all methods)
    """
    import base64

    key = _parse_route(route)
    if ":" in key and not key.startswith("/"):
        reset_method, _, reset_path = key.partition(":")
    else:
        reset_method, reset_path = None, key

    async def _run_rl_reset() -> None:
        client = make_client()
        path_key = base64.urlsafe_b64encode(reset_path.encode()).decode().rstrip("=")
        result = await client.reset_rate_limit(path_key, method=reset_method)
        scope = (
            f"[cyan]{reset_method} {reset_path}[/cyan]"
            if reset_method
            else f"[cyan]{reset_path}[/cyan] (all methods)"
        )
        if result.get("ok"):
            console.print(f"[green]✓[/green] Rate limit counters reset for {scope}")
        else:
            console.print(f"[yellow]?[/yellow] {result}")

    _run(_run_rl_reset)


@rl_app.command("set")
def rl_set(
    route: str = typer.Argument(..., help="Route: /path or METHOD:/path, e.g. GET:/api/items"),
    limit: str = typer.Argument(..., help="Rate limit string, e.g. 100/minute"),
    algorithm: str | None = typer.Option(
        None,
        "--algorithm",
        "-a",
        help="Algorithm: fixed_window, sliding_window, moving_window, token_bucket.",
    ),
    key_strategy: str | None = typer.Option(
        None,
        "--key",
        "-k",
        help="Key strategy: ip, user, api_key, global, custom.",
    ),
    burst: int = typer.Option(0, "--burst", "-b", help="Burst allowance (extra requests)."),
) -> None:
    """Set or update a rate limit policy for a route.

    The policy is persisted to the backend so it survives restarts and
    is visible to all instances.  Examples:

    \b
      shield rl set GET:/api/items 100/minute
      shield rl set POST:/api/pay 10/second --algorithm fixed_window --key ip
    """
    key = _parse_route(route)
    if ":" in key and not key.startswith("/"):
        parsed_method, _, parsed_path = key.partition(":")
    else:
        parsed_method, parsed_path = "GET", key

    async def _run_rl_set() -> None:
        client = make_client()
        result = await client.set_rate_limit_policy(
            path=parsed_path,
            method=parsed_method,
            limit=limit,
            algorithm=algorithm,
            key_strategy=key_strategy,
            burst=burst,
        )
        algo = result.get("algorithm", "")
        key_strat = result.get("key_strategy", "ip")
        console.print(
            f"[green]✓[/green] Rate limit set: "
            f"[cyan]{parsed_method.upper()} {result.get('path')}[/cyan] "
            f"→ [bold]{result.get('limit')}[/bold] ({algo}, key={key_strat})"
        )

    _run(_run_rl_set)


@rl_app.command("delete")
def rl_delete(
    route: str = typer.Argument(..., help="Route: /path or METHOD:/path, e.g. GET:/api/items"),
) -> None:
    """Delete a persisted rate limit policy for a route.

    Removes the policy from the backend so it will not be re-applied after
    a restart.  Running in-process counters are unaffected — use
    ``rl reset`` to clear them.  Examples:

    \b
      shield rl delete GET:/api/items
      shield rl delete /api/items       (defaults to GET)
    """
    key = _parse_route(route)
    if ":" in key and not key.startswith("/"):
        del_method, _, del_path = key.partition(":")
    else:
        del_method, del_path = "GET", key

    async def _run_rl_delete() -> None:
        client = make_client()
        result = await client.delete_rate_limit_policy(path=del_path, method=del_method)
        if result.get("ok"):
            console.print(
                f"[green]✓[/green] Rate limit policy deleted: "
                f"[cyan]{del_method.upper()} {del_path}[/cyan]"
            )
        else:
            console.print(f"[yellow]?[/yellow] {result}")

    _run(_run_rl_delete)


# ---------------------------------------------------------------------------
# Global rate limit command group  (shield global-rate-limit ...)
# ---------------------------------------------------------------------------

grl_app = typer.Typer(
    name="global-rate-limit",
    help="Manage the global rate limit policy applied to all routes.",
    no_args_is_help=True,
)
cli.add_typer(grl_app, name="global-rate-limit")
cli.add_typer(grl_app, name="grl")


@grl_app.command("get")
def grl_get() -> None:
    """Show the current global rate limit policy."""

    async def _run_grl_get() -> None:
        result = await make_client().get_global_rate_limit()
        policy = result.get("policy")
        if not policy:
            console.print("[dim]No global rate limit policy configured.[/dim]")
            return

        table = Table(box=box.ROUNDED, show_header=True, header_style="bold")
        table.add_column("Field", style="dim")
        table.add_column("Value")

        table.add_row("Limit", f"[magenta]{policy.get('limit', '—')}[/magenta]")
        table.add_row("Algorithm", policy.get("algorithm", "—"))
        table.add_row("Key Strategy", policy.get("key_strategy", "—"))
        table.add_row("Burst", str(policy.get("burst", 0)))
        table.add_row("Enabled", "[green]yes[/green]" if policy.get("enabled") else "[red]no[/red]")
        exempt = policy.get("exempt_routes") or []
        table.add_row("Exempt Routes", "\n".join(exempt) if exempt else "[dim](none)[/dim]")
        console.print(table)

    _run(_run_grl_get)


@grl_app.command("set")
def grl_set(
    limit: str = typer.Argument(..., help="Rate limit string, e.g. 1000/minute"),
    algorithm: str | None = typer.Option(
        None,
        "--algorithm",
        "-a",
        help="Algorithm: fixed_window, sliding_window, moving_window, token_bucket.",
    ),
    key_strategy: str | None = typer.Option(
        None,
        "--key",
        "-k",
        help="Key strategy: ip, user, api_key, global, custom.",
    ),
    burst: int = typer.Option(0, "--burst", "-b", help="Burst allowance (extra requests)."),
    exempt: list[str] | None = typer.Option(
        None,
        "--exempt",
        "-e",
        help=(
            "Route to exempt from the global limit.  Repeat for multiple routes.  "
            "Use /path to exempt all methods, or METHOD:/path for a specific method."
        ),
    ),
) -> None:
    """Set or update the global rate limit policy.

    The policy applies to every route that is not explicitly exempted.
    Persisted to the backend so it survives restarts.  Examples:

    \b
      shield grl set 1000/minute
      shield grl set 500/minute --key ip --exempt /health --exempt GET:/api/internal
      shield grl set 200/hour --algorithm sliding_window --burst 20
    """

    async def _run_grl_set() -> None:
        result = await make_client().set_global_rate_limit(
            limit=limit,
            algorithm=algorithm,
            key_strategy=key_strategy,
            burst=burst,
            exempt_routes=list(exempt) if exempt else [],
        )
        key_strat = result.get("key_strategy", "ip")
        algo = result.get("algorithm", "")
        console.print(
            f"[green]✓[/green] Global rate limit set: "
            f"[bold]{result.get('limit')}[/bold] ({algo}, key={key_strat})"
        )
        exempt_list = result.get("exempt_routes") or []
        if exempt_list:
            console.print(f"  Exempt routes: [dim]{', '.join(exempt_list)}[/dim]")

    _run(_run_grl_set)


@grl_app.command("delete")
def grl_delete() -> None:
    """Remove the global rate limit policy.

    Clears the policy from the backend.  In-process counters are not
    affected — use ``grl reset`` to clear them too.
    """

    async def _run_grl_delete() -> None:
        result = await make_client().delete_global_rate_limit()
        if result.get("ok"):
            console.print("[green]✓[/green] Global rate limit policy removed.")
        else:
            console.print(f"[yellow]?[/yellow] {result}")

    _run(_run_grl_delete)


@grl_app.command("reset")
def grl_reset() -> None:
    """Reset global rate limit counters.

    Clears all counters so the limit starts fresh.  The policy itself
    is not removed — use ``grl delete`` for that.
    """

    async def _run_grl_reset() -> None:
        result = await make_client().reset_global_rate_limit()
        if result.get("ok"):
            console.print("[green]✓[/green] Global rate limit counters reset.")
        else:
            console.print(f"[yellow]?[/yellow] {result}")

    _run(_run_grl_reset)


@grl_app.command("enable")
def grl_enable() -> None:
    """Resume a paused global rate limit policy."""

    async def _run_grl_enable() -> None:
        result = await make_client().enable_global_rate_limit()
        if result.get("ok"):
            console.print("[green]✓[/green] Global rate limit resumed.")
        else:
            console.print(f"[yellow]?[/yellow] {result}")

    _run(_run_grl_enable)


@grl_app.command("disable")
def grl_disable() -> None:
    """Pause the global rate limit policy without removing it."""

    async def _run_grl_disable() -> None:
        result = await make_client().disable_global_rate_limit()
        if result.get("ok"):
            console.print("[green]✓[/green] Global rate limit paused.")
        else:
            console.print(f"[yellow]?[/yellow] {result}")

    _run(_run_grl_disable)


# ---------------------------------------------------------------------------
# Per-service rate limit command group  (shield service-rate-limit ...)
# ---------------------------------------------------------------------------

srl_app = typer.Typer(
    name="service-rate-limit",
    help="Manage per-service rate limit policies applied to all routes of a service.",
    no_args_is_help=True,
)
cli.add_typer(srl_app, name="service-rate-limit")
cli.add_typer(srl_app, name="srl")


@srl_app.command("get")
def srl_get(
    service: str = typer.Argument(..., help="Service name, e.g. payments-service"),
) -> None:
    """Show the current per-service rate limit policy."""

    async def _run_srl_get() -> None:
        result = await make_client().get_service_rate_limit(service)
        policy = result.get("policy")
        if not policy:
            console.print(f"[dim]No rate limit policy configured for {service!r}.[/dim]")
            return

        table = Table(box=box.ROUNDED, show_header=True, header_style="bold")
        table.add_column("Field", style="dim")
        table.add_column("Value")

        table.add_row("Service", f"[cyan]{service}[/cyan]")
        table.add_row("Limit", f"[magenta]{policy.get('limit', '—')}[/magenta]")
        table.add_row("Algorithm", policy.get("algorithm", "—"))
        table.add_row("Key Strategy", policy.get("key_strategy", "—"))
        table.add_row("Burst", str(policy.get("burst", 0)))
        table.add_row("Enabled", "[green]yes[/green]" if policy.get("enabled") else "[red]no[/red]")
        exempt = policy.get("exempt_routes") or []
        table.add_row("Exempt Routes", "\n".join(exempt) if exempt else "[dim](none)[/dim]")
        console.print(table)

    _run(_run_srl_get)


@srl_app.command("set")
def srl_set(
    service: str = typer.Argument(..., help="Service name, e.g. payments-service"),
    limit: str = typer.Argument(..., help="Rate limit string, e.g. 1000/minute"),
    algorithm: str | None = typer.Option(
        None,
        "--algorithm",
        "-a",
        help="Algorithm: fixed_window, sliding_window, moving_window, token_bucket.",
    ),
    key_strategy: str | None = typer.Option(
        None,
        "--key",
        "-k",
        help="Key strategy: ip, user, api_key, global, custom.",
    ),
    burst: int = typer.Option(0, "--burst", "-b", help="Burst allowance (extra requests)."),
    exempt: list[str] | None = typer.Option(
        None,
        "--exempt",
        "-e",
        help=(
            "Route to exempt from the service limit.  Repeat for multiple routes.  "
            "Use /path to exempt all methods, or METHOD:/path for a specific method."
        ),
    ),
) -> None:
    """Set or update the per-service rate limit policy.

    The policy applies to every route of SERVICE that is not explicitly exempted.
    Persisted to the backend so it survives restarts.  Examples:

    \b
      shield srl set payments-service 1000/minute
      shield srl set orders-service 500/minute --key ip --exempt /health
      shield srl set auth-service 200/hour --algorithm sliding_window --burst 20
    """

    async def _run_srl_set() -> None:
        result = await make_client().set_service_rate_limit(
            service,
            limit=limit,
            algorithm=algorithm,
            key_strategy=key_strategy,
            burst=burst,
            exempt_routes=list(exempt) if exempt else [],
        )
        key_strat = result.get("key_strategy", "ip")
        algo = result.get("algorithm", "")
        console.print(
            f"[green]✓[/green] Service rate limit set for [cyan]{service}[/cyan]: "
            f"[bold]{result.get('limit')}[/bold] ({algo}, key={key_strat})"
        )
        exempt_list = result.get("exempt_routes") or []
        if exempt_list:
            console.print(f"  Exempt routes: [dim]{', '.join(exempt_list)}[/dim]")

    _run(_run_srl_set)


@srl_app.command("delete")
def srl_delete(
    service: str = typer.Argument(..., help="Service name, e.g. payments-service"),
) -> None:
    """Remove the per-service rate limit policy.

    Clears the policy from the backend.  In-process counters are not
    affected — use ``srl reset`` to clear them too.
    """

    async def _run_srl_delete() -> None:
        result = await make_client().delete_service_rate_limit(service)
        if result.get("ok"):
            console.print(
                f"[green]✓[/green] Service rate limit policy removed for [cyan]{service}[/cyan]."
            )
        else:
            console.print(f"[yellow]?[/yellow] {result}")

    _run(_run_srl_delete)


@srl_app.command("reset")
def srl_reset(
    service: str = typer.Argument(..., help="Service name, e.g. payments-service"),
) -> None:
    """Reset per-service rate limit counters.

    Clears all counters so the limit starts fresh.  The policy itself
    is not removed — use ``srl delete`` for that.
    """

    async def _run_srl_reset() -> None:
        result = await make_client().reset_service_rate_limit(service)
        if result.get("ok"):
            console.print(f"[green]✓[/green] Rate limit counters reset for [cyan]{service}[/cyan].")
        else:
            console.print(f"[yellow]?[/yellow] {result}")

    _run(_run_srl_reset)


@srl_app.command("enable")
def srl_enable(
    service: str = typer.Argument(..., help="Service name, e.g. payments-service"),
) -> None:
    """Resume a paused per-service rate limit policy."""

    async def _run_srl_enable() -> None:
        result = await make_client().enable_service_rate_limit(service)
        if result.get("ok"):
            console.print(
                f"[green]✓[/green] Service rate limit resumed for [cyan]{service}[/cyan]."
            )
        else:
            console.print(f"[yellow]?[/yellow] {result}")

    _run(_run_srl_enable)


@srl_app.command("disable")
def srl_disable(
    service: str = typer.Argument(..., help="Service name, e.g. payments-service"),
) -> None:
    """Pause the per-service rate limit policy without removing it."""

    async def _run_srl_disable() -> None:
        result = await make_client().disable_service_rate_limit(service)
        if result.get("ok"):
            console.print(f"[green]✓[/green] Service rate limit paused for [cyan]{service}[/cyan].")
        else:
            console.print(f"[yellow]?[/yellow] {result}")

    _run(_run_srl_disable)


# ---------------------------------------------------------------------------
# Per-service maintenance command group  (shield sm ...)
# ---------------------------------------------------------------------------

sm_app = typer.Typer(
    name="service-maintenance",
    help="Manage per-service maintenance mode (blocks all routes of one service).",
    no_args_is_help=True,
)
cli.add_typer(sm_app, name="service-maintenance")
cli.add_typer(sm_app, name="sm")


@sm_app.command("status")
def sm_status(
    service: str = typer.Argument(..., help="Service name, e.g. payments-service"),
) -> None:
    """Show the current maintenance configuration for a service."""

    async def _run_sm_status() -> None:
        cfg = await make_client().service_maintenance_status(service)
        state_str = "[green]OFF[/green]"
        if cfg.get("enabled"):
            state_str = "[yellow]ON[/yellow]"
        console.print(f"\n  Service maintenance ({service}): {state_str}")
        if cfg.get("enabled"):
            console.print(f"  Reason               : {cfg.get('reason') or '—'}")
            fa = cfg.get("include_force_active", False)
            fa_colour = "red" if fa else "green"
            fa_text = "yes" if fa else "no"
            console.print(f"  Include @force_active: [{fa_colour}]{fa_text}[/{fa_colour}]")
            exempts = cfg.get("exempt_paths") or []
            if exempts:
                console.print("  Exempt paths         :")
                for p in exempts:
                    console.print(f"    • {p}")
            else:
                console.print("  Exempt paths         : (none)")
        console.print()

    _run(_run_sm_status)


@sm_app.command("enable")
def sm_enable(
    service: str = typer.Argument(..., help="Service name, e.g. payments-service"),
    reason: str = typer.Option("", "--reason", "-r", help="Reason shown in 503 responses."),
    exempt: list[str] | None = typer.Option(
        None,
        "--exempt",
        "-e",
        help="Route to exempt (repeat for multiple). Use /path or METHOD:/path.",
    ),
    include_force_active: bool = typer.Option(
        False,
        "--include-force-active/--no-include-force-active",
        help="Also block @force_active routes.",
    ),
) -> None:
    """Enable maintenance mode for all routes of a service.

    All routes belonging to SERVICE return 503 until maintenance is disabled.
    Exempt paths bypass the block and respond normally.

    \b
      shield sm enable payments-service --reason "DB migration"
      shield sm enable payments-service --reason "Upgrade" --exempt /health
      shield sm enable orders-service --include-force-active
    """

    async def _run_sm_enable() -> None:
        cfg = await make_client().service_maintenance_enable(
            service,
            reason=reason,
            exempt_paths=list(exempt) if exempt else [],
            include_force_active=include_force_active,
        )
        console.print(
            f"[yellow]⚠[/yellow]  Service maintenance [yellow]ENABLED[/yellow]"
            f" for [cyan]{service}[/cyan]"
        )
        if cfg.get("reason"):
            console.print(f"   Reason: {cfg['reason']}")
        if cfg.get("exempt_paths"):
            console.print(f"   Exempt: {', '.join(cfg['exempt_paths'])}")
        if cfg.get("include_force_active"):
            console.print("   [red]@force_active routes are also blocked.[/red]")

    _run(_run_sm_enable)


@sm_app.command("disable")
def sm_disable(
    service: str = typer.Argument(..., help="Service name, e.g. payments-service"),
) -> None:
    """Disable service maintenance mode, restoring normal per-route state."""

    async def _run_sm_disable() -> None:
        await make_client().service_maintenance_disable(service)
        console.print(
            f"[green]✓[/green]  Service maintenance [green]DISABLED[/green]"
            f" for [cyan]{service}[/cyan]"
        )

    _run(_run_sm_disable)


# ---------------------------------------------------------------------------
# Feature flags command group  (shield flags ...)
# ---------------------------------------------------------------------------

_FLAG_TYPE_COLOURS = {
    "boolean": "green",
    "string": "cyan",
    "integer": "blue",
    "float": "blue",
    "json": "magenta",
}

flags_app = typer.Typer(
    name="flags",
    help="Manage feature flags.",
    no_args_is_help=True,
)
cli.add_typer(flags_app, name="flags")


def _flag_status_colour(enabled: bool) -> str:
    return "green" if enabled else "dim"


def _print_flags_table(flags: list[dict[str, Any]]) -> None:
    tbl = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False)
    tbl.add_column("Key", style="bold cyan", no_wrap=True)
    tbl.add_column("Type", style="white")
    tbl.add_column("Status", style="white")
    tbl.add_column("Variations", style="dim")
    tbl.add_column("Fallthrough", style="dim")
    for f in flags:
        enabled = f.get("enabled", True)
        status_text = "[green]enabled[/green]" if enabled else "[dim]disabled[/dim]"
        ftype = f.get("type", "")
        colour = _FLAG_TYPE_COLOURS.get(ftype, "white")
        variations = ", ".join(v["name"] for v in f.get("variations", []))
        fallthrough = f.get("fallthrough", "")
        if isinstance(fallthrough, list):
            fallthrough = "rollout"
        tbl.add_row(
            f.get("key", ""),
            f"[{colour}]{ftype}[/{colour}]",
            status_text,
            variations,
            str(fallthrough),
        )
    console.print(tbl)


@flags_app.command("list")
def flags_list(
    type: str = typer.Option("", "--type", "-t", help="Filter by flag type (boolean, string, …)"),
    enabled: str = typer.Option("", "--status", "-s", help="Filter by status: enabled or disabled"),
) -> None:
    """List all feature flags."""

    async def _run_flags_list() -> None:
        flags = await make_client().list_flags()
        if type:
            flags = [f for f in flags if f.get("type") == type]
        if enabled == "enabled":
            flags = [f for f in flags if f.get("enabled", True)]
        elif enabled == "disabled":
            flags = [f for f in flags if not f.get("enabled", True)]
        if not flags:
            console.print("[dim]No flags found.[/dim]")
            return
        _print_flags_table(flags)
        console.print(f"[dim]{len(flags)} flag(s)[/dim]")

    _run(_run_flags_list)


@flags_app.command("get")
def flags_get(key: str = typer.Argument(..., help="Flag key")) -> None:
    """Show details for a single feature flag."""

    async def _run_flags_get() -> None:
        flag = await make_client().get_flag(key)
        console.print(f"[bold cyan]{flag['key']}[/bold cyan]  [dim]{flag.get('name', '')}[/dim]")
        ftype = flag.get("type", "")
        colour = _FLAG_TYPE_COLOURS.get(ftype, "white")
        enabled = flag.get("enabled", True)
        status_text = "[green]enabled[/green]" if enabled else "[dim]disabled[/dim]"
        console.print(f"  Type:    [{colour}]{ftype}[/{colour}]")
        console.print(f"  Status:  {status_text}")
        console.print(f"  Off variation: [dim]{flag.get('off_variation', '')}[/dim]")
        fallthrough = flag.get("fallthrough", "")
        if isinstance(fallthrough, list):
            parts = [f"{rv['variation']}:{rv['weight'] // 1000}%" for rv in fallthrough]
            console.print(f"  Fallthrough: [dim]{', '.join(parts)}[/dim]")
        else:
            console.print(f"  Fallthrough: [dim]{fallthrough}[/dim]")
        # Variations
        console.print("  Variations:")
        for v in flag.get("variations", []):
            console.print(f"    • [bold]{v['name']}[/bold] = {v['value']!r}")
        # Rules
        rules = flag.get("rules") or []
        if rules:
            console.print(f"  Rules: [dim]{len(rules)} targeting rule(s)[/dim]")
        # Prerequisites
        prereqs = flag.get("prerequisites") or []
        if prereqs:
            console.print("  Prerequisites:")
            for p in prereqs:
                console.print(
                    f"    • [cyan]{p['flag_key']}[/cyan] must be [bold]{p['variation']}[/bold]"
                )

    _run(_run_flags_get)


@flags_app.command("create")
def flags_create(
    key: str = typer.Argument(..., help="Unique flag key (e.g. new_checkout)"),
    name: str = typer.Option(..., "--name", "-n", help="Human-readable name"),
    type: str = typer.Option(
        "boolean", "--type", "-t", help="Flag type: boolean, string, integer, float, json"
    ),
    description: str = typer.Option("", "--description", "-d", help="Optional description"),
) -> None:
    """Create a new boolean feature flag with on/off variations.

    For other types or advanced configuration, use the dashboard or the API
    directly.  The flag is created enabled with fallthrough=off.

    \b
      shield flags create new_checkout --name "New Checkout Flow"
      shield flags create dark_mode --name "Dark Mode" --type boolean
    """

    async def _run_flags_create() -> None:
        flag_type = type.lower()
        # Build default on/off variations based on type.
        if flag_type == "boolean":
            variations = [{"name": "on", "value": True}, {"name": "off", "value": False}]
            off_variation = "off"
            fallthrough = "off"
        elif flag_type == "string":
            variations = [
                {"name": "control", "value": "control"},
                {"name": "treatment", "value": "treatment"},
            ]
            off_variation = "control"
            fallthrough = "control"
        elif flag_type in ("integer", "float"):
            variations = [{"name": "off", "value": 0}, {"name": "on", "value": 1}]
            off_variation = "off"
            fallthrough = "off"
        elif flag_type == "json":
            variations = [{"name": "off", "value": {}}, {"name": "on", "value": {}}]
            off_variation = "off"
            fallthrough = "off"
        else:
            err_console.print(
                f"[red]Error:[/red] Unknown type {type!r}. "
                "Use boolean, string, integer, float, or json."
            )
            raise typer.Exit(code=1)

        flag_data = {
            "key": key,
            "name": name,
            "type": flag_type,
            "description": description,
            "variations": variations,
            "off_variation": off_variation,
            "fallthrough": fallthrough,
            "enabled": True,
        }
        result = await make_client().create_flag(flag_data)
        console.print(f"[green]✓[/green] Flag [bold cyan]{result['key']}[/bold cyan] created.")

    _run(_run_flags_create)


@flags_app.command("enable")
def flags_enable(key: str = typer.Argument(..., help="Flag key")) -> None:
    """Enable a feature flag."""

    async def _run_flags_enable() -> None:
        result = await make_client().enable_flag(key)
        console.print(f"[green]✓[/green] Flag [bold cyan]{result['key']}[/bold cyan] enabled.")

    _run(_run_flags_enable)


@flags_app.command("disable")
def flags_disable(key: str = typer.Argument(..., help="Flag key")) -> None:
    """Disable a feature flag (serves the off variation to all users)."""

    async def _run_flags_disable() -> None:
        result = await make_client().disable_flag(key)
        console.print(f"[dim]✓ Flag {result['key']} disabled.[/dim]")

    _run(_run_flags_disable)


@flags_app.command("delete")
def flags_delete(
    key: str = typer.Argument(..., help="Flag key"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Permanently delete a feature flag."""
    if not yes:
        typer.confirm(f"Delete flag '{key}'? This cannot be undone.", abort=True)

    async def _run_flags_delete() -> None:
        result = await make_client().delete_flag(key)
        console.print(f"[green]✓[/green] Flag [bold]{result['deleted']}[/bold] deleted.")

    _run(_run_flags_delete)


@flags_app.command("eval")
def flags_eval(
    key: str = typer.Argument(..., help="Flag key"),
    ctx_key: str = typer.Option("anonymous", "--key", "-k", help="Context key (user ID)"),
    kind: str = typer.Option("user", "--kind", help="Context kind"),
    attr: list[str] = typer.Option([], "--attr", "-a", help="Attribute as key=value (repeatable)"),
) -> None:
    """Evaluate a feature flag for a given context (debug tool).

    \b
      shield flags eval new_checkout --key user_123 --attr role=admin --attr plan=pro
    """

    async def _run_flags_eval() -> None:
        attributes: dict[str, str] = {}
        for a in attr:
            if "=" not in a:
                err_console.print(f"[red]Error:[/red] Attribute must be key=value, got: {a!r}")
                raise typer.Exit(code=1)
            k, _, v = a.partition("=")
            attributes[k.strip()] = v.strip()

        context = {"key": ctx_key, "kind": kind, "attributes": attributes}
        result = await make_client().evaluate_flag(key, context)

        value = result.get("value")
        variation = result.get("variation", "")
        reason = result.get("reason", "")
        rule_id = result.get("rule_id")

        tbl = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, show_header=False)
        tbl.add_column("Field", style="dim", no_wrap=True)
        tbl.add_column("Value", style="bold")
        tbl.add_row("value", str(value))
        tbl.add_row("variation", variation or "—")
        tbl.add_row("reason", reason)
        if rule_id:
            tbl.add_row("rule_id", rule_id)
        prereq = result.get("prerequisite_key")
        if prereq:
            tbl.add_row("prerequisite_key", prereq)
        err_msg = result.get("error_message")
        if err_msg:
            tbl.add_row("error", f"[red]{err_msg}[/red]")
        console.print(tbl)

    _run(_run_flags_eval)


@flags_app.command("edit")
def flags_edit(
    key: str = typer.Argument(..., help="Flag key"),
    name: str | None = typer.Option(None, "--name", "-n", help="New display name"),
    description: str | None = typer.Option(None, "--description", "-d", help="New description"),
    off_variation: str | None = typer.Option(
        None, "--off-variation", help="Variation served when flag is disabled"
    ),
    fallthrough: str | None = typer.Option(
        None, "--fallthrough", help="Default variation when no rule matches"
    ),
) -> None:
    """Patch a feature flag (partial update — only provided fields are changed).

    \b
      shield flags edit dark_mode --name "Dark Mode v2"
      shield flags edit dark_mode --off-variation off --fallthrough control
    """

    async def _run_flags_edit() -> None:
        patch: dict[str, Any] = {}
        if name is not None:
            patch["name"] = name
        if description is not None:
            patch["description"] = description
        if off_variation is not None:
            patch["off_variation"] = off_variation
        if fallthrough is not None:
            patch["fallthrough"] = fallthrough
        if not patch:
            err_console.print("[yellow]Nothing to update — provide at least one option.[/yellow]")
            raise typer.Exit(1)
        result = await make_client().patch_flag(key, patch)
        console.print(f"[green]✓[/green] Flag [bold cyan]{result['key']}[/bold cyan] updated.")
        tbl = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, show_header=False)
        tbl.add_column("Field", style="dim", no_wrap=True)
        tbl.add_column("Value", style="bold")
        for field in ("name", "description", "off_variation", "fallthrough"):
            if field in patch:
                val = result.get(field)
                tbl.add_row(field, str(val) if val is not None else "—")
        console.print(tbl)

    _run(_run_flags_edit)


@flags_app.command("variations")
def flags_variations(key: str = typer.Argument(..., help="Flag key")) -> None:
    """List variations for a feature flag."""

    async def _run_flags_variations() -> None:
        flag = await make_client().get_flag(key)
        variations = flag.get("variations") or []
        if not variations:
            console.print(f"[dim]No variations for flag '{key}'.[/dim]")
            return
        tbl = Table(box=box.ROUNDED, show_header=True, header_style="bold")
        tbl.add_column("Name", style="bold cyan", no_wrap=True)
        tbl.add_column("Value", style="white")
        tbl.add_column("Description", style="dim")
        tbl.add_column("Role", style="dim")
        off_var = flag.get("off_variation", "")
        fallthrough = flag.get("fallthrough")
        for v in variations:
            vname = v.get("name", "")
            role = ""
            if vname == off_var:
                role = "[slate]off[/slate]"
            elif isinstance(fallthrough, str) and vname == fallthrough:
                role = "[magenta]fallthrough[/magenta]"
            tbl.add_row(vname, str(v.get("value", "")), v.get("description") or "—", role)
        console.print(f"[bold cyan]{flag['key']}[/bold cyan]  [dim]{flag.get('type', '')}[/dim]")
        console.print(tbl)

    _run(_run_flags_variations)


@flags_app.command("targeting")
def flags_targeting(key: str = typer.Argument(..., help="Flag key")) -> None:
    """Show targeting rules for a feature flag (read-only view)."""

    async def _run_flags_targeting() -> None:
        flag = await make_client().get_flag(key)
        rules = flag.get("rules") or []

        off_var = flag.get("off_variation", "—")
        ft = flag.get("fallthrough", "—")
        console.print(
            f"[bold cyan]{flag['key']}[/bold cyan]"
            f"  off=[cyan]{off_var}[/cyan]"
            f"  fallthrough=[cyan]{ft}[/cyan]"
        )

        if not rules:
            console.print("[dim]No targeting rules.[/dim]")
            return

        for i, rule in enumerate(rules):
            desc = rule.get("description") or ""
            variation = rule.get("variation") or "—"
            clauses = rule.get("clauses") or []
            console.print(
                f"\n  [bold]Rule {i + 1}[/bold]"
                + (f" — {desc}" if desc else "")
                + f"  →  [green]{variation}[/green]"
            )
            console.print(f"  [dim]id: {rule.get('id', '')}[/dim]")
            for clause in clauses:
                attr = clause.get("attribute", "")
                op = clause.get("operator", "")
                vals = clause.get("values") or []
                negate = clause.get("negate", False)
                neg_str = "[dim]NOT[/dim] " if negate else ""
                vals_str = ", ".join(str(v) for v in vals)
                console.print(f"    {neg_str}[cyan]{attr}[/cyan] [dim]{op}[/dim] {vals_str}")

    _run(_run_flags_targeting)


@flags_app.command("add-rule")
def flags_add_rule(
    key: str = typer.Argument(..., help="Flag key"),
    variation: str = typer.Option(
        ..., "--variation", "-v", help="Variation to serve when rule matches"
    ),
    segment: str | None = typer.Option(
        None, "--segment", "-s", help="Segment key (adds an in_segment clause)"
    ),
    attribute: str | None = typer.Option(
        None, "--attribute", "-a", help="Attribute name for a custom clause"
    ),
    operator: str = typer.Option(
        "is", "--operator", "-o", help="Operator (e.g. is, in_segment, contains)"
    ),
    values: str | None = typer.Option(None, "--values", help="Comma-separated clause values"),
    description: str = typer.Option("", "--description", "-d", help="Optional rule description"),
    negate: bool = typer.Option(False, "--negate", help="Negate the clause result"),
) -> None:
    """Add a targeting rule to a feature flag.

    \b
    Segment-based rule (most common):
      shield flags add-rule my-flag --variation on --segment beta-users

    Custom attribute rule:
      shield flags add-rule my-flag --variation on \
        --attribute plan --operator is --values pro,enterprise
    """
    if segment is None and attribute is None:
        console.print("[red]Error:[/red] provide --segment or --attribute.")
        raise typer.Exit(1)
    if segment is not None and attribute is not None:
        console.print("[red]Error:[/red] --segment and --attribute are mutually exclusive.")
        raise typer.Exit(1)

    async def _run_add_rule() -> None:
        client = make_client()
        flag = await client.get_flag(key)
        rules = list(flag.get("rules") or [])

        if segment is not None:
            clause = {
                "attribute": "key",
                "operator": "in_segment",
                "values": [segment],
                "negate": negate,
            }
        else:
            raw_vals: list[Any] = [v.strip() for v in (values or "").split(",") if v.strip()]
            clause = {
                "attribute": attribute,
                "operator": operator,
                "values": raw_vals,
                "negate": negate,
            }

        import uuid as _uuid

        new_rule: dict[str, Any] = {
            "id": str(_uuid.uuid4()),
            "description": description,
            "clauses": [clause],
            "variation": variation,
        }
        rules.append(new_rule)
        await client.patch_flag(key, {"rules": rules})
        clause_summary = (
            f"in_segment [cyan]{segment}[/cyan]"
            if segment is not None
            else f"[cyan]{attribute}[/cyan] [dim]{operator}[/dim] {values}"
        )
        console.print(
            f"[green]✓[/green] Rule added to [bold cyan]{key}[/bold cyan]: "
            f"{clause_summary} → [green]{variation}[/green]"
        )
        console.print(f"  [dim]id: {new_rule['id']}[/dim]")

    _run(_run_add_rule)


@flags_app.command("remove-rule")
def flags_remove_rule(
    key: str = typer.Argument(..., help="Flag key"),
    rule_id: str = typer.Option(..., "--rule-id", "-r", help="Rule ID to remove"),
) -> None:
    """Remove a targeting rule from a feature flag by its ID.

    \b
      shield flags remove-rule my-flag --rule-id <uuid>

    Use 'shield flags targeting my-flag' to list rule IDs.
    """

    async def _run_remove_rule() -> None:
        client = make_client()
        flag = await client.get_flag(key)
        rules = list(flag.get("rules") or [])
        original_len = len(rules)
        rules = [r for r in rules if r.get("id") != rule_id]
        if len(rules) == original_len:
            console.print(f"[red]Error:[/red] no rule with id '{rule_id}' found on flag '{key}'.")
            raise typer.Exit(1)
        await client.patch_flag(key, {"rules": rules})
        console.print(
            f"[green]✓[/green] Rule [dim]{rule_id}[/dim] removed from [bold cyan]{key}[/bold cyan]."
        )

    _run(_run_remove_rule)


# ---------------------------------------------------------------------------
# Prerequisites commands  (shield flags add-prereq / remove-prereq)
# ---------------------------------------------------------------------------


@flags_app.command("add-prereq")
def flags_add_prereq(
    key: str = typer.Argument(..., help="Flag key"),
    prereq_flag: str = typer.Option(..., "--flag", "-f", help="Prerequisite flag key"),
    variation: str = typer.Option(
        ..., "--variation", "-v", help="Variation the prerequisite flag must return"
    ),
) -> None:
    """Add a prerequisite flag to a feature flag.

    \b
      shield flags add-prereq my-flag --flag auth-flag --variation on

    The prerequisite flag must evaluate to the given variation before this
    flag's rules run. If it doesn't, this flag serves its off_variation.
    """

    async def _run_add_prereq() -> None:
        client = make_client()
        flag = await client.get_flag(key)
        if flag["key"] == prereq_flag:
            console.print("[red]Error:[/red] a flag cannot be its own prerequisite.")
            raise typer.Exit(1)
        prereqs = list(flag.get("prerequisites") or [])
        # avoid duplicates
        for p in prereqs:
            if p.get("flag_key") == prereq_flag:
                console.print(
                    f"[yellow]Warning:[/yellow] prerequisite [cyan]{prereq_flag}[/cyan]"
                    " already exists. Updating variation."
                )
                p["variation"] = variation
                await client.patch_flag(key, {"prerequisites": prereqs})
                console.print(
                    f"[green]✓[/green] Prerequisite [cyan]{prereq_flag}[/cyan]"
                    f" updated → must be [green]{variation}[/green]."
                )
                return
        prereqs.append({"flag_key": prereq_flag, "variation": variation})
        await client.patch_flag(key, {"prerequisites": prereqs})
        console.print(
            f"[green]✓[/green] Prerequisite [cyan]{prereq_flag}[/cyan]"
            f" added to [bold cyan]{key}[/bold cyan]:"
            f" must be [green]{variation}[/green]."
        )

    _run(_run_add_prereq)


@flags_app.command("remove-prereq")
def flags_remove_prereq(
    key: str = typer.Argument(..., help="Flag key"),
    prereq_flag: str = typer.Option(..., "--flag", "-f", help="Prerequisite flag key to remove"),
) -> None:
    """Remove a prerequisite from a feature flag.

    \b
      shield flags remove-prereq my-flag --flag auth-flag
    """

    async def _run_remove_prereq() -> None:
        client = make_client()
        flag = await client.get_flag(key)
        prereqs = list(flag.get("prerequisites") or [])
        original_len = len(prereqs)
        prereqs = [p for p in prereqs if p.get("flag_key") != prereq_flag]
        if len(prereqs) == original_len:
            console.print(
                f"[red]Error:[/red] prerequisite [cyan]{prereq_flag}[/cyan]"
                f" not found on flag [cyan]{key}[/cyan]."
            )
            raise typer.Exit(1)
        await client.patch_flag(key, {"prerequisites": prereqs})
        console.print(
            f"[green]✓[/green] Prerequisite [cyan]{prereq_flag}[/cyan]"
            f" removed from [bold cyan]{key}[/bold cyan]."
        )

    _run(_run_remove_prereq)


# ---------------------------------------------------------------------------
# Individual targets commands  (shield flags target / untarget)
# ---------------------------------------------------------------------------


@flags_app.command("target")
def flags_target(
    key: str = typer.Argument(..., help="Flag key"),
    variation: str = typer.Option(
        ..., "--variation", "-v", help="Variation to serve to the context keys"
    ),
    context_keys: str = typer.Option(
        ..., "--keys", "-k", help="Comma-separated context keys to pin"
    ),
) -> None:
    """Pin context keys to always receive a specific variation.

    \b
      shield flags target my-flag --variation on --keys user_123,user_456

    Individual targets are evaluated before rules — highest priority targeting.
    """

    async def _run_target() -> None:
        client = make_client()
        flag = await client.get_flag(key)
        variation_names = [v["name"] for v in (flag.get("variations") or [])]
        if variation not in variation_names:
            console.print(
                f"[red]Error:[/red] variation [cyan]{variation}[/cyan] not found."
                f" Available: {', '.join(variation_names)}"
            )
            raise typer.Exit(1)
        new_keys = [k.strip() for k in context_keys.split(",") if k.strip()]
        targets: dict[str, Any] = dict(flag.get("targets") or {})
        existing = list(targets.get(variation, []))
        added = [k for k in new_keys if k not in existing]
        existing.extend(added)
        targets[variation] = existing
        await client.patch_flag(key, {"targets": targets})
        console.print(
            f"[green]✓[/green] Added {len(added)} key(s)"
            f" to [bold cyan]{key}[/bold cyan] → [green]{variation}[/green]."
        )

    _run(_run_target)


@flags_app.command("untarget")
def flags_untarget(
    key: str = typer.Argument(..., help="Flag key"),
    variation: str = typer.Option(
        ..., "--variation", "-v", help="Variation to remove context keys from"
    ),
    context_keys: str = typer.Option(
        ..., "--keys", "-k", help="Comma-separated context keys to unpin"
    ),
) -> None:
    """Remove context keys from individual targeting.

    \b
      shield flags untarget my-flag --variation on --keys user_123
    """

    async def _run_untarget() -> None:
        client = make_client()
        flag = await client.get_flag(key)
        remove_keys = {k.strip() for k in context_keys.split(",") if k.strip()}
        targets: dict[str, Any] = dict(flag.get("targets") or {})
        existing = list(targets.get(variation, []))
        if not existing:
            console.print(
                f"[yellow]Warning:[/yellow] no targets for variation [cyan]{variation}[/cyan]."
            )
            raise typer.Exit(1)
        updated = [k for k in existing if k not in remove_keys]
        if updated:
            targets[variation] = updated
        else:
            targets.pop(variation, None)
        await client.patch_flag(key, {"targets": targets})
        removed = len(existing) - len(updated)
        console.print(
            f"[green]✓[/green] Removed {removed} key(s)"
            f" from [bold cyan]{key}[/bold cyan] → [cyan]{variation}[/cyan]."
        )

    _run(_run_untarget)


# ---------------------------------------------------------------------------
# Segments command group  (shield segments ...)
# ---------------------------------------------------------------------------

segments_app = typer.Typer(
    name="segments",
    help="Manage targeting segments.",
    no_args_is_help=True,
)
cli.add_typer(segments_app, name="segments")
cli.add_typer(segments_app, name="seg")


def _print_segments_table(segments: list[dict[str, Any]]) -> None:
    tbl = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False)
    tbl.add_column("Key", style="bold cyan", no_wrap=True)
    tbl.add_column("Name", style="white")
    tbl.add_column("Included", style="green")
    tbl.add_column("Excluded", style="red")
    tbl.add_column("Rules", style="dim")
    for s in segments:
        included = s.get("included") or []
        excluded = s.get("excluded") or []
        rules = s.get("rules") or []
        tbl.add_row(
            s.get("key", ""),
            s.get("name", ""),
            str(len(included)),
            str(len(excluded)),
            str(len(rules)),
        )
    console.print(tbl)


@segments_app.command("list")
def segments_list() -> None:
    """List all targeting segments."""

    async def _run_segments_list() -> None:
        segments = await make_client().list_segments()
        if not segments:
            console.print("[dim]No segments found.[/dim]")
            return
        _print_segments_table(segments)
        console.print(f"[dim]{len(segments)} segment(s)[/dim]")

    _run(_run_segments_list)


@segments_app.command("get")
def segments_get(key: str = typer.Argument(..., help="Segment key")) -> None:
    """Show details for a single segment."""

    async def _run_segments_get() -> None:
        seg = await make_client().get_segment(key)
        console.print(f"[bold cyan]{seg['key']}[/bold cyan]  [dim]{seg.get('name', '')}[/dim]")
        included = seg.get("included") or []
        excluded = seg.get("excluded") or []
        rules = seg.get("rules") or []
        if included:
            console.print(
                f"  Included ({len(included)}): [green]{', '.join(included[:10])}[/green]"
                + (" …" if len(included) > 10 else "")
            )
        if excluded:
            console.print(
                f"  Excluded ({len(excluded)}): [red]{', '.join(excluded[:10])}[/red]"
                + (" …" if len(excluded) > 10 else "")
            )
        if rules:
            console.print(f"  Rules: [dim]{len(rules)} targeting rule(s)[/dim]")
        if not included and not excluded and not rules:
            console.print("  [dim](empty segment)[/dim]")

    _run(_run_segments_get)


@segments_app.command("create")
def segments_create(
    key: str = typer.Argument(..., help="Unique segment key"),
    name: str = typer.Option(..., "--name", "-n", help="Human-readable segment name"),
    description: str = typer.Option("", "--description", "-d", help="Optional description"),
) -> None:
    """Create a new targeting segment.

    \b
      shield segments create beta_users --name "Beta Users"
    """

    async def _run_segments_create() -> None:
        segment_data = {
            "key": key,
            "name": name,
            "description": description,
            "included": [],
            "excluded": [],
            "rules": [],
        }
        result = await make_client().create_segment(segment_data)
        console.print(f"[green]✓[/green] Segment [bold cyan]{result['key']}[/bold cyan] created.")

    _run(_run_segments_create)


@segments_app.command("delete")
def segments_delete(
    key: str = typer.Argument(..., help="Segment key"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Permanently delete a targeting segment."""
    if not yes:
        typer.confirm(f"Delete segment '{key}'? This cannot be undone.", abort=True)

    async def _run_segments_delete() -> None:
        result = await make_client().delete_segment(key)
        console.print(f"[green]✓[/green] Segment [bold]{result['deleted']}[/bold] deleted.")

    _run(_run_segments_delete)


@segments_app.command("include")
def segments_include(
    key: str = typer.Argument(..., help="Segment key"),
    context_key: str = typer.Option(
        ...,
        "--context-key",
        "-k",
        help="Comma-separated context keys to add to the included list",
    ),
) -> None:
    """Add context keys to the segment's included list.

    \b
      shield segments include beta_users --context-key user_123,user_456
    """

    async def _run_segments_include() -> None:
        new_keys = [k.strip() for k in context_key.split(",") if k.strip()]
        seg = await make_client().get_segment(key)
        included = list(seg.get("included") or [])
        added = [k for k in new_keys if k not in included]
        included.extend(added)
        seg["included"] = included
        await make_client().update_segment(key, seg)
        console.print(
            f"[green]✓[/green] Added {len(added)} key(s) to [bold cyan]{key}[/bold cyan] "
            f"included list."
        )

    _run(_run_segments_include)


@segments_app.command("exclude")
def segments_exclude(
    key: str = typer.Argument(..., help="Segment key"),
    context_key: str = typer.Option(
        ...,
        "--context-key",
        "-k",
        help="Comma-separated context keys to add to the excluded list",
    ),
) -> None:
    """Add context keys to the segment's excluded list.

    \b
      shield segments exclude beta_users --context-key user_789
    """

    async def _run_segments_exclude() -> None:
        new_keys = [k.strip() for k in context_key.split(",") if k.strip()]
        seg = await make_client().get_segment(key)
        excluded = list(seg.get("excluded") or [])
        added = [k for k in new_keys if k not in excluded]
        excluded.extend(added)
        seg["excluded"] = excluded
        await make_client().update_segment(key, seg)
        console.print(
            f"[green]✓[/green] Added {len(added)} key(s) to [bold cyan]{key}[/bold cyan] "
            f"excluded list."
        )

    _run(_run_segments_exclude)


@segments_app.command("add-rule")
def segments_add_rule(
    key: str = typer.Argument(..., help="Segment key"),
    attribute: str = typer.Option(
        ...,
        "--attribute",
        "-a",
        help="Context attribute (e.g. plan, country)",
    ),
    operator: str = typer.Option(
        "is",
        "--operator",
        "-o",
        help="Operator (e.g. is, in, contains, in_segment)",
    ),
    values: str = typer.Option(
        ...,
        "--values",
        "-V",
        help="Comma-separated values to compare against",
    ),
    description: str = typer.Option("", "--description", "-d", help="Optional rule description"),
    negate: bool = typer.Option(False, "--negate", help="Negate the clause result"),
) -> None:
    """Add an attribute-based targeting rule to a segment.

    \b
    Users matching ANY rule are included in the segment.
    Multiple clauses within one rule are AND-ed together.

    \b
    Examples:
      shield segments add-rule beta_users --attribute plan --operator in --values pro,enterprise
      shield segments add-rule beta_users --attribute country --operator is --values GB
      shield segments add-rule beta_users --attribute email --operator ends_with \\
          --values @acme.com --description "Acme staff"
    """

    async def _run_add_rule() -> None:
        import uuid as _uuid

        client = make_client()
        seg = await client.get_segment(key)
        rules = list(seg.get("rules") or [])

        # For segment operators the attribute defaults to "key"
        attr = "key" if operator in ("in_segment", "not_in_segment") else attribute
        raw_vals: list[Any] = [v.strip() for v in values.split(",") if v.strip()]
        clause: dict[str, Any] = {
            "attribute": attr,
            "operator": operator,
            "values": raw_vals,
            "negate": negate,
        }
        new_rule: dict[str, Any] = {
            "id": str(_uuid.uuid4()),
            "clauses": [clause],
        }
        if description:
            new_rule["description"] = description
        rules.append(new_rule)
        seg["rules"] = rules
        await client.update_segment(key, seg)

        clause_summary = f"[cyan]{attr}[/cyan] [dim]{operator}[/dim] {values}"
        console.print(
            f"[green]✓[/green] Rule added to segment [bold cyan]{key}[/bold cyan]: {clause_summary}"
        )
        console.print(f"  [dim]id: {new_rule['id']}[/dim]")

    _run(_run_add_rule)


@segments_app.command("remove-rule")
def segments_remove_rule(
    key: str = typer.Argument(..., help="Segment key"),
    rule_id: str = typer.Option(..., "--rule-id", "-r", help="Rule ID to remove"),
) -> None:
    """Remove a targeting rule from a segment by its ID.

    \b
      shield segments remove-rule beta_users --rule-id <uuid>

    Use 'shield segments get beta_users' to list rule IDs.
    """

    async def _run_remove_rule() -> None:
        client = make_client()
        seg = await client.get_segment(key)
        rules = list(seg.get("rules") or [])
        original_len = len(rules)
        rules = [r for r in rules if r.get("id") != rule_id]
        if len(rules) == original_len:
            console.print(
                f"[red]Error:[/red] no rule with id '{rule_id}' found on segment '{key}'."
            )
            raise typer.Exit(1)
        seg["rules"] = rules
        await client.update_segment(key, seg)
        console.print(
            f"[green]✓[/green] Rule [dim]{rule_id}[/dim] removed from segment "
            f"[bold cyan]{key}[/bold cyan]."
        )

    _run(_run_remove_rule)


if __name__ == "__main__":
    cli()
