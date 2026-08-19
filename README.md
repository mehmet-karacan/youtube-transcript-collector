# YouTube Transcript Collector

A standalone Python 3.11+ collector for durable, per-video YouTube transcripts. It provides a stable JSON CLI for agents such as Zekam while keeping downloading and state management independent.

## Status

This is an MVP. It handles individual videos, lists of videos, full channels, and channel selections by latest count, date range, or title query. Completed live streams are eligible; active, upcoming, and post-live broadcasts are deferred until complete.

## Install

```bash
python -m pip install .
yt-transcripts --json doctor
```

`yt-dlp` is the only runtime dependency. Authentication is optional and must be supplied by the operator through an external cookie file; cookies are never copied into state, manifests, or logs.

## Examples

```bash
: "${VIDEO_URL:?set VIDEO_URL to an authorized video URL}"
: "${CHANNEL_URL:?set CHANNEL_URL to an authorized channel URL}"
yt-transcripts --json targets resolve --video "$VIDEO_URL"
yt-transcripts --json collect preview --video "$VIDEO_URL"
yt-transcripts --json collect preview --channel "$CHANNEL_URL" --latest 10
yt-transcripts --json collect start --video "$VIDEO_URL"
yt-transcripts --json runs status "$RUN_ID"
yt-transcripts --json runs logs "$RUN_ID"
yt-transcripts --json runs cancel "$RUN_ID"
```

`VIDEO_URL`, `CHANNEL_URL`, and `RUN_ID` are shell environment-variable templates, not bundled targets. The leading `:` checks stop the example before the CLI runs when a required URL is unset. Set them to values you are authorized to process; no external transcript or video content is bundled. You remain responsible for YouTube's Terms of Service and all applicable content rights. `targets resolve` and `collect preview` do not contact YouTube.

Outputs default to a flat `.yt-transcripts/subtitles/` directory shared by the configured state root, plus authoritative `state.sqlite3`, per-run logs, and a deterministic `subtitles-index.jsonl` projection. Filenames use `YYYY-MM-DD_VIDEO_ID_TITLE.txt`; identity and idempotency always use video ID and request digest, never title.

Rate behavior is part of the normalized request and can be bounded with `--min-delay-seconds` (0.5–60), `--max-retries` (0–20), and `--rate-limit-cooldown-seconds` (60–86400). A 429 opens a persisted cooldown in SQLite so a restarted worker cannot immediately resume requests.

Every yt-dlp launch ignores user configuration, plugins, and cache; runs in an isolated temporary directory with a sanitized environment; and is bounded by wall time and captured-output size. Cookie files are read into an ephemeral private snapshot so yt-dlp never receives or rewrites the operator's source file. Per-state-root SQLite reservations serialize request pacing across concurrent workers. Requests also bound target/language counts and lengths, discovery/metadata/subtitle bytes, active runs, retained events, and managed disk usage.

All remote titles, descriptions, transcript text, metadata, URLs, and backend errors are hostile data. The CLI allowlists metadata fields, strips format controls from filenames, and never treats remote text as commands or authorization.

## Migration

Legacy corpora are read-only inputs. Preview produces a deterministic digest; apply recomputes the preview and refuses a mismatched digest.

```bash
: "${LEGACY_DIR:?set LEGACY_DIR to the corpus directory}"
yt-transcripts --json migrate preview --legacy-dir "$LEGACY_DIR"
yt-transcripts --json migrate apply --legacy-dir "$LEGACY_DIR" --plan-digest "$PLAN_DIGEST"
```

Apply copies completed transcripts into the collector's managed `subtitles/` output and seeds the database/manifest without modifying, renaming, or deleting legacy files. Archive IDs without an existing text file are stored as `legacy-archive-only`; they remain non-complete and eligible for a later controlled probe.

Cancellation is cooperative. `runs cancel` first records `cancel_requested`; the worker terminates any active `yt-dlp` child, confirms termination, and only then records `cancelled`.

Background workers persist a heartbeat lease. Status reconciliation fails a stale PID-less queued run or a stale run whose recorded worker process is confirmed absent, rather than leaving an eternal active record. A start without `--idempotency-key` always creates a new monotonically numbered generation for that normalized request. An explicit key is an exact replay key: it always returns its original run and outcome, including failure. Retrying a failed explicit-key run therefore requires a caller-approved new key; the collector never silently changes one.

