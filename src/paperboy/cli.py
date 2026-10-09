"""The `paperboy` Typer CLI — thin wrappers over `app.py` (composition root),
`recipes.collect_channel`, `export.jsonl.export_jsonl`, and `doctor.run_doctor`.
Every command is `asyncio.run`-based since everything underneath is async.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, cast

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from paperboy import app as composition
from paperboy.collectors.posts import GET_MESSAGES_BATCH
from paperboy.config import (
    Settings,
    load_settings,
    parse_duration,
    parse_msg_ids,
    parse_since,
    profile_dir,
)
from paperboy.doctor import doctor_blocks, run_doctor
from paperboy.export.jsonl import export_jsonl
from paperboy.fetch_from_list import (
    FetchSummary,
    fetch_from_list,
    initial_results,
    write_report,
)
from paperboy.ids import channel_uri
from paperboy.logging_setup import configure_logging
from paperboy.media_list import (
    ClassifiedRow,
    MediaListError,
    Segment,
    classify_rows,
    excluded_channel_ids,
    parse_media_list,
    plan_segments,
)
from paperboy.media_store import MediaStoreError, build_media_store
from paperboy.recipes import collect_channel
from paperboy.replay import ReplaySource, ReprojectSourceError
from paperboy.reproject import ReprojectError, TargetFilter, resolve_target_filter
from paperboy.reproject import reproject as reproject_run
from paperboy.store.channels import find_channel_id
from paperboy.targets import Target, UnsupportedTarget, parse_target

app = typer.Typer(
    add_completion=False,
    help="Local, read-only Telegram channel OSINT collector. See docs/opsec.md first.",
)
console = Console()


def _parse_target_or_exit(raw: str) -> Target:
    """`parse_target`, but an unparsable TARGET is a clean one-line CLI error
    (exit 1) rather than an uncaught exception with a Rich traceback."""
    try:
        return parse_target(raw)
    except UnsupportedTarget as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=1) from exc

_PACING_HELP = (
    "Multiply every request interval we assume by this (default 2.0, min 1.0). "
    "Never applied to server-mandated FLOOD_WAITs."
)
_MEDIA_STORE_HELP = (
    "Where this run's media goes: gs://<bucket>/<prefix> (the bucket must be in "
    "PAPERBOY_MEDIA_STORE_BUCKETS; no local copy is kept). Default: the local profile folder."
)
_FLOOD_HELP = (
    "Longest single FLOOD_WAIT (seconds, margin included) to sleep through before "
    "stopping the phase (default 3600)."
)


def _load_settings_or_exit(profile: str, overrides: dict[str, object]) -> Settings:
    """`load_settings`, but an invalid setting (e.g. a media store whose bucket is not
    in `media_store_buckets`) is a clean one-line CLI error (exit 1), not a traceback.
    Only the field and message are printed, never the offending value."""
    try:
        return load_settings(profile, overrides)
    except ValidationError as exc:
        for err in exc.errors():
            where = ".".join(str(part) for part in err["loc"]) or "settings"
            console.print(f"[red]invalid setting {escape(where)}: {escape(err['msg'])}[/]")
        raise typer.Exit(code=1) from None


def _settings_with_overrides(profile: str, **overrides: object) -> Settings:
    clean = {k: v for k, v in overrides.items() if v is not None}
    return _load_settings_or_exit(profile, clean)


def _run_async_or_exit[T](coro: Coroutine[Any, Any, T]) -> T:
    """`asyncio.run(coro)`, translating a missing-credentials `ConfigError`
    (raised by `composition.build_client`/`build_gateway` when no `api_id`/
    `api_hash` is configured for this profile) into a clean, actionable CLI
    exit instead of a raw traceback. Every command that reaches Telethon
    (`doctor`, `collect`) goes through here before ever touching the network.
    """
    try:
        return asyncio.run(coro)
    except composition.ConfigError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=1) from None


def _find_channel_id(store, target: Target) -> int | None:
    """`status`/`export`: the locally stored channel a target names."""
    return find_channel_id(store, target)


@app.command()
def auth(profile: str = typer.Option("default", "--profile")) -> None:
    """Interactive login: prompts for phone + code on stdin, saves the session to the Keychain."""
    settings = _settings_with_overrides(profile)
    secrets = composition.build_secrets(profile)
    try:
        client = composition.build_client(settings, secrets, profile)
    except composition.ConfigError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=1) from None

    async def _run() -> None:
        # Telethon ships no return-type stubs for these — `start`/`disconnect`
        # are plain `def`s that return a coroutine when (as here) an event
        # loop is already running; verified against the installed version,
        # not guessed. See the same rationale in gateway.py.
        await cast(Coroutine[Any, Any, Any], client.start())  # phone + code (+ 2FA) on stdin
        me = await client.get_me()
        session = client.session
        assert session is not None
        secrets.set_session(session.save())
        await cast(Coroutine[Any, Any, Any], client.disconnect())
        me_id = getattr(me, "id", "unknown")
        console.print(f"[green]Logged in[/] as id={me_id} — session saved to the Keychain.")

    asyncio.run(_run())


@app.command()
def doctor(
    profile: str = typer.Option("default", "--profile"),
    strict: bool = typer.Option(False, "--strict"),
    pacing_factor: float = typer.Option(None, "--pacing-factor", min=1.0, help=_PACING_HELP),
    max_flood_sleep: int = typer.Option(None, "--max-flood-sleep", min=0, help=_FLOOD_HELP),
) -> None:
    """Opsec preflight: proxy, session age, 2FA, privacy keys, profile minimalism."""
    settings = _settings_with_overrides(
        profile, pacing_factor=pacing_factor, flood_sleep_threshold=max_flood_sleep
    )
    configure_logging(profile_dir(settings, profile) / "paperboy.log", console=False)
    secrets = composition.build_secrets(profile)

    with composition.build_store(settings, profile) as store:
        checks = _run_async_or_exit(_run_doctor(settings, secrets, profile, store))

    table = Table(title=f"paperboy doctor — profile {profile!r}")
    table.add_column("check")
    table.add_column("status")
    table.add_column("detail")
    for c in checks:
        if c.ok:
            status = "[green]ok[/]"
        elif c.severity == "fail":
            status = "[red]fail[/]"
        else:
            status = "[yellow]warn[/]"
        table.add_row(c.name, status, c.detail)
    console.print(table)

    blocked = doctor_blocks(checks)
    if blocked:
        console.print("[red]BLOCKED[/]: collect refuses to run without --unsafe.")
        raise typer.Exit(code=1)
    if strict and any(not c.ok for c in checks):
        console.print("[yellow]--strict: a warning is present.[/]")
        raise typer.Exit(code=1)
    console.print("[green]PASS[/]")


async def _run_doctor(settings, secrets, profile: str, store):
    gateway = await composition.build_gateway(settings, secrets, profile, store)
    return await run_doctor(gateway, settings)


@app.command()
def collect(
    target: str,
    profile: str = typer.Option("default", "--profile"),
    phases: str = typer.Option(
        None, "--phases",
        help="Comma-separated: channel,history,discussion,participants,profiles,graph,web,media",
    ),
    join: bool = typer.Option(
        False, "--join",
        help="Join a linked discussion group that gates reading behind membership "
             "(join_to_send). An active, non-passive WRITE — off by default.",
    ),
    media: bool = typer.Option(
        False, "--media", help="Also download message media (opt-in; off by default)."
    ),
    web: bool = typer.Option(
        False, "--web", help="Also capture t.me/s + Wayback snapshots over HTTP (opt-in)."
    ),
    profile_budget: int = typer.Option(None, "--profile-budget"),
    max_rpc: int = typer.Option(None, "--max-rpc"),
    unsafe: bool = typer.Option(False, "--unsafe", help="Skip the doctor preflight gate."),
    profiles: bool = typer.Option(
        False, "--profiles",
        help="Run FULL profile enrichment (getFullUser, photo history, avatar download) on top of "
             "the always-on getUsers triage — ~1 RPC/s, bounded by --profile-budget.",
    ),
    profile_interval: float = typer.Option(
        None, "--profile-interval",
        help="Base seconds between full-profile RPCs (default 1.0), multiplied by "
             "--pacing-factor.",
    ),
    pacing_factor: float = typer.Option(None, "--pacing-factor", min=1.0, help=_PACING_HELP),
    max_flood_sleep: int = typer.Option(None, "--max-flood-sleep", min=0, help=_FLOOD_HELP),
    profile_refresh_after: str = typer.Option(
        None, "--profile-refresh-after",
        help="Skip re-enriching users enriched more recently than this (e.g. 7d, 12h, 30m).",
    ),
    media_since: str = typer.Option(
        None, "--media-since",
        help="With --media: only download media for posts dated at/after this — a "
             "duration back from now (180d) or an ISO date (2026-03-22, UTC).",
    ),
    media_msgs: str = typer.Option(
        None, "--media-msgs",
        help="With --media: only download media for these message ids, e.g. 8554,8600-8602.",
    ),
    media_max_mb: int = typer.Option(
        None, "--media-max-mb", min=1,
        help="With --media: skip any file larger than this many MB (size as Telegram records it).",
    ),
    media_min_free_gb: float = typer.Option(
        None, "--media-min-free-gb", min=0.0,
        help="With --media: stop the media phase when free disk on the media volume, minus "
             "the next file's declared size, would fall below this many GB (default 5).",
    ),
    media_store: str = typer.Option(None, "--media-store", help=_MEDIA_STORE_HELP),
) -> None:
    """Collect channel metadata, full message history, and the discovery/
    relationship graph for TARGET."""
    overrides: dict[str, object] = {}
    if join:
        console.print(
            "[yellow]--join enabled: paperboy will JOIN a join_to_send discussion "
            "group to read it — an active, non-passive act, recorded in run_events.[/]"
        )
        overrides["allow_join"] = True
    if profile_budget is not None:
        overrides["profile_budget"] = profile_budget
    if max_rpc is not None:
        overrides["max_rpc_per_run"] = max_rpc
    if profiles:
        overrides["enrich_profiles"] = True
    if profile_interval is not None:
        overrides["profile_interval"] = profile_interval
    if pacing_factor is not None:
        overrides["pacing_factor"] = pacing_factor
    if max_flood_sleep is not None:
        overrides["flood_sleep_threshold"] = max_flood_sleep
    if profile_refresh_after is not None:
        try:
            overrides["profile_refresh_after"] = parse_duration(profile_refresh_after)
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint="--profile-refresh-after") from None
    if media_since is not None:
        try:
            overrides["media_since"] = parse_since(media_since, datetime.now(UTC))
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint="--media-since") from None
    if media_msgs is not None:
        try:
            overrides["media_msgs"] = parse_msg_ids(media_msgs)
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint="--media-msgs") from None
    if media_max_mb is not None:
        overrides["media_max_mb"] = media_max_mb
    if media_min_free_gb is not None:
        overrides["media_min_free_gb"] = media_min_free_gb
    if media_store is not None:
        overrides["media_store"] = media_store
    if unsafe:
        overrides["unsafe"] = True
    settings = _load_settings_or_exit(profile, overrides)
    if settings.enrich_profiles:
        console.print(
            "[yellow]--profiles enabled: full profile enrichment (getFullUser, photo history, "
            f"avatars) will run for up to {settings.profile_budget} users this run.[/]"
        )

    configure_logging(profile_dir(settings, profile) / "paperboy.log", console=True)
    log = logging.getLogger("paperboy.cli")
    parsed_target = _parse_target_or_exit(target)
    secrets = composition.build_secrets(profile)
    phase_list = phases.split(",") if phases else None
    _dependent_phases = [
        p for p in ("history", "discussion", "participants", "profiles", "graph", "media", "web")
        if phase_list and p in phase_list
    ]
    if phase_list is not None and _dependent_phases and "channel" not in phase_list:
        # The dependent phases need `CollectContext.input_channel`/`channel_id`
        # (the channel's numeric id + access_hash), which the `channel` phase
        # sets for every later collector in the SAME run — it's per-process
        # context, not reloaded from the store (access_hash can rotate and
        # isn't persisted). Running any of them without `channel` leaves that
        # context unset and the collector would crash; reject it here, before
        # any RPC (or even doctor/store setup) runs, with a clear message.
        console.print(
            f"[red]--phases {','.join(_dependent_phases)} requires channel in the "
            "same run[/] (channel resolves the access hash they need — it isn't "
            "persisted between runs). Pass e.g. --phases channel,history,graph, "
            "or omit --phases to run the default set."
        )
        raise typer.Exit(code=1)

    with composition.build_store(settings, profile) as store:
        results = _run_async_or_exit(
            _run_collect(
                settings, secrets, profile, store, parsed_target,
                phase_list, log, media, web,
            )
        )

    table = Table(title=f"collect {target}")
    table.add_column("phase")
    table.add_column("counts")
    table.add_column("stopped")
    for r in results:
        table.add_row(r.name, str(r.counts), r.stopped or "-")
    console.print(table)

    # Issue #56: if the target itself could not be used — the `channel` phase
    # was skipped or stopped (a deleted/renamed handle, a private channel) —
    # exit non-zero for scripts and queues. A later phase stopping leaves the
    # exit code alone: the target was usable.
    channel_result = next((r for r in results if r.name == "channel"), None)
    if channel_result is not None and channel_result.stopped is not None:
        how = {"skip": "was skipped", "phase_stop": "stopped", "hard_stop": "hit a hard stop"}.get(
            channel_result.stopped, f"stopped ({channel_result.stopped})"
        )
        # Some phases don't need the channel: `web` works from the handle
        # alone and can still archive a deleted channel's t.me/s and Wayback
        # pages. Name any phase that reported non-zero counts rather than
        # claiming nothing was collected; the exit code stays 1 either way.
        others = [r for r in results if r.name != "channel" and any(r.counts.values())]
        if others:
            detail = "; ".join(f"{r.name} {r.counts}" for r in others)
            log.warning(
                "collect: channel phase %s; other phases reported: %s",
                channel_result.stopped, detail,
            )
            console.print(
                f"[red]{target}: the channel phase {how} — the target itself couldn't be "
                f"used.[/] Other phases still reported: {detail}. "
                "See the warning above for the reason."
            )
        else:
            console.print(
                f"[red]{target}: the channel phase {how} — nothing was collected.[/] "
                "See the warning above for the reason."
            )
        raise typer.Exit(code=1)


async def _run_collect(
    settings, secrets, profile, store, target, phase_list, log, media, web
):
    gateway = await composition.build_gateway(settings, secrets, profile, store)
    if not settings.unsafe:
        checks = await run_doctor(gateway, settings)
        if doctor_blocks(checks):
            console.print(
                "[red]doctor preflight failed[/] — refusing to collect. "
                "Run `paperboy doctor` for details, or pass --unsafe to override."
            )
            raise typer.Exit(code=1)
    return await collect_channel(
        gateway, store, settings, target, phase_list, log, media=media, web=web, profile=profile
    )


def _gb(nbytes: int) -> str:
    return f"{nbytes / 1e9:.2f}"


def _dry_label(c: ClassifiedRow) -> str:
    """The offline outcome the dry run shows: a handle row the store has never
    seen is `needs_resolve` (it is looked up live, by handle, in the real run)."""
    return "needs_resolve" if c.needs_resolve else c.outcome


# `pending` = addressable rows whose post will be fetched (and media, unless held).
_DRY_OUTCOMES = ("pending", "needs_resolve", "excluded", "duplicate_row")


def _print_plan(classified: list[ClassifiedRow], segments: list[Segment]) -> None:
    """The three offline tables: outcome -> rows -> declared GB, channel x
    state, and the segment plan. Channels appear by numeric id only
    (logs/consoles reference targets by id); a handle row for a channel the
    store has never seen is grouped under `-`."""
    by_outcome: dict[str, list[ClassifiedRow]] = {}
    for c in classified:
        by_outcome.setdefault(_dry_label(c), []).append(c)
    outcomes = Table(title="fetch-from-list: offline classification")
    outcomes.add_column("outcome")
    outcomes.add_column("rows", justify="right")
    outcomes.add_column("declared GB", justify="right")
    for name in _DRY_OUTCOMES:
        rows = by_outcome.get(name, [])
        outcomes.add_row(name, str(len(rows)), _gb(sum(c.declared_bytes or 0 for c in rows)))
    total_bytes = sum(c.declared_bytes or 0 for c in classified)
    outcomes.add_row("total", str(len(classified)), _gb(total_bytes))
    console.print(outcomes)

    # Per channel (by id): what each costs before anything is fetched. Of the
    # pending rows, `in_store` already have their post, `not_yet_collected` will
    # be new, and `media_stored` already have their file in this run's store.
    columns = (
        "pending", "in_store", "not_yet_collected", "media_stored", "needs_resolve",
        "excluded", "duplicate_row",
    )
    per_channel: dict[str, Counter[str]] = {}
    for c in classified:
        label = str(c.channel_id) if c.channel_id is not None else "-"
        counts = per_channel.setdefault(label, Counter())
        name = _dry_label(c)
        counts[name] += 1
        if name == "pending":
            counts["in_store" if c.in_store else "not_yet_collected"] += 1
            counts["media_stored"] += c.media_held
    channels = Table(title="fetch-from-list: per channel (rows by state)")
    channels.add_column("channel id", justify="right")
    for name in columns:
        channels.add_column(name, justify="right")
    channels.add_column("total", justify="right")
    for label in sorted(per_channel, key=lambda x: (x == "-", int(x) if x != "-" else 0)):
        counts = per_channel[label]
        channels.add_row(
            label, *(str(counts[n]) for n in columns),
            str(sum(counts[n] for n in _DRY_OUTCOMES)),
        )
    console.print(channels)

    plan = Table(title="fetch-from-list: segment plan (list order)")
    for column in ("segment", "priority", "channel id", "rows", "posts calls", "declared GB"):
        plan.add_column(column, justify="right" if column != "priority" else "left")
    for i, seg in enumerate(segments, start=1):
        plan.add_row(
            str(i), seg.priority or "-",
            str(seg.channel_id) if seg.channel_id is not None else "(by handle)",
            str(len(seg.rows)), str(-(-len(seg.msg_ids) // GET_MESSAGES_BATCH)),
            _gb(sum(c.declared_bytes or 0 for c in seg.rows)),
        )
    console.print(plan)


def _print_summary(summary: FetchSummary) -> None:
    table = Table(title="fetch-from-list: result")
    table.add_column("outcome")
    table.add_column("rows", justify="right")
    for name, n in sorted(summary.counts.items()):
        table.add_row(name, str(n))
    table.add_row("bytes downloaded", str(summary.bytes_downloaded))
    console.print(table)


async def _run_fetch(
    settings, profile, store, classified, log, report_path, with_media, excluded_ids
):
    try:
        secrets = composition.build_secrets(profile)
        gateway = await composition.build_gateway(settings, secrets, profile, store)
        checks = [] if settings.unsafe else await run_doctor(gateway, settings)
    except BaseException:
        # The report is written in every case, including auth/keychain and
        # doctor-preflight failures.
        write_report(report_path, store, initial_results(classified))
        raise
    if not settings.unsafe and doctor_blocks(checks):
        write_report(report_path, store, initial_results(classified))
        console.print(
            "[red]doctor preflight failed[/] — refusing to fetch. "
            "Run `paperboy doctor` for details, or pass --unsafe to override."
        )
        raise typer.Exit(code=1)
    return await fetch_from_list(
        gateway, store, settings, classified, log, profile=profile, report_path=report_path,
        with_media=with_media, excluded_ids=excluded_ids,
    )


@app.command(name="fetch-from-list")
def fetch_from_list_cmd(
    list_file: Annotated[
        Path, typer.Argument(metavar="LIST", help="CSV with a `uri` column, or one URI per line.")
    ],
    profile: str = typer.Option("default", "--profile"),
    no_media: bool = typer.Option(
        False, "--no-media",
        help="Fetch and store the listed posts only; download no media.",
    ),
    media_max_mb: int = typer.Option(
        None, "--media-max-mb", min=1, help="Skip any file larger than this many MB."
    ),
    media_min_free_gb: float = typer.Option(
        None, "--media-min-free-gb", min=0.0,
        help="Stop when free disk, minus the next file's declared size, would fall below "
             "this many GB (default 5).",
    ),
    report: Annotated[
        Path | None,
        typer.Option(
            "--report",
            help="Where to write the per-row report CSV "
                 "(default <data_dir>/<profile>/fetch-from-list-<timestamp>.csv).",
        ),
    ] = None,
    exclude_target: Annotated[
        list[str] | None,
        typer.Option(
            "--exclude-target",
            help="Never fetch rows of this channel (repeatable; same forms as "
                 "`reproject`: handle or channel id; a linked group follows its parent). "
                 "Those rows are reported `excluded`.",
        ),
    ] = None,
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help=(
            "Classify and print the plan; no Telegram, no keychain, no report. "
            "With a bucket store: read-only metadata GETs (needs ADC)."
        ),
    ),
    media_store: str = typer.Option(None, "--media-store", help=_MEDIA_STORE_HELP),
    max_rpc: int = typer.Option(None, "--max-rpc"),
    unsafe: bool = typer.Option(False, "--unsafe", help="Skip the doctor preflight gate."),
    pacing_factor: float = typer.Option(None, "--pacing-factor", min=1.0, help=_PACING_HELP),
    max_flood_sleep: int = typer.Option(None, "--max-flood-sleep", min=0, help=_FLOOD_HELP),
) -> None:
    """Fetch the posts of an ordered list of message URIs across channels, then
    their media (resumable; every input row gets an outcome in the report)."""
    try:
        rows = parse_media_list(list_file)
    except (MediaListError, OSError) as exc:
        console.print(f"[red]{list_file}: {exc}[/]")
        raise typer.Exit(code=1) from None

    overrides: dict[str, object] = {}
    if media_max_mb is not None:
        overrides["media_max_mb"] = media_max_mb
    if media_min_free_gb is not None:
        overrides["media_min_free_gb"] = media_min_free_gb
    if max_rpc is not None:
        overrides["max_rpc_per_run"] = max_rpc
    if pacing_factor is not None:
        overrides["pacing_factor"] = pacing_factor
    if max_flood_sleep is not None:
        overrides["flood_sleep_threshold"] = max_flood_sleep
    if media_store is not None:
        overrides["media_store"] = media_store
    if unsafe:
        overrides["unsafe"] = True
    settings = _load_settings_or_exit(profile, overrides)

    configure_logging(profile_dir(settings, profile) / "paperboy.log", console=True)
    log = logging.getLogger("paperboy.cli")
    with composition.build_store(settings, profile) as store:
        try:
            excluded_ids = excluded_channel_ids(store, exclude_target or [])
        except MediaListError as exc:
            console.print(f"[red]{escape(str(exc))}[/]")
            raise typer.Exit(code=1) from None
        if excluded_ids:
            log.info("fetch-from-list: excluding %d channel id(s): %s",
                     len(excluded_ids), sorted(excluded_ids))
        try:
            classified = classify_rows(
                store, rows, media_store=build_media_store(settings, profile),
                excluded_ids=excluded_ids,
            )
        except MediaStoreError as exc:
            # A bucket dry run asks GCS whether it holds each candidate (metadata only).
            console.print(f"[red]cannot reach the media store: {escape(str(exc))}[/]")
            raise typer.Exit(code=1) from None
        segments = plan_segments(classified)
        _print_plan(classified, segments)
        if dry_run:
            return

        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        report_path = report or profile_dir(settings, profile) / f"fetch-from-list-{stamp}.csv"
        try:
            # Fail before any segment, not after hours of downloading.
            report_path.open("w", encoding="utf-8").close()
        except OSError as exc:
            console.print(f"[red]cannot write the report {report_path}: {exc}[/]")
            raise typer.Exit(code=1) from None

        if segments:
            summary = _run_async_or_exit(
                _run_fetch(
                    settings, profile, store, classified, log, report_path, not no_media,
                    excluded_ids,
                )
            )
        else:
            summary = asyncio.run(
                fetch_from_list(
                    None, store, settings, classified, log,
                    profile=profile, report_path=report_path, with_media=not no_media,
                )
            )

    _print_summary(summary)
    console.print(f"report: {report_path}")
    if summary.stop_reason:
        console.print(f"[red]stopped early: {summary.stop_reason}[/] — re-run to resume.")
    if not summary.complete:
        raise typer.Exit(code=1)


@app.command()
def status(
    target: str = typer.Argument(None),
    profile: str = typer.Option("default", "--profile"),
) -> None:
    """Summarize what's stored for TARGET, or the whole profile if TARGET is omitted."""
    settings = _settings_with_overrides(profile)
    with composition.build_store(settings, profile) as store:

        def count(sql: str, params: tuple = ()) -> int:
            return store.conn.execute(sql, params).fetchone()[0]

        channel_id = None
        if target:
            parsed = _parse_target_or_exit(target)
            channel_id = _find_channel_id(store, parsed)
            if channel_id is None:
                console.print(f"[yellow]No local data for {target!r} yet — run `collect` first.[/]")
                raise typer.Exit(code=1)

        title = (
            f"paperboy status — {target}" if target else f"paperboy status — profile {profile!r}"
        )
        table = Table(title=title)
        table.add_column("metric")
        table.add_column("count")
        if channel_id is not None:
            pattern = f"tg:msg:{channel_id}/%"
            messages_n = count("SELECT count(*) FROM messages WHERE channel_id=?", (channel_id,))
            revisions_n = count(
                "SELECT count(*) FROM message_revisions WHERE message_uri LIKE ?", (pattern,)
            )
            tombstones_n = count(
                "SELECT count(*) FROM message_tombstones WHERE message_uri LIKE ?", (pattern,)
            )
            table.add_row("messages", str(messages_n))
            table.add_row("revisions", str(revisions_n))
            table.add_row("tombstones", str(tombstones_n))
        else:
            table.add_row("channels", str(count("SELECT count(*) FROM channels")))
            table.add_row("messages", str(count("SELECT count(*) FROM messages")))
            table.add_row("peers", str(count("SELECT count(*) FROM peers")))
            table.add_row("edges", str(count("SELECT count(*) FROM edges")))
            table.add_row("users", str(count("SELECT count(*) FROM users")))
            table.add_row("participants", str(count("SELECT count(*) FROM participants")))
        console.print(table)


