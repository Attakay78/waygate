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
            all_states = sorted(await client.list_routes(), key=lambda x: x["path"])
            paginated, has_prev, has_next, first_num, last_num = _paginate(
                all_states, page, per_page
            )

        if not paginated:
            console.print("[dim]No routes registered.[/dim]")
            return

        table = Table(box=box.ROUNDED, show_header=True, header_style="bold")
        table.add_column("Route", style="cyan")
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
            table.add_row(
                s["path"],
                f"[{colour}]{s['status'].upper()}[/{colour}]",
                s.get("reason") or "—",
                ", ".join(envs) if envs else "—",
                window_end or "—",
            )

        console.print(table)
        if not route and (has_prev or has_next):
            _print_page_footer(page, per_page, first_num, last_num, has_prev, has_next)

    _run(_run_status)


@cli.command("enable")
def enable(
    route: str = typer.Argument(..., help="Route: /path or METHOD:/path"),
    reason: str = typer.Option("", "--reason", "-r", help="Optional note for the audit log."),
) -> None:
    """Enable a route that is in maintenance or disabled state."""

    async def _run_enable() -> None:
        key = _parse_route(route)
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
) -> None:
    """Permanently disable a route (returns 503 to all callers)."""

    async def _run_disable() -> None:
        key = _parse_route(route)
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
) -> None:
    """Put a route into maintenance mode immediately."""

    async def _run_maintenance() -> None:
        key = _parse_route(route)
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
) -> None:
    """Schedule a future maintenance window (auto-activates and deactivates)."""

    async def _run_schedule() -> None:
        key = _parse_route(route)
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


if __name__ == "__main__":
    cli()
