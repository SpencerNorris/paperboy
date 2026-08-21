# ADR-0002: Storage — SQLite primary, raw-first, triple-shaped edges

**Status:** accepted (2026-08-20)

## Problem
Store an exhaustive, re-queryable, forensically-sound channel corpus that
survives TL schema growth, supports incremental sync, versions edits, and
dovetails with a Datasette-based analysis workflow — without prematurely
committing to a graph database.

## Options
- SQLite (normalised) + raw TL JSON, graph as a projection.
- A triple store / RDF-native DB (SPARQL).
- Flat JSONL/CSV (what most prior-art tools do).

## Decision
**SQLite is the system of record** (WAL, explicit migrations). Every TL object
is written verbatim to `raw_records` **before** any normalisation; normalised
entity/history tables are a projection that can be rebuilt from raw after a
layer bump. `edges` is **triple-shaped** — `(subject_uri, predicate,
object_uri, observed_at, tier, source_raw_id, evidence_json)` — and every
entity has a URI id (`tg:user:123`), so RDF/Turtle and GraphML are *export
projections*, not the primary store. Datasette-friendly: plain columns, JSON
text for raw, FTS5 over message text, `metadata.json`, per-media SHA-256 +
`custody_log`.

## Why not RDF-native
The corpus is mostly tabular by volume (messages × 30+ fields needing FTS,
media hashes, counter time-series); the graph part is real but shallow (1–2
hops), which SQL joins handle trivially. Provenance ("seen at T, at tier X,
from record R") is three columns in SQL but needs reification/RDF-star in a
triple store. The target toolchain (FTS5, Datasette) is SQLite. We keep the triple shape so nothing is lost.

## Consequences
- A layer bump means re-parsing `raw_records`, not re-scraping.
- Counters, edits, deletions, and participants are append-only history, never
  overwritten (see ADR-0004).
- Encryption is by encrypted volume, not SQLCipher (ADR-0002a below).

### 0002a — encryption
SQLCipher is **not** used: Datasette opens DBs via stdlib `sqlite3`, which
cannot read SQLCipher files, and no maintained keyed-Datasette path was
investigated. Data lives on an encrypted volume instead. Revisit only if an
at-rest-encrypted, Datasette-served store becomes a hard requirement.
