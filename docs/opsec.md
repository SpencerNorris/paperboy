# Operator security runbook

paperboy makes the tool's behaviour safe (read-only, passive, allow-listed,
rate-limited). The steps in this file are the **human-side** work the tool
cannot do for you. Read all of it before your first collection against a
sensitive target.

## Threat model

You cannot be anonymous *from Telegram* — it always holds the phone number and
IP history and, per its 2024 policy, may disclose phone + IP on a valid legal
request. You **can** be:
- **Invisible to targets** — reading a public channel, its discussion group,
  profiles, and similar channels is unobservable by anyone. *Joining* is the
  exposure event. Default to passive; use `--join` only when a private group
  requires it, and read what the tool prints about what the join exposes.
- **Pseudonymous to Telegram** — make the two controllable identifiers (phone,
  IP) non-attributable.
- **Silent to third parties** — the tool never queries OSINT bots, only talks
  to `t.me` and `web.archive.org`, and never dereferences URLs found in content.

## The collecting account

1. **Use a dedicated account, never your personal one.** Everything the tool
   does is done *as* that account.
2. **Phone number** (pick one):
   - **Fragment +888 anonymous number** (Telegram's sanctioned path, bought
     with TON). Caveat: fund the wallet carefully — TON transactions are public
     and a KYC-exchange-funded wallet links back to you.
   - **Prepaid SIM/eSIM bought with cash** (no KYC in the US). Receive the one
     login SMS on a **cheap dedicated device**, not your personal phone (the
     IMEI pairing is logged by the carrier), then keep the SIM powered off.
   - **Avoid** VoIP numbers (blocked/ban-prone) and bought accounts (ToS
     violation, pre-flagged, reclaimable by the SIM holder).
3. **Age the account** a few weeks with light, human-looking use before any
   bulk work. Telegram polices new accounts hardest. The tool refuses
   participant sweeps on sessions younger than `min_session_age_days` (7)
   without `--unsafe`.
4. **Harden it:** set a 2FA password (prevents SIM-swap takeover); skip or use
   a dedicated recovery email. The `.session` file **is** the account — it
   lives in the macOS Keychain, never in the repo or plaintext.
5. **Get `api_id`/`api_hash` from my.telegram.org using the research account**,
   not your personal one — it is an app credential tied to whoever requested it.

## Network

- Configure a **proxy** (`proxy = "socks5://…"` or `mtproxy://…`); `require_proxy`
  is on by default and the tool refuses to connect without one.
- **Consistency beats exoticism**: a stable exit in the number's region looks
  like a normal user; churning exits looks like a compromised account. Register
  and operate through the *same* proxy. Tor exits are often blocked. A paid VPN
  or self-hosted proxy moves trust to that provider — an honest trade-off.
- Keep `device` (model/system/app) stable and generic. We do not impersonate an
  official client.

## Handling collected content

- **Never open collected documents on your working machine.** Office files,
  PDFs, and HTML can fetch remote resources **when opened**, leaking your IP and
  timing to whoever planted them (issue #1). Open them offline or in a sandbox
  / air-gapped viewer with external content disabled. This is tapedeck
  territory — keep the corpus offline.
- The tool stores URLs and Telegram's own server-side previews but **never
  fetches** a URL found inside a message.
- If you encounter CSAM or other illegal material: do **not** download it;
  follow your jurisdiction's reporting obligations (e.g. NCMEC in the US). The
  tool records `restriction_reason` as metadata without downloading flagged
  media by default.

## Data at rest

- Keep the data directory on an **encrypted volume**. FileVault covers the
  internal disk only; confirm any external/`/Volumes` disk is APFS-encrypted.
- Logs redact credentials and reference targets by id. Exports scrub the
  collecting account's own record before you share a dataset.

## Compartmentalisation

Any group member can see *groups in common* with your account. If you collect
in several sensitive groups from one account, you link those investigations.
Use one **profile** (separate session + database) per investigation that must
stay unlinkable: `paperboy <cmd> --profile <name>`.

## Pre-flight

Run `paperboy doctor --profile <name>` before collecting. It checks the
account's privacy settings, 2FA, profile minimalism, session age, and proxy,
and blocks `collect` on failure unless you pass `--unsafe`. A failure means
fix the account, not override the check.

## What still isn't in your control

If Telegram's anti-spam heuristics decide a read pattern is abusive, they will
limit the account regardless of how anonymous it is — which is why the dedicated
account, aging, and the tool's pacing matter as much as the proxy. If you see
`PEER_FLOOD` / a limited account, stop and check @SpamBot.
