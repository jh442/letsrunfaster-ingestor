"""Command-line interface for Garmin Ingestor."""

import json
from pathlib import Path
from typing import Optional
import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn
from rich.table import Table

from .auth import GarminAuthManager, AuthStatus
from .exporter import GarminExporter, ExportFormat
from .version import get_version
from . import settings as app_settings

app = typer.Typer(
    help="Garmin Ingestor: Bulk export activities (FIT/GPX) to local disk or remote API."
)
console = Console()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"LetsRunFaster Ingestor {get_version()}")
        raise typer.Exit()


@app.callback()
def _app_meta(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show the installed version and exit.",
    ),
) -> None:
    """Bulk export Garmin Connect activities to disk or the LetsRunFaster site."""


@app.command()
def login(
    email: Optional[str] = typer.Option(None, prompt=True, help="Garmin Connect email"),
    password: Optional[str] = typer.Option(
        None, prompt=True, hide_input=True, help="Garmin Connect password"
    ),
):
    """Log in to Garmin Connect and cache session tokens locally."""
    auth_mgr = GarminAuthManager()
    console.print("[cyan]Authenticating with Garmin Connect...[/cyan]")
    res, client = auth_mgr.login_with_credentials(email, password)

    if res.status == AuthStatus.MFA_REQUIRED:
        console.print("[yellow]Two-step verification (MFA) required.[/yellow]")
        mfa_code = typer.prompt("Enter the verification code sent to your email/SMS")
        res = auth_mgr.submit_mfa(client, res.client_state, mfa_code)

    if res.status == AuthStatus.AUTHENTICATED:
        console.print(f"[green]✓ {res.message}[/green]")
        console.print(f"Logged in as: [bold]{res.display_name}[/bold]")
        console.print(f"Session cached in: [dim]{auth_mgr.token_store}[/dim]")
    else:
        console.print(f"[bold red]✗ Login failed:[/bold red] {res.message}")
        raise typer.Exit(code=1)


@app.command()
def status():
    """Check current authentication status and active user."""
    auth_mgr = GarminAuthManager()
    res = auth_mgr.login_with_tokens()
    if res.status == AuthStatus.AUTHENTICATED:
        console.print(f"[green]✓ Authenticated as:[/green] [bold]{res.display_name}[/bold]")
        console.print(f"Session path: [dim]{auth_mgr.token_store}[/dim]")
    else:
        console.print(f"[yellow]Not logged in or tokens expired:[/yellow] {res.message}")
        console.print("Run [bold cyan]garmin-ingestor login[/bold cyan] to authenticate.")


@app.command()
def logout():
    """Clear cached session tokens."""
    auth_mgr = GarminAuthManager()
    if auth_mgr.clear_tokens():
        console.print("[green]✓ Session tokens cleared.[/green]")
    else:
        console.print("[yellow]No cached tokens found or error clearing tokens.[/yellow]")


@app.command()
def list_activities(
    start_date: str = typer.Option(..., "--start", "-s", help="Start date (YYYY-MM-DD)"),
    end_date: str = typer.Option(..., "--end", "-e", help="End date (YYYY-MM-DD)"),
    activity_type: Optional[str] = typer.Option(None, "--type", "-t", help="Filter by activity type (e.g. running, cycling)"),
    unit: str = typer.Option("mi", "--unit", "-u", help="Distance unit: mi (miles) or km (kilometers)"),
):
    """List activities in the given date range without downloading files."""
    auth_mgr = GarminAuthManager()
    auth_res = auth_mgr.login_with_tokens()
    if auth_res.status != AuthStatus.AUTHENTICATED:
        console.print("[red]Not authenticated. Please run 'garmin-ingestor login' first.[/red]")
        raise typer.Exit(code=1)

    client = auth_mgr.get_client()
    client.login(tokenstore=auth_mgr.token_store)
    exporter = GarminExporter(client)

    with console.status("[cyan]Fetching activity list...[/cyan]"):
        activities = exporter.get_activities_in_range(start_date, end_date, activity_type=activity_type)

    if not activities:
        console.print("[yellow]No activities found for the specified date range.[/yellow]")
        return

    is_miles = unit.lower() != "km"
    unit_label = "mi" if is_miles else "km"
    table = Table(title=f"Garmin Activities ({start_date} to {end_date})")
    table.add_column("ID", style="dim", width=12)
    table.add_column("Start Time", style="cyan", width=20)
    table.add_column("Type", style="green", width=15)
    table.add_column("Name", style="bold")
    table.add_column(f"Distance ({unit_label})", justify="right", width=14)

    for act in activities:
        if act.distance_meters:
            dist_val = (act.distance_meters * 0.000621371) if is_miles else (act.distance_meters / 1000)
            dist_str = f"{dist_val:.2f}"
        else:
            dist_str = "-"

        table.add_row(
            str(act.activity_id),
            act.start_time_local,
            act.activity_type,
            act.activity_name,
            dist_str,
        )

    console.print(table)
    console.print(f"[dim]Total: {len(activities)} activities[/dim]")


