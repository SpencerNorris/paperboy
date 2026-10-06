# Implementation plan — #69 conservative pacing and patient flood handling

(Planner: Fable. Commit this file as `docs/superpowers/plans/2026-09-28-pacing-safety-factor.md` in your first commit.)

Branch: cut `feat/pacing-factor` from `origin/dev/media-storage`. Per task: `uv run pytest -q`, `uv run ruff check`, `uv run pyright` green before committing.

Spec: `docs/superpowers/specs/2026-09-28-pacing-safety-factor-design.md`. Code touched: `src/paperboy/budget.py`, `config.py`, `cli.py`, `app.py`, `collectors/web.py`, `store/migrations/0005_*.sql`.

## Ambiguities / contradictions and recommended resolutions

1. **Flood decision on `applied` vs `classify`'s raw `seconds`.** `errors.classify` (`src/paperboy/errors.py:152`) returns RETRY/PHASE_STOP by comparing raw `seconds <= threshold`; spec §3.3 says the ceiling test is `applied > ceiling`. Resolution: `Budget.call` computes `applied` itself and decides sleep-vs-stop on `applied <= settings.flood_sleep_threshold`, ignoring `classify`'s RETRY/PHASE_STOP split for flood exceptions (still using `classify` for SKIP/HARD_STOP/network-RETRY). Leave `classify`'s signature and `test_budget.py:13` untouched.
2. **"`max_rpc_per_run` still counts every attempt" (spec §3.3)** is not what the code does today (`budget.py:144-148` increments once per `call()`). Resolution: move the cap check + increment inside the attempt loop so every attempt counts. Add a test.
3. **Flag on `auth`.** `auth` never builds a `Budget` (`cli.py:77`), so `--pacing-factor` there would be a dead flag. Resolution: add the flags to `collect` and `doctor` only; state this in the PR body. `fetch-media` (#68) will inherit via `Settings`.
4. **"A 4th transient failure goes through `classify` as today."** Resolution: 4th consecutive transient error → `PhaseStop(str(exc))`, cause chained.
5. **"Update the progress display" during long sleeps.** `Progress._beat` (`progress.py:105`) already fires every 5 s at any await point; Budget's own 60 s INFO heartbeat satisfies the spec. Do not thread `Progress` into `Budget`.
6. **Spec smoke vs overview stop rule.** Run every live smoke with `--max-flood-sleep 60` so the tool stops instead of sleeping through a >60 s wait (overview §5).
7. **Float ceil.** `applied = (seconds * 11 + 9) // 10 + 5` (== `ceil(s × 1.1) + 5`; 20→27, 3→9, 1800→1985, 7200→7925).
8. **Existing tests that legitimately change** (not weakening): `test_budget.py:97` `slept == [3]` → `[9]`; `:238-258` `3 in slept` → `9 in slept`; `:209-234` per-method test — pass `{"pacing_factor": 1.0}`; `test_config.py:39` `flood_sleep_threshold == 60` → `3600`.

## Task 0 — ADR-0003 amendment (before any code)

`docs/adr/0003-guardrails.md`: status `accepted (2026-08-20); amended 2026-09-28 (#69)`. Append `## Amendment (2026-09-28, #69): conservative pacing and patient flood handling` with Context / Options considered (a: keep 1 s + 60 s stop; b: multiplier on server waits; c: factor on our assumptions + margin on server waits + sleep-to-ceiling — chosen) / Decision (spec §1 policy bullets; base intervals stated as assumptions: `contacts.resolveUsername` 5 s, all else 1 s; `applied = ceil(s×1.1)+5`; ceiling = `flood_sleep_threshold`, default 3600; 3 flood retries; 3 transient retries at 5/10/20 s × factor; `PEER_FLOOD`/`FROZEN_METHOD_INVALID` unchanged; Telethon `flood_sleep_threshold: 0` stays) / Consequences / Notes. Follow `~/.claude/rules/adr-format.md`.
Commit: `docs(adr): amend ADR-0003 — pacing safety factor and patient flood handling (#69)`

## Task 1 — Settings: `pacing_factor`, ceiling default 3600

Tests first (`tests/test_config.py`): `test_pacing_factor_default_and_bounds` (default 2.0; 1.0 ok; 0.5 → ValidationError); `test_pacing_factor_env_and_cli_precedence` (`PAPERBOY_PACING_FACTOR=3` → 3.0; override 1.5 beats env); update `test_defaults_match_spec` → 3600.
Code (`config.py`): `flood_sleep_threshold: int = Field(default=3600, ge=0)`; `pacing_factor: float = Field(default=2.0, ge=1.0)`, both commented.
Commit: `feat(config): pacing_factor setting; flood_sleep_threshold default 3600 (#69)`

## Task 2 — Budget: every interval × factor; resolveUsername 5 s base; pacing summary line

Tests first (`tests/test_budget.py`, fake clock + recording sleeper pattern from `:208-234`):
- `test_default_factor_doubles_the_default_interval` → `slept == [2.0]`.
- `test_resolve_username_base_is_five_seconds_scaled` → `[10.0]`.
- `test_factor_one_reproduces_previous_intervals` → `[1.0]` / `[5.0]`.
- `test_method_intervals_are_scaled_too`: `{"users.getFullUser": 2.5}` → `[5.0]`.
- `test_describe_pacing_lists_factor_and_non_default_methods`.
- Update `test_per_method_interval_paces_only_that_method` to pass `{"pacing_factor": 1.0}`.
Code (`budget.py`): `BASE_METHOD_INTERVALS = {"contacts.resolveUsername": 5.0}` (commented as an assumption); `self._factor`; `self._method_intervals = {**BASE_METHOD_INTERVALS, **(method_intervals or {})}`; `effective_interval(method)`; `describe_pacing() -> str`. `app.py:37-42` unchanged. Update docstrings.
Commit: `feat(budget): scale every interval by pacing_factor; resolveUsername 5 s base (#69)`

## Task 3 — Flood margin + `flood_log.applied_seconds` (migration)

Tests first: `tests/test_store_migrations.py::test_0005_flood_applied_column`; `test_flood_applied_seconds_is_ceil_ten_percent_plus_five` (20→27, 3→9, 0→5, 1800→1985, 7200→7925); update `test_short_flood_wait_sleeps_and_retries` (`FakeFlood(3)` → `slept == [9]`, row `seconds=3, applied_seconds=9`); `test_flood_wait_20_sleeps_27_and_records_both` (`until == now + 27`).
Code: `src/paperboy/store/migrations/0005_flood_applied.sql`: `ALTER TABLE flood_log ADD COLUMN applied_seconds INTEGER;` (commented; NULL on pre-#69 rows). `budget.py`: margin constants (commented), `flood_applied_seconds(seconds) -> int`, `_record_flood(method, seconds, applied)` with `until = now + applied`.
Commit: `feat(store,budget): FLOOD_WAIT margin ceil(s×1.1)+5; flood_log.applied_seconds (#69)`

## Task 4 — Rewrite `Budget.call` as an attempt loop

Write all tests first, then implement once (`caplog.set_level(logging.INFO, logger="paperboy.budget")`):
- `test_long_flood_under_ceiling_is_slept_through_with_heartbeat`: `FakeFlood(1800)` → succeeds, no PhaseStop; `sum(slept) == 1985`; chunks ≤ 60; ≥ 30 INFO heartbeats with method/remaining/total; row `1800/1985`.
- `test_short_flood_has_no_heartbeat`.
- `test_flood_over_ceiling_is_phase_stop_with_applied_cooldown`: `FakeFlood(7200)` → PhaseStop; `until == now + 7925`; `slept == []`.
- `test_ceiling_is_compared_against_applied_not_raw`: threshold 60; `FakeFlood(55)` → PhaseStop; `FakeFlood(50)` → slept + retried.
- Update `test_long_flood_wait_raises_phase_stop_and_persists_cooldown` (assert `applied_seconds`).
- `test_three_consecutive_floods_then_success` (4 factory calls, 3 rows, 3 WARNINGs with attempt 1..3).
- `test_four_consecutive_floods_is_phase_stop` (4 rows).
- `test_transient_errors_back_off_5_10_20_times_factor`: `[10, 20, 40]` at factor 2; `[5, 10, 20]` at 1.0; flood_log empty.
- `test_fourth_transient_error_is_phase_stop` (`__cause__` is the ConnectionError).
- `test_retry_reinvokes_the_factory_fresh_each_attempt`.
- `test_every_attempt_counts_toward_max_rpc`.
- `test_hard_stop_and_skip_are_not_retried`.
Code (`budget.py`): `MAX_FLOOD_RETRIES = 3`, `MAX_TRANSIENT_RETRIES = 3`, `TRANSIENT_BACKOFF_SECONDS = (5, 10, 20)`, `HEARTBEAT_SECONDS = 60`, module logger `paperboy.budget`. `call()`: pacing + cooldown once up front; `while True:` cap check/increment → `_last_call[method] = now` → `try: return await factory()`; flood branch (has `.seconds`): compute applied, record, stop if `applied > ceiling` or retries exhausted, else WARNING + `_sleep_with_heartbeat`; else `classify`: RETRY → backoff × factor up to 3, then `PhaseStop(...) from exc`; else raise mapped exception. `_sleep_with_heartbeat` driven by the counter, not the clock (fake clock doesn't advance); no heartbeat line after the final chunk. Delete the old second-attempt block; update module docstring.
Commit: `feat(budget): sleep through floods up to the ceiling with a heartbeat; 3 flood and 3 transient retries (#69)`

## Task 5 — Web collector interval × factor

Test: `test_web_collector_interval_scales_with_pacing_factor` (default → 2.0; factor 1.0 → 1.0; `min_interval=0.0` (reproject path) → 0.0).
Code (`collectors/web.py`): effective interval = `min_interval * ctx.settings.pacing_factor`, computed in `collect()`; update constant comment.
Commit: `feat(web): web collector pacing scales with pacing_factor (#69)`

## Task 6 — CLI flags + gateway-construction INFO line

Tests (`tests/test_cli.py`, CliRunner + monkeypatched `build_gateway`): flags reach settings (`--pacing-factor 3 --max-flood-sleep 120`); `--pacing-factor 0.5` → exit 2, no gateway built; env `PAPERBOY_PACING_FACTOR=1.5` reaches settings; `doctor` accepts the flags; `collect --help` lists both.
Code (`cli.py`): options on `collect` and `doctor` (`min=1.0` / `min=0`) → `overrides["pacing_factor"]` / `overrides["flood_sleep_threshold"]`; fix `--profile-interval` help text ("base seconds…, multiplied by --pacing-factor"). `app.build_gateway`: log `pacing: <describe_pacing()>` at INFO after constructing `Budget`.
Commit: `feat(cli): --pacing-factor and --max-flood-sleep on collect/doctor; pacing INFO line (#69)`

## Task 7 — Docs

New `docs/features/pacing.md` (format of `docs/features/collect-channel.md`). README flags + config table rows (`FLOOD_SLEEP_THRESHOLD 3600`, `PACING_FACTOR 2.0`). `CLAUDE.md` command synopsis. Optional clause in `docs/opsec.md`.
Commit: `docs(pacing): feature doc; README/CLAUDE.md flags and defaults (#69)`

## Task 8 — Definition of Done smoke

Offline: gates; `collect --help` / `doctor --help` grep for the flags; `collect @x --pacing-factor 0.5` → exit 2; `pragma table_info(flood_log)` shows `applied_seconds` on the scratch store after migration.

Live (RUN RULES protocol; this uses 2 of the 5 allowed invocations):
- Scratch store via `sqlite3 <real>/default/paperboy.sqlite ".backup '<scratch>/default/paperboy.sqlite'"`.
- VPN check before each command.
- `PAPERBOY_REQUIRE_PROXY=false PAPERBOY_DATA_DIR=<scratch> uv run paperboy doctor --profile default --max-flood-sleep 60` — must pass; otherwise PENDING with output.
- `PAPERBOY_REQUIRE_PROXY=false PAPERBOY_DATA_DIR=<scratch> uv run paperboy collect @<channel already in the store> --profile default --phases channel,history --max-rpc 30 --max-flood-sleep 60 2>&1 | tee <scratch>/smoke-69.log`. Show (redacted) the `pacing: factor=2.0 …` line and two consecutive same-method timestamps ≥ 2 s apart from the log.
- `select method, seconds, applied_seconds from flood_log order by rowid desc limit 5` — empty is fine; say so.
- Never provoke a flood. Stop conditions per RUN RULES.
Record the redacted transcript in `docs/features/pacing.md`.
Commit: `docs(pacing): live smoke transcript (#69)`

## Edge cases and failure modes to cover

- `FakeFlood(0)` → applied 5, slept, one row `0/5`; transient errors stay out of `flood_log`.
- Flood exactly at the ceiling is slept (≤); one above is a stop.
- `flood_sleep_threshold=0`: every flood → PhaseStop with cooldown persisted.
- Mixed flood/transient/flood/success within one call → succeeds; counters independent.
- A flood on attempt 2 records the cooldown based on `applied`, honoured by the next `call()`.
- HardStop/SkipAndRecord on attempt ≥ 2 propagate immediately.
- Cap hit mid-retry raises HardStop.
- Heartbeat count for exact multiples of 60 (applied 120 → 1 heartbeat).
- `describe_pacing()` never logs credentials.
- Pre-#69 rows with NULL `applied_seconds` don't break `_active_cooldown_seconds`.
- Reproject builds no Budget; `WebCollector(min_interval=0.0)` stays 0 — existing parity tests still pass.
