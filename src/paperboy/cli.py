"""The `paperboy` Typer CLI — thin wrappers over `app.py` (composition root),
`recipes.collect_channel`, `export.jsonl.export_jsonl`, and `doctor.run_doctor`.
Every command is `asyncio.run`-based since everything underneath is async.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, cast

import typer
from rich.console import Console
from rich.table import Table

from paperboy import app as composition
from paperboy.config import Settings, load_settings, profile_dir
from paperboy.doctor import doctor_blocks, run_doctor
from paperboy.export.jsonl import export_jsonl
from paperboy.ids import channel_uri
from paperboy.logging_setup import configure_logging
from paperboy.recipes import collect_channel
from paperboy.targets import parse_target

app = typer.Typer(
    add_completion=False,
    help="Local, read-only Telegram channel OSINT collector. See docs/opsec.md first.",
)
console = Console()


def _settings_with_overrides(profile: str, **overrides: object) -> Settings:
    clean = {k: v for k, v in overrides.items() if v is not None}
    return load_settings(profile, clean)


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


def _find_channel_id(store, username: str) -> int | None:
    row = store.conn.execute(
        "SELECT id FROM channels WHERE username = ?", (username.lstrip("@"),)
    ).fetchone()
    return row["id"] if row else None


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
) -> None:
    """Opsec preflight: proxy, session age, 2FA, privacy keys, profile minimalism."""
    settings = _settings_with_overrides(profile)
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
        None, "--phases", help="Comma-separated: channel,history,graph,web,media"
    ),
    join: bool = typer.Option(False, "--join", help="Not implemented in core v1 (Phase 2)."),
    media: bool = typer.Option(
        False, "--media", help="Also download message media (opt-in; off by default)."
    ),
    web: bool = typer.Option(
        False, "--web", help="Also capture t.me/s + Wayback snapshots over HTTP (opt-in)."
    ),
    profile_budget: int = typer.Option(None, "--profile-budget"),
    max_rpc: int = typer.Option(None, "--max-rpc"),
    unsafe: bool = typer.Option(False, "--unsafe", help="Skip the doctor preflight gate."),
) -> None:
    """Collect channel metadata, full message history, and the discovery/
    relationship graph for TARGET."""
    if join:
        console.print(
            "[yellow]--join is accepted but not implemented in core v1 — "
            "paperboy never joins.[/]"
        )

    overrides: dict[str, object] = {}
    if profile_budget is not None:
        overrides["profile_budget"] = profile_budget
    if max_rpc is not None:
        overrides["max_rpc_per_run"] = max_rpc
    settings = load_settings(profile, overrides)

    configure_logging(profile_dir(settings, profile) / "paperboy.log", console=True)
    log = logging.getLogger("paperboy.cli")
    parsed_target = parse_target(target)
    secrets = composition.build_secrets(profile)
    phase_list = phases.split(",") if phases else None
    _dependent_phases = [
        p for p in ("history", "graph", "media", "web") if phase_list and p in phase_list
    ]
    if phase_list is not None and _dependent_phases and "channel" not in phase_list:
        # `channel` populates `CollectContext.input_channel`/`channel_id` (the
        # channel's numeric id + access_hash) for every later collector in
        # *this run* — it is per-process context, not reloaded from
        # `channels` even if a prior run already stored that channel, since
        # `access_hash` can rotate and isn't persisted. Selecting `history`
        # `history`/`graph`/`media` each need `input_channel`/`channel_id`
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
                phase_list, log, unsafe, media, web,
            )
        )

    table = Table(title=f"collect {target}")
    table.add_column("phase")
    table.add_column("counts")
    table.add_column("stopped")
    for r in results:
        table.add_row(r.name, str(r.counts), r.stopped or "-")
    console.print(table)


async def _run_collect(
    settings, secrets, profile, store, target, phase_list, log, unsafe, media, web
):
    gateway = await composition.build_gateway(settings, secrets, profile, store)
    if not unsafe:
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
            parsed = parse_target(target)
            channel_id = _find_channel_id(store, parsed.value)
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
    parsed = parse_target(target)
    with composition.build_store(settings, profile) as store:
        channel_id = _find_channel_id(store, parsed.value)
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
