# ADR-0007: Media locations are profile-relative keys

## Status

Accepted (2026-09-29, #62).

## Context

`media.path`, `custody_log.path` and the `MediaDownload`/`AvatarDownload` raw
payloads stored `str(path)` exactly as the run built it from `settings.data_dir`.
The live store therefore mixed two forms:

| table | repo-relative `data/<profile>/media/...` | absolute `/.../data/<profile>/media/...` |
|---|---|---|
| `custody_log` | 933 | 211 |
| `media` | 609 | 146 |

Relative forms only resolve from the launching cwd; absolute forms pin the
store to one machine's mount. Both block moving the store to the VM and the
bytes to object storage (#63), and a downstream catalogue already had to stop
trusting stored paths.

## Options considered

- **a. Always absolute.** Rejected: pins the store to one machine.
- **b. Always cwd-relative.** Rejected: depends on how the run was launched.
- **c. Profile-relative key resolved at read time.** Chosen.
- **d. Store no location; derive from `sha256`.** Rejected: the extension is
  not a column, and `custody_log` records a location per write.

## Decision

A media location is a key: `media/<sha256[:2]>/<sha256><ext>`, POSIX
separators, extension case preserved (case-sensitive backends), never
containing the data dir, profile name, drive or a leading `/`.

- `paperboy.media_keys.media_key` is the single constructor and validates its
  inputs. Keys are resolved against `profile_dir(settings, profile)` at read
  time; a stored key is data and is validated before use as a path.
- Legacy values are normalised by anchoring on the sha (the filename is the
  substring starting at the sha), not by directory string surgery.
- Migration `0006_media_keys.sql` rewrites `media.path` and `custody_log.path`
  in place. `raw_records` is never modified; replay normalises old payloads.
- Rows that do not contain their own sha are left untouched, counted and
  reported with a WARNING on `Store.open`. They are never guessed.

## Consequences

- The store is portable across cwd, machine and backend; #63 prefixes the key.
- `status`/`export` opening an old store apply 0006 like every prior migration.
- Reproject's read-only source keeps legacy `media`/`custody_log` until opened
  with `Store.open`; its raw payloads are normalised on replay.
- Byte-writing mechanics are unchanged here; #64 builds on `media_key`.

## Notes

Spec `docs/superpowers/specs/2026-09-28-media-relative-paths-design.md`; plan
`docs/superpowers/plans/2026-09-28-media-relative-paths.md`; issue #62;
ADR-0002 (Datasette-friendly `custody_log`).
