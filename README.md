# tadpoles-image-downloader

Downloads images from [Tadpoles](https://www.tadpoles.com/) childcare notification emails and uploads them to Google Photos, preserving original timestamps as EXIF metadata.

## Architecture

```
Gmail (Tadpoles emails)
        |
        v
 Google Apps Script          <- runs on a daily trigger in Google Workspace
  - Scans emails with label
  - Extracts image URLs
  - Deduplicates by URL
  - Writes JSON queue files to Google Drive
        |
        v
  Google Drive (queue/)      <- JSON files, one per day
        |
        v
 Python CLI (this repo)      <- run manually or on a schedule
  - Reads queue files from local mount / rclone
  - Downloads images concurrently via aiohttp
  - Injects DateTimeOriginal EXIF metadata
  - Uploads to Google Photos in batch
  - Moves processed files to Done/
        |
        v
  Google Photos
```

## Prerequisites

- Python 3.12+
- [Poetry](https://python-poetry.org/)
- [sops](https://github.com/getsops/sops) + [age](https://github.com/FiloSottile/age) for secrets decryption
- Google Cloud project with the **Photos Library API** enabled
- [clasp](https://github.com/google/clasp) (for deploying the Apps Script — or manually paste `src/code.js` into the Apps Script web editor)

## Setup

### 1. Python environment

```bash
poetry install
```

### 2. Google Photos credentials

1. Create OAuth 2.0 credentials in your Google Cloud Console and download as `client.json` in the project root.
2. On first run the browser will open for authorization; the token is saved to `token_photos.json`.

### 3. Secrets (healthcheck URL)

The healthcheck URL is stored encrypted via SOPS + age.

1. Place your age private key at `$XDG_CONFIG_HOME/age/tadpoles-image-downloader.agekey`.
2. See [HEALTHCHECK.md](HEALTHCHECK.md) for details on the healthcheck setup.

### 4. Google Apps Script

1. Install clasp: `npm install`
2. Configure script properties in the Apps Script UI:
   - `label_name` — Gmail label applied to Tadpoles emails
   - `drive_folder_id` — Google Drive folder ID for queue JSON files
3. Deploy: `npx clasp push`
4. Set a time-based trigger on `forRealsies` (e.g. daily).

## Usage

### Process queue and upload

```bash
poetry run main main \
  --queue-dir /path/to/google-drive/queue \
  --images-dir /tmp/tadpoles-images \
  --no-dry-run
```

### Upload previously downloaded images

```bash
poetry run main upload-images \
  --images-dir /tmp/tadpoles-images
```

Run with `--help` on either command for full option descriptions including concurrency tuning.

## Development

```bash
make install     # install all dependencies
make lint        # ruff check
make format      # ruff format
make typecheck   # mypy
```

### Pre-commit hooks

```bash
poetry run pre-commit install
```

Hooks run `ruff` (lint + fix) and `ruff format` on every commit.
