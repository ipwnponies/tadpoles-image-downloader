# tadpoles-image-downloader

Downloads Tadpoles childcare images from Gmail (via Apps Script queueing) and uploads them to Google Photos with correct EXIF timestamps.

## Architecture

```
Gmail (label) --> Apps Script (src/code.js, daily trigger)
                     - scans, dedupes by URL, writes JSON queue files
                     v
              Google Drive queue/ (one JSON file per day)
                     v
      Python CLI (tadpoles_image_downloader/, run manually/scheduled)
                     - process_queue.py: fetch, dedupe, EXIF-tag, orchestrate
                     - cloud_storage.py: Google Photos OAuth + upload
                     v
              Google Photos + healthcheck ping
```

Two independent runtimes in one repo: **JS** (`src/code.js`, deployed via `clasp` to Apps Script) and **Python** (`tadpoles_image_downloader/`, the CLI). They only communicate through the JSON queue files in Drive — never assume shared state or types between them.

## Commands

```bash
make install                 # poetry install
make lint                    # ruff check .
make format                  # ruff format .
make typecheck               # mypy tadpoles_image_downloader/
poetry run pre-commit install  # enable pre-commit (ruff check --fix + ruff format)

poetry run main main --queue-dir <dir> --images-dir <dir> [--no-dry-run]
poetry run main upload-images --images-dir <dir>

npx clasp push                # deploy src/code.js to Apps Script (needs `npm install` first)
```

There is no test suite. Validate Python changes with `make lint`, `make format`, `make typecheck` (all three run in CI on every push/PR — see `.github/workflows/ci.yml`) plus a dry-run of `main main` (dry-run is the default; pass `--no-dry-run` to actually write/upload).

## Key files

- `tadpoles_image_downloader/process_queue.py` — Typer CLI (`main`, `upload-images`), queue processing, EXIF writing, healthcheck ping. Entry point is `tadpoles_image_downloader.process_queue:app` (see `pyproject.toml`'s `[tool.poetry.scripts]`).
- `tadpoles_image_downloader/cloud_storage.py` — Google Photos OAuth flow + upload/`batchCreate` (photoslibrary API).
- `src/code.js` — Apps Script: reads Gmail label, extracts Tadpoles image URLs + captions, dedupes, writes queue JSON to Drive.
- `secrets.yaml` — SOPS/age-encrypted; currently just `healthcheck_url`. Decrypted at runtime by `process_queue.secrets()`.

## Environment / credentials (none of this belongs in git)

- **Google Photos OAuth**: `client.json` (OAuth client, from Google Cloud Console) and `token_photos.json` (written on first run after the browser auth flow) live in the repo root — see `CREDENTIALS_FILE`/`TOKEN_FILE` in `cloud_storage.py`. `client.json` is gitignored; `token_photos.json` is **not** (see Gotchas) — check `git status` before committing if you've run the CLI locally.
- **Healthcheck secret**: decrypting `secrets.yaml` requires the `sops` CLI on `PATH` and an age identity at `$XDG_CONFIG_HOME/age/tadpoles-image-downloader.agekey` (path resolved via `platformdirs`, so it differs per OS — see `HEALTHCHECK.md`). Only the `main` command needs this (it always pings healthcheck, even in `--dry-run`); `upload-images` never touches `secrets.yaml`.
- **Apps Script**: script properties `label_name` (Gmail label) and `drive_folder_id` (Drive folder ID) are configured in the Apps Script UI, not in source.

## Gotchas

- `.gitignore` excludes `token_photos.pickle`, but the code reads/writes `token_photos.json` — double-check that file is actually untracked before committing if you touch OAuth handling.
- In `_main`, `process_concurrency` is passed as `process_file`'s `write_concurrency` argument — one CLI flag controls both queue-file concurrency and per-file image-write concurrency.
- `main`'s `dry_run` defaults to `True`; forgetting `--no-dry-run` silently skips writes/uploads (payload is `None` on fetched entries in dry-run) — but it still pings the healthcheck URL unconditionally at the end, so a dry run still needs a working `sops`/age setup.
- Dedup in `process_file` keeps the **earliest** timestamp per filename; dedup in `src/code.js` does the same when merging queue entries — keep both in sync if that policy ever changes.
- `write_image_file` re-derives the file extension from sniffed content type (`filetype.guess`), not the URL, and silently skips (logs a warning, writes nothing) if the payload isn't a recognized image.
- Ruff ignores `B008` (Typer's `Option()`-as-default pattern is intentional) and `UP047` (PEP 695 generics not yet supported by the mypy version pinned here) — don't "fix" either.

## Code style

- Python 3.12+, `from __future__ import annotations` everywhere, full type hints (mypy `strict`-ish via `disable_error_code = ["import-untyped"]`).
- Ruff line length 120, double-quote strings, import sorting via ruff's `I` rules — run `make format` rather than hand-formatting.
- Async-first: I/O (HTTP, file writes) goes through `gather_with_concurrency(limit, ...)` rather than unbounded `asyncio.gather`.
