"""Command-line interface.

Exit codes: 0 success, 1 configuration problem, 2 Eventor API failure,
3 Google API failure (including any failed write during ``--apply``),
4 safety guard refused to apply.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv
from eventor_client import EventorError

from eventor_contacts_sync import __version__
from eventor_contacts_sync.config import ConfigError, load_config
from eventor_contacts_sync.google import GoogleError
from eventor_contacts_sync.report import scrub, write_report
from eventor_contacts_sync.sync import (
    SafetyGuardError,
    make_eventor_client,
    make_people_client,
    run,
    with_overrides,
)

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_EVENTOR = 2
EXIT_GOOGLE = 3
EXIT_GUARD = 4

app = typer.Typer(
    help="Keep Google Contacts in sync with a club's Eventor members and entrants.",
    add_completion=False,
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _echo(message: str = "") -> None:
    typer.echo(message)


def _err(message: str) -> None:
    typer.echo(message, err=True)


@app.callback()
def _common(
    ctx: typer.Context,
    env_file: Annotated[
        Path,
        typer.Option("--env-file", help="Load environment variables from this file if it exists."),
    ] = Path(".env"),
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
    no_progress: Annotated[
        bool, typer.Option("--no-progress", help="Suppress progress messages on stderr.")
    ] = False,
    version: Annotated[bool, typer.Option("--version", help="Print the version and exit.")] = False,
) -> None:
    if version:
        _echo(f"eventor-contacts-sync {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        _echo(ctx.get_help())
        raise typer.Exit()
    if env_file.is_file():
        load_dotenv(env_file, override=False)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    if not verbose:
        # "No project ID could be determined" is noise here: no project-scoped API is called.
        logging.getLogger("google.auth._default").setLevel(logging.ERROR)
    if not verbose and not no_progress:
        # Progress lines from this package only; the Eventor pull takes a few minutes
        # and would otherwise sit silent the whole time.
        progress = logging.StreamHandler(sys.stderr)
        progress.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        progress.setLevel(logging.INFO)
        for name in ("eventor_contacts_sync.sources", "eventor_contacts_sync.sync"):
            logger = logging.getLogger(name)
            logger.setLevel(logging.INFO)
            logger.addHandler(progress)
            logger.propagate = False


def _load(config_path: Path | None, *, require_google: bool = True):
    try:
        return load_config(config_path=config_path, require_google=require_google)
    except ConfigError as exc:
        _err(f"configuration error: {exc}")
        raise typer.Exit(EXIT_CONFIG) from exc


@app.command()
def sync(
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Write changes to Google. Without it, nothing is written."),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Explicitly request a dry run (the default).")
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Apply even if the label-removal safety guard would refuse."),
    ] = False,
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit", min=1, help="Write at most N contacts (for a cautious first apply)."
        ),
    ] = None,
    report: Annotated[
        Path | None,
        typer.Option(
            "--report",
            help="Path for the JSON run report (default SYNC_REPORT_PATH or report.json).",
        ),
    ] = None,
    config: Annotated[
        Path | None, typer.Option("--config", help="TOML file with event-series label mappings.")
    ] = None,
    window_months: Annotated[
        int | None, typer.Option("--window-months", min=1, help="Override SYNC_WINDOW_MONTHS.")
    ] = None,
    redact: Annotated[
        bool,
        typer.Option(
            "--redact",
            help="No names or contact details in the diff, report or errors; Eventor IDs only.",
        ),
    ] = False,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Do not print the diff.")] = False,
    as_of: Annotated[
        datetime | None,
        typer.Option("--as-of", hidden=True, formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"]),
    ] = None,
) -> None:
    """Pull members and entrants from Eventor and reconcile the Google contacts."""
    if apply and dry_run:
        _err("--apply and --dry-run are mutually exclusive")
        raise typer.Exit(EXIT_CONFIG)
    cfg = with_overrides(_load(config), window_months=window_months, report_path=report)
    try:
        result = run(cfg, apply=apply, force=force, limit=limit, now=as_of, redact=redact)
    except EventorError as exc:
        _err(f"Eventor API failure: {exc}")
        raise typer.Exit(EXIT_EVENTOR) from exc
    except GoogleError as exc:
        _err(f"Google API failure: {scrub(str(exc)) if redact else exc}")
        raise typer.Exit(EXIT_GOOGLE) from exc
    except SafetyGuardError as exc:
        _err(f"safety guard: {exc}")
        raise typer.Exit(EXIT_GUARD) from exc

    if not quiet:
        _echo(result.diff_text)
    write_report(cfg.report_path, result.report)
    _echo(f"Report written to {cfg.report_path}")
    if not result.ok:
        _err(f"{len(result.api_errors)} change(s) failed to apply; see the report")
        raise typer.Exit(EXIT_GOOGLE)


@app.command()
def whoami() -> None:
    """Check both connections: the Eventor key's organisation and the Google account."""
    cfg = _load(None, require_google=False)
    try:
        with make_eventor_client(cfg) as client:
            org = client.organisation_for_api_key()
    except EventorError as exc:
        _err(f"Eventor API failure: {exc}")
        raise typer.Exit(EXIT_EVENTOR) from exc
    _echo(f"Eventor: {org.name or org.short_name} (organisation #{org.id})")
    if not cfg.google_user:
        _echo("Google: GOOGLE_USER is not set; skipped")
        return
    cfg = _load(None)
    try:
        with make_people_client(cfg) as people:
            labels = people.list_groups()
            contacts = people.list_contacts()
    except GoogleError as exc:
        _err(f"Google API failure: {exc}")
        raise typer.Exit(EXIT_GOOGLE) from exc
    _echo(f"Google: {cfg.google_user} has {len(contacts)} contacts and {len(labels)} labels")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