@app.command()
def configure(
    remote_url: Optional[str] = typer.Option(None, "--remote-url", help="LetsRunFaster ingest URL to save"),
    remote_key: Optional[str] = typer.Option(None, "--remote-key", help="LetsRunFaster API key to save (lrf_...)"),
    clear_key: bool = typer.Option(False, "--clear-key", help="Forget the saved API key"),
    show: bool = typer.Option(False, "--show", help="Show saved settings (key prefix only)"),
):
    """Save/show the LetsRunFaster push target used by default on export."""
    if show or (remote_url is None and remote_key is None and not clear_key):
        saved = app_settings.public_config()
        console.print(f"Target URL: [bold]{saved['remote_url']}[/bold]")
        if saved["has_key"]:
            console.print(f"API key: [green]saved ({saved['key_prefix']}...)[/green]")
        else:
            console.print("[yellow]API key: not saved[/yellow]")
        return
    saved = app_settings.save_config(
        remote_url=remote_url, remote_api_key=remote_key, clear_key=clear_key
    )
    console.print(f"[green]✓ Saved push target:[/green] [bold]{saved['remote_url']}[/bold]")
    if saved["has_key"]:
        console.print(f"API key: [green]saved ({saved['key_prefix']}...)[/green]")
    elif clear_key:
        console.print("[yellow]API key cleared.[/yellow]")


@app.command()
def export(
    start_date: str = typer.Option(..., "--start", "-s", help="Start date (YYYY-MM-DD)"),
    end_date: str = typer.Option(..., "--end", "-e", help="End date (YYYY-MM-DD)"),
    format: ExportFormat = typer.Option(ExportFormat.FIT, "--format", "-f", help="Export format (fit or gpx)"),
    output_dir: Optional[str] = typer.Option("./garmin_exports", "--output", "-o", help="Local directory to save files"),
    remote_url: Optional[str] = typer.Option(None, "--remote-url", help="Target API URL for pushing activity files"),
    remote_api_key: Optional[str] = typer.Option(None, "--remote-key", help="API key for remote target Authorization header"),
    activity_type: Optional[str] = typer.Option(None, "--type", "-t", help="Filter by activity type"),
    delay: float = typer.Option(0.5, "--delay", help="Delay in seconds between requests to avoid rate limits"),
):
    """Bulk download activities between start and end dates."""
    auth_mgr = GarminAuthManager()
    auth_res = auth_mgr.login_with_tokens()
    if auth_res.status != AuthStatus.AUTHENTICATED:
        console.print("[red]Not authenticated. Please run 'garmin-ingestor login' first.[/red]")
        raise typer.Exit(code=1)

    # Omitted flags fall back to saved `configure` settings ("" disables push).
    saved_url, saved_key = app_settings.get_remote_settings()
    if remote_url is None:
        remote_url = saved_url
    elif not remote_url.strip():
        remote_url = None
    if remote_api_key is None:
        remote_api_key = saved_key
    elif not remote_api_key.strip():
        remote_api_key = None

    client = auth_mgr.get_client()
    client.login(tokenstore=auth_mgr.token_store)
    exporter = GarminExporter(client)

    with console.status("[cyan]Querying Garmin activities...[/cyan]"):
        activities = exporter.get_activities_in_range(start_date, end_date, activity_type=activity_type)

    if not activities:
        console.print("[yellow]No activities found for the specified date range.[/yellow]")
        return

    console.print(f"Found [bold cyan]{len(activities)}[/bold cyan] activities to export.")
    dest_path = Path(output_dir) if output_dir else None
    if dest_path:
        console.print(f"Saving to directory: [bold]{dest_path.resolve()}[/bold]")
    if remote_url:
        console.print(f"Syncing to remote API: [bold]{remote_url}[/bold]")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TextColumn("({task.completed}/{task.total})"),
    ) as progress:
        task = progress.add_task("[green]Exporting activities...", total=len(activities))

        def on_progress(current: int, total: int, res):
            progress.update(task, advance=1, description=f"[green]Exported: {res.filename}")

        results = exporter.bulk_export(
            activities=activities,
            export_format=format,
            output_dir=dest_path,
            remote_url=remote_url,
            remote_api_key=remote_api_key,
            delay_seconds=delay,
            progress_callback=on_progress,
        )

    successful = [r for r in results if r.success]
    failed = [r for r in results if not r.success]

    console.print("\n[bold]Export Summary:[/bold]")
    console.print(f"  ✓ Successfully downloaded: [green]{len(successful)}[/green]")
    if failed:
        console.print(f"  ✗ Failed: [red]{len(failed)}[/red]")
        for f in failed:
            console.print(f"    - Activity {f.activity_id}: {f.error_message}")


@app.command()
def ui(
    host: str = typer.Option("127.0.0.1", "--host", "-h", help="Host address to bind to"),
    port: int = typer.Option(8000, "--port", "-p", help="Port number (0 picks a free port)"),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Automatically open browser"),
    no_browser: bool = typer.Option(
        False, "--no-browser", hidden=True, help="Alias for --no-open"
    ),
):
    """Launch the interactive web UI for Garmin Ingestor."""
    # Shared with the double-click launcher so both print the LRF_READY
    # handshake (the desktop shell needs the resolved port) and so neither
    # depends on uvicorn's string-based import machinery, which is
    # unavailable inside a PyInstaller-frozen binary.
    from .launcher import run_ui

    run_ui(host=host, port=port, open_browser=open_browser and not no_browser)


@app.command()
def diagnose():
    """Check TLS, HTTP, Garmin, and config one step at a time.

    Use this when the app stops without a Python error: every step writes
    begin/end lines to logs/engine-python.log, so the last line names the
    primitive that killed the process.
    """
    from .diagnose import run

    raise typer.Exit(code=run())


if __name__ == "__main__":
    app()