@app.command(name="export")
def export_cmd(
    target: str,
    format: str = typer.Option("jsonl", "--format"),
    out: str = typer.Option(None, "--out"),
    profile: str = typer.Option("default", "--profile"),
) -> None:
    """Export TARGET's stored data. Only --format jsonl exists in core v1."""
    if format != "jsonl":
        console.print(
            f"[red]--format {format!r} is Phase 2 (csv/rdf/datasette); only jsonl exists.[/]"
        )
        raise typer.Exit(code=1)

    settings = _settings_with_overrides(profile)
    parsed = _parse_target_or_exit(target)
    with composition.build_store(settings, profile) as store:
        channel_id = _find_channel_id(store, parsed)
        if channel_id is None:
            console.print(f"[red]No local data for {target!r}. Run `collect` first.[/]")
            raise typer.Exit(code=1)
        out_dir = Path(out) if out else profile_dir(settings, profile) / "export"
        counts = export_jsonl(store, channel_uri(channel_id), out_dir)

    table = Table(title=f"export {target} -> {out_dir}")
    table.add_column("file")
    table.add_column("rows")
    for name, n in counts.items():
        table.add_row(f"{name}.jsonl", str(n))
    console.print(table)


def _out_profile_store_path(settings: Settings, profile: str, name: str) -> Path:
    """`<data_dir>/<name>/paperboy.sqlite` for `--out-profile`, or exit 1.

    The name becomes a directory under the data dir, so it must be a plain
    name (no separators, not `.`/`..`), differ from the source profile, and
    must not already hold a store — a split never merges into an existing
    profile."""
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        console.print(f"[red]--out-profile {name!r} is not a plain profile name[/]")
        raise typer.Exit(code=1)
    if name == profile:
        console.print(f"[red]--out-profile must differ from the source --profile ({profile!r})[/]")
        raise typer.Exit(code=1)
    path = profile_dir(settings, name) / "paperboy.sqlite"
    if path.exists():
        console.print(
            f"[red]profile {name!r} already has a store at {path} — pick a fresh "
            "--out-profile or move it aside[/]"
        )
        raise typer.Exit(code=1)
    return path