All managed transcript, manifest, run-log, migration-copy, database-path, and directory writes pass through one confinement layer. Existing path components and destinations reject symlinks and Windows reparse points/junctions; temporary files are created only in a verified parent and the path is rechecked before atomic replacement. This materially narrows path-redirection races, but Python cannot eliminate a race against a local administrator who can replace filesystem components between checks and the OS operation. Do not share the state root with untrusted local writers, and apply OS ACLs accordingly.

## Development

```bash
python -m pip install -e ".[dev]"
ruff check .
pytest
$env:SOURCE_DATE_EPOCH = "1750000000" # PowerShell; use export in POSIX shells
python -m build --no-isolation
python tools/release_provenance.py check --wheel dist/*.whl --sdist dist/*.tar.gz --sbom sbom/youtube-transcript-collector.cdx.json
```

`pyproject.toml` keeps bounded compatibility ranges for library consumers. `constraints-ci.txt` pins the complete tested graph. `requirements/ci-windows-py314.lock` is the reviewed Windows CPython 3.14 release environment: every entry is exact and SHA-256 locked from locally verified wheel bytes. CI installs that file with pip's `--require-hashes`; the wider Python 3.11–3.13 matrix continues to exercise the bounded consumer ranges.

`tools/release_provenance.py` rejects lock/constraint/direct-requirement drift, provenance-manifest drift, unsafe or unexpected archive contents, stale artifact hashes, and a stale deterministic CycloneDX 1.6 SBOM. Reproducible builds use `SOURCE_DATE_EPOCH=1750000000`. The SBOM has three explicit graph roots: the application runtime depends on `yt-dlp`; the CI/build environment depends on the direct build/test tools, which carry target-evaluated transitive edges derived from each locked wheel's standard `Requires-Dist` metadata; and the release-artifact set references the reviewed wheel and sdist. The SBOM is deliberately excluded from the sdist to avoid a self-referential artifact hash. Updating dependencies or release inputs requires reviewed distributions, regenerated real hashes, a deterministic rebuild, and an explicit SBOM regeneration—never hand-authored hashes.

The lock declares `https://pypi.org/simple/` as its resolver source and enforces wheel-only selection; pip still accepts a distribution only when its downloaded bytes match the recorded hash. For a fully offline audit, first assemble a complete wheelhouse containing all locked distributions and pass it explicitly:

```bash
python -m pip install --require-hashes -r requirements/ci-windows-py314.lock
python tools/release_provenance.py lock-check
python tools/release_provenance.py lock-check --wheelhouse /path/to/complete-wheelhouse
python tools/release_provenance.py manifest-check --wheelhouse /path/to/complete-wheelhouse
```

`provenance/locked-artifacts.json` is the durable wheel-byte provenance record. For every locked distribution it records normalized name/version, exact wheel filename, recomputed SHA-256, wheel tags, raw `Requires-Dist` metadata, the exact PyPI Simple project-index URL used for independent resolution, and the truthful local acquisition basis. The original per-artifact download URL was not retained by the pip cache and is intentionally not invented. `--wheelhouse` has no default and the verifier never searches pip caches. A partial build-backend cache is therefore expected to fail and must not be presented as release provenance.

## Agent Skill portability

`skills/youtube-transcripts/SKILL.md` is the canonical, vendor-neutral Agent Skill. The `agents/openai.yaml` file is optional UI metadata and is not required for skill discovery or validation.

The Python wheel deliberately contains only the collector CLI/library. Repository-level `schemas/` files are development/CI contract artifacts, and `skills/youtube-transcripts/` is the separately reviewed Agent Skill distribution artifact; install the skill from a trusted checkout using the exact-digest workflow below.

The repository includes a dependency-free installer used by tests and CI. It writes only below an explicitly supplied workspace, never probes user-global directories, and does not require an agent executable:

```bash
python tools/skill_portability.py validate --skill skills/youtube-transcripts
python tools/skill_portability.py install --skill skills/youtube-transcripts --workspace /tmp/demo --expected-digest REVIEWED_SHA256 --target universal --target codex --target claude-code --target opencode --target gemini-cli
```

The supported project-local discovery roots are `.agents/skills` (the universal and Codex convention), `.claude/skills`, `.opencode/skills`, and `.gemini/skills`. Review the digest returned by `validate` and pass that exact value to `install`. Installation copies the same validated package to each distinct selected root and is idempotent when content is unchanged.

Skill promotion is staged beside each destination. On Windows, only transient access-denied/sharing/lock failures (WinError 5/32/33) receive a bounded retry; confinement and reparse checks run again before every attempt. Other errors fail immediately, persistent denial is propagated, and a failed multi-target installation rolls back destinations created earlier in that operation.

No license has been selected yet. Choose one before public release.
