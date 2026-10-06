# Media storage batch — overview and run order

**Status:** draft for Gate A, 2026-09-28. **Operator goal:** pull ~146 GB of
catalogued media (1,620 posts, 11 channels, files up to 2.4 GB) into the
content-addressed archive and the GCS bucket, preferably from the OSINT VM
(2 vCPU, 7.7 GiB RAM, 38 GB free disk), falling back to the Mac — into a
`default` profile that first has an unrelated investigation split out.

This file is the index. Each feature has its own spec, written so it can be
handed to one implementing agent on its own.

| Spec | Issue | Run | Why it's needed |
|---|---|---|---|
| # | Spec | Issue | Why it's needed |
|---|---|---|---|
| 1 | [`…-pacing-safety-factor-design.md`](2026-09-28-pacing-safety-factor-design.md) | #69 | Conservative pacing first, so every later live smoke runs under it |
| 2 | [`…-media-relative-paths-design.md`](2026-09-28-media-relative-paths-design.md) | #62 | Portable media keys — prerequisite for streaming, the split, the VM and GCS |
| 3 | [`…-media-streaming-design.md`](2026-09-28-media-streaming-design.md) | #64 (+#53) | 2.4 GB files must not sit in RAM; the disk must not fill silently |
| 3b | [`2026-09-29-replay-lookup-performance-design.md`](2026-09-29-replay-lookup-performance-design.md) | #75 | Added 2026-09-29: replay lookups scan the whole run (~13 s each, days for a full media replay); the #70 split is two full replays |
| 4 | [`…-profile-split-design.md`](2026-09-28-profile-split-design.md) | #70 | Get the unrelated investigation out of `default` before adding 146 GB to it |
| 5 | [`…-media-list-fetch-design.md`](2026-09-28-media-list-fetch-design.md) | #68 | The pull is driven by a cross-channel CSV |
| — | [`…-media-gcs-backend-design.md`](2026-09-28-media-gcs-backend-design.md) | #63 | Daytime: needs IAM, a VM proxy and a VM smoke with the operator |

## Run order — strictly sequential

**Status 2026-09-29:** #69 and #62 merged into `dev/media-storage`. #64
escalated after three review rounds. Its spec was then amended (§2.3: replay
leaves the source untouched; §4.1: small-fixture replay smoke), and it
resumes from its escalated branch. #75 was added to the chain after #64
(operator decision, 2026-09-29). Order: **#64 → #75 → #70 → #68**.

**Docs are part of every feature's DoD:** the feature doc under
`docs/features/`, the README (commands, flags, config table, documentation
list), `CLAUDE.md` (commands and status), `docs/data-model.md` for any schema
change, and `docs/how-it-works.md` wherever a concept it explains changes.

Features 1–5 run **one at a time, in the order above**, each as a
`single-feature-run` whose branch is cut from `dev/media-storage` *after*
the previous feature has merged into it. Each feature's PR targets
`dev/media-storage`; the operator merges `dev/media-storage → main` (Gate B)
once, in the morning. Sequential means no two agents ever share the
Telegram session, and each feature builds on the finished code of the one
before it (e.g. #64 calls #62's `media_key` directly — there is no merge
seam to manage).

If a feature exhausts its retry budget (K=3), it escalates on its issue and
**the chain stops there** — later features depend on earlier ones.

- **Daytime:** #63 (security reviewer on; ops prerequisites in its spec).
- **Then the operator procedure** in the #70 spec (split, verify, swap), a
  `fetch-media --dry-run` on the cleaned list, and the pull.
- **Mac fallback** is possible after 1–5: the data volume had 190 GiB free
  on 2026-09-28, so the full pull fits but leaves ~44 GiB; P1+P2 (~90 GB) is
  comfortable. Set the #64 free-disk floor accordingly.

## Live smoke protocol (agents may run limited live smokes)

The operator authorizes implementing agents to run the live smokes named in
each spec's Definition of Done, against Telegram with the collecting account,
**within these limits**. Anything outside them is pending for the operator.