@app.command()
def reproject(
    profile: str = typer.Option("default", "--profile"),
    out: str = typer.Option(
        None, "--out",
        help="Target DB path (default <data_dir>/<profile>/paperboy.reprojected.sqlite). "
             "The source DB is never touched.",
    ),
    phases: str = typer.Option(
        None, "--phases",
        help="Comma-separated phase subset; default: auto-detected from the raw log.",
    ),
    include_target: Annotated[
        list[str] | None,
        typer.Option(
            "--include-target",
            help="Replay ONLY this target (@username, username or channel id; repeatable). "
                 "Its linked discussion group follows it. See 'Splitting a mixed profile'.",
        ),
    ] = None,
    exclude_target: Annotated[
        list[str] | None,
        typer.Option(
            "--exclude-target",
            help="Replay everything EXCEPT this target (repeatable; exclusive with "
                 "--include-target).",
        ),
    ] = None,
    out_profile: str = typer.Option(
        None, "--out-profile",
        help="Write the output to <data_dir>/<name>/paperboy.sqlite and copy the media it "
             "references into <data_dir>/<name>/media/ (exclusive with --out).",
    ),
) -> None:
    """Rebuild all projections from raw_records into a fresh DB — offline,
    no Telegram, no keychain; a bucket receipt is read back read-only (needs ADC,
    allow-listed buckets only). See docs/features/reproject.md."""
    settings = _settings_with_overrides(profile)
    phase_list = phases.split(",") if phases else None
    include_target, exclude_target = include_target or [], exclude_target or []
    if include_target and exclude_target:
        console.print("[red]--include-target and --exclude-target are mutually exclusive[/]")
        raise typer.Exit(code=1)
    if out_profile is not None and out:
        console.print("[red]--out and --out-profile are mutually exclusive[/]")
        raise typer.Exit(code=1)
    if out_profile is not None:
        out_path = _out_profile_store_path(settings, profile, out_profile)
    elif out:
        out_path = Path(out)
    else:
        out_path = profile_dir(settings, profile) / "paperboy.reprojected.sqlite"

    target_filter: TargetFilter | None = None

    def _check_targets(source: ReplaySource) -> None:
        # Runs before the output exists: an unknown target must write nothing.
        nonlocal target_filter
        target_filter = resolve_target_filter(source, include_target, exclude_target)

    try:
        source, out_store = composition.build_reproject(
            settings, profile, out_path, check=_check_targets
        )
    except (composition.ConfigError, ReprojectError) as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=1) from None
    # The log lives beside the output, never in the source profile: replay
    # must not write there (#64 §2.3, a read-only profile must work).
    # `build_reproject` has already validated the profile and created
    # `out_path`'s parent, so this cannot manufacture `data/<typo>/`.
    configure_logging(out_path.with_name(out_path.name + ".log"), console=True)
    log = logging.getLogger("paperboy.cli")
    if source.opened_immutable:
        log.warning(
            "source DB could not be opened plain read-only (read-only directory, WAL "
            "sidecars absent); opened with immutable=1 - it must not be written concurrently"
        )
    try:
        with source, out_store:
            summary = asyncio.run(
                reproject_run(
                    source, out_store, settings, profile, phase_list, log,
                    target_filter=target_filter, out_profile=out_profile,
                )
            )
    except (ReprojectError, ReprojectSourceError) as exc:
        console.print(f"[red]{exc}[/]")
        out_path.unlink(missing_ok=True)  # don't leave a half-made target behind
        raise typer.Exit(code=1) from None
    except Exception:
        # `build_reproject` already created + migrated `out_path` before this
        # block runs, so ANY failure here — not just a `ReprojectError` (a
        # corrupt/unreadable source DB raises a bare `sqlite3.DatabaseError`
        # from `resolve_targets`, found running this smoke test) — leaves a
        # half-migrated file at `out_path` behind unless we clean it up too.
        # Left in place, it would make a retry against the default --out path
        # hit "refusing to overwrite" for a file that was never actually
        # usable. Unlike the ReprojectError branch, an unexpected failure is
        # not translated into a clean message — the real traceback is more
        # useful for a genuinely unanticipated error than a shim message.
        out_path.unlink(missing_ok=True)
        raise

    for raw_target, results in summary.results.items():
        table = Table(title=f"reproject {raw_target} -> {out_path}")
        table.add_column("phase")
        table.add_column("counts")
        table.add_column("stopped")
        for r in results:
            table.add_row(r.name, str(r.counts), r.stopped or "-")
        console.print(table)

    diff = Table(title="row counts — source vs reprojected")
    diff.add_column("table")
    diff.add_column("source")
    diff.add_column("reprojected")
    for name, (src_n, out_n) in summary.table_counts.items():
        diff.add_row(name, str(src_n), str(out_n))
    console.print(diff)


@app.command()
def watch(
    target: str,
    profile: str = typer.Option("default", "--profile"),
    interval: int = typer.Option(60, "--interval"),
) -> None:
    """Not in core v1 — Phase 2."""
    del target, profile, interval
    console.print("[yellow]`watch` is not implemented in core v1 (Phase 2).[/]")
    raise typer.Exit(code=1)


@app.command()
def lookup(
    kind: str = typer.Argument(..., help="Only 'phone' is planned, and it's Phase 2."),
    value: str = typer.Argument(None),
    profile: str = typer.Option("default", "--profile"),
    i_understand_the_risk: bool = typer.Option(False, "--i-understand-the-risk"),
) -> None:
    """Not in core v1 — Phase 2, flag-gated."""
    del kind, value, profile, i_understand_the_risk
    console.print("[yellow]`lookup` is not implemented in core v1 (Phase 2, flag-gated).[/]")
    raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