1. **Never the real store.** Work in a scratch data dir outside the repo
   (the handoff prompt names it). Snapshot the store with SQLite's online
   backup, never `cp` of a live file:
   `sqlite3 <real>/default/paperboy.sqlite ".backup '<scratch>/default/paperboy.sqlite'"`
   (or Python's `sqlite3.Connection.backup`), then run with
   `PAPERBOY_DATA_DIR=<scratch>`. The keychain session is looked up by
   profile name, so `--profile default` still authenticates. Offline smokes
   that need existing media files (#70) may symlink `<scratch>/default/media`
   to the real media dir and must show its file count and total size
   unchanged before/after.
2. **Read-only, guarded.** Never `--unsafe`, `--join` or `--profiles`, and
   never configure a proxy. The operator's machine protects Telegram egress
   with a system VPN instead (operator decision 2026-09-28: with the VPN up,
   a proxy adds nothing), so run every live command with
   `PAPERBOY_REQUIRE_PROXY=false` — `doctor` then reports the proxy check as
   disabled and otherwise gates as usual. If `doctor` blocks for any other
   reason, do not override — record the smoke as pending with the doctor
   output.
3. **VPN egress check before every live invocation.** Telegram traffic must
   leave through the VPN tunnel. Immediately before each live command, run:
   ```
   for ip in 149.154.167.51 91.108.56.130; do printf '%s -> ' $ip; route -n get $ip | awk '/interface:/{print $2}'; done
   ```
   (two public Telegram data-centre addresses). Both must route via a tunnel
   interface (`utun*`, `ipsec*` or `ppp*`). If either shows a physical
   interface (`en*`) or the lookup fails, **don't run the command** — the VPN
   is down — and treat it as a stop condition (§5). Paste the check output
   with each smoke transcript.
4. **Caps per feature:** ≤ 5 live invocations; each with `--max-rpc 60` or
   lower; ≤ 3 media files and ≤ 3 GB downloaded in total; only channels
   already in the store, and never the investigation being split out (#70).
5. **Stop conditions:** any `FLOOD_WAIT` over 60 s, `PEER_FLOOD`,
   `FROZEN_METHOD_INVALID`, an auth/session error, or a failed VPN egress
   check (§3) → stop, record it on
   the feature's issue, and make **no further live calls for the rest of the
   night** (later features mark their live smokes pending). Never retry to
   "get a clean run".
6. **Public repo — redact.** In commits, PR bodies and issue comments, write
   channels as `@<channel>` and message ids as `<id>`; never paste usernames,
   channel ids, titles or message text. Keep the unredacted transcript in the
   scratch dir and reference its filename.
7. Scratch outputs stay in the scratch dir for the operator's review — don't
   delete them.

## Shared constraints (from CLAUDE.md; non-negotiable)

- Read-only against Telegram; every RPC goes through `Budget` — no collector
  calls the gateway raw.
- Raw first: `raw_records` stays append-only. Old payloads are never
  rewritten; the projection normalizes them.
- Storage and guardrails are settled decisions: #62 lands ADR-0007, #63
  ADR-0008, #69 amends ADR-0003 — before code.
- Tests: `uv run pytest -q` (point `TMPDIR`/`--basetemp` at a volume with
  free space — a near-full root disk causes spurious SQLite I/O errors),
  `uv run ruff check`, `uv run pyright` — all green per task.
- Commits end with `Co-Authored-By:` only — **no `Claude-Session:` lines**
  (history was scrubbed of them on 2026-09-28). Commit email is the GitHub
  noreply address (set repo-locally).
- Never name real collection targets, their ids, or local machine paths in
  anything committed — this repo is public.
- Implementation worktrees start from `main`: these specs must be merged to
  `main` (or the dev branch cut from them) before launch.

## Roles

Planner: Fable. Implementers: Sonnet 5.5 (confirm the `sonnet` alias
resolves to 5.5 before launch — on 2026-09-28 it still resolved to Sonnet 5).
Reviewers: Opus 5.5 (adversarial + correctness; security reviewer on #63).

## Data notes for the operator

- The current download list contains **40 rows from one linked discussion
  group** belonging to the investigation being split out (#70). Drop them
  when the list is regenerated from the clean store; paperboy would report
  them `unresolvable` anyway.
