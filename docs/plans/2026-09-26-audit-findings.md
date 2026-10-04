# Audit findings and proposed changes

Status: **triage complete (2026-10-03); implementation not started.** Every finding has an agreed Decision block. The implementation order is at the end of this document.

Scope: full read of `tadpoles_image_downloader/`, `src/code.js`, `appsscript.json`, CI, tooling, and docs. Baseline `make lint`, `ruff format --check`, `make typecheck` all pass on `3b60aa4`.

The beads backlog (`.beads/backup/issues.jsonl`) is stale and is not used as a reference.

Findings are triaged one at a time. Each agreed finding gets a **Decision** block, which replaces the original proposal where the two differ.

## Documentation policy (applies to every decision)
Each implemented decision updates two audiences:
- **README.md (and HEALTHCHECK.md):** for the user who runs the system. It covers behaviour, limitations and what to do when something fails. Examples: link expiry, what lands in `Failed/`, how to retry.
- **CLAUDE.md:** for agents editing the code. It covers architecture, trade-offs, and limitations discovered empirically, with the evidence, so they are not "fixed" back. Examples: the email timestamp is deliberate, and the placeholder signature.

Each decision's Documentation line lists what goes where.

Severity: **High** = data loss, silent wrong results, or credential exposure. **Med** = failure modes that block or degrade runs. **Low** = cleanup and hygiene.

---

## 1. Correctness bugs

### 1.1 High: `batchCreate` sends every image in one request
`cloud_storage.mint` puts all upload tokens in one `mediaItems:batchCreate` call. The API accepts at most 50 items per call, so any run with more than 50 images fails at mint time.

Worse, a successful HTTP response does not mean every item was created. Each item's result is in `newMediaItemResults[].status`, and nothing reads it. `_upload_images` then moves **every** image to `Done/`, so an image the API rejected is marked done and never retried.

**Fix:** send chunks of up to 50. Return the set of tokens whose result has a `mediaItem`. Move only those images to `Done/`. Catch errors per chunk so one bad chunk does not throw away the chunks already created, which would re-upload them as duplicates on the next run.

**Decision (agreed 2026-09-27):**
- Context: a typical run has about 5 images (p90) and at most about 10. The 50-item limit matters only for a backlog of roughly 10 or more days. The chunking fix is cheap, so it is kept.
- Send `batchCreate` in chunks of at most 50 items. Handle errors per chunk, so a failed chunk does not discard chunks that already succeeded.
- Treat an item as created only when its result in `newMediaItemResults` contains a `mediaItem`. Log each failed item with its filename and `status.message`.
- `mint` returns the set of created upload tokens. The caller keeps a map from token to entry, so it knows exactly which entries were created.
- **Revised 2026-10-03 (see 3.2):** `images_dir` is a fresh temp directory on every run, so it cannot hold retries. An entry whose upload failed becomes a retry entry in the queue (`retry-<run time>.json`), and the next run fetches and uploads it again while it is within 96 hours (1.3). The queue is the only durable state.
- Any upload failure makes the run exit non-zero and skip the healthcheck ping. A missed ping is the alert.
- `upload_to_google_photos` returns only the upload token. The caption is attached at mint time.
- Deferred: a quarantine for images that fail every time is decided with 1.3. An immediate `/fail` ping and a failure summary are decided with 3.4. Keeping captions for retried images is decided with 3.3.
- Documentation: README says failed uploads are retried from the queue on the next run, and that a failure skips the healthcheck ping. CLAUDE.md records the 50-item `batchCreate` limit and the per-item result check.
- Verification: use a fake session with 120 tokens and one failed item. Expect 3 requests of 50, 50 and 20 items. Expect 119 entries reported as created, 1 entry written to the run's retry file, and a non-zero exit.

### 1.2 High: each JPEG is re-encoded at Pillow's default quality
`write_image_file` does `Image.open(...).save(file, exif=exif)`. For a JPEG this decodes the image and re-encodes it at Pillow's default quality of 75, which is a permanent quality loss on every photo. The save also drops the original EXIF (including Orientation, so photos can end up rotated) and the ICC profile.

**Fix:** for JPEG and WebP, use `piexif.insert(exif, data, path)`, which splices the EXIF segment in without touching the pixels. Merge into the existing EXIF (`piexif.load`) so Orientation is kept, and fall back to fresh EXIF if parsing fails. Keep the Pillow path for other formats; PNG is lossless anyway.

**Decision (agreed 2026-09-27):**
- Context: Tadpoles serves both PNG and JPEG. A sampled PNG (768x1024 RGBA, iOS-generated) had no date tags at all; its EXIF held only Orientation, resolution and dimensions. Pillow re-encoding is lossless for PNG, so the quality loss affects JPEGs only. Severity stays High, because each JPEG loses quality silently and the loss cannot be undone.
- Background: Google Photos dates each photo from EXIF `DateTimeOriginal`. Without it, the date is the time of upload. Tadpoles images carry no usable capture date, so the email `Date` is written as the capture time. This is deliberate and stays.
- JPEG and WebP: write EXIF with `piexif.insert`. The pixel data is copied unchanged, with no re-encode (the metadata equivalent of `ffmpeg -c copy`). The ICC profile lives in a separate segment and is kept.
- PNG and other formats: keep the Pillow path, which is lossless. Pass the DPI and ICC profile through.
- Merge with the existing EXIF instead of replacing it. Keep tags that are not dates, such as Orientation. Remove every original date and time tag (`DateTime`, `DateTimeOriginal`, `DateTimeDigitized`, and the `OffsetTime*` and `SubSecTime*` variants). Write the email time into `DateTimeOriginal`, `DateTimeDigitized`, `DateTime` and `OffsetTimeOriginal`. If the existing EXIF cannot be parsed or dumped, fall back to fresh EXIF.
- Documentation: add a code comment where the timestamp is written, and a CLAUDE.md Gotchas entry. README states that photos are dated by the email's send time, not the moment of capture. Both say that Photos dates from EXIF, that Tadpoles images carry no usable capture date, that the email `Date` is the only source, and that this must not be removed or replaced with the image's EXIF.
- Verification:
  - JPEG test: pixels byte-identical, Orientation kept, ICC kept, all date fields equal to the email time, even when the source carries a different date.
  - PNG test: pixels identical, date written.
  - Fallback test: a JPEG with malformed EXIF still gets written with the date.
  - Manual: upload one PNG and one JPEG, and confirm both show the email date in Google Photos.

### 1.3 High: one failed download blocks the whole pipeline indefinitely
In `process_file`, `_fetch_entry` calls `raise_for_status()` inside a plain `asyncio.gather`. One expired or 404 Tadpoles link does three things:
- It throws away that file's other fetches.
- It aborts `_main`'s gather, so **no** queue file moves to `Done/` and nothing uploads.
- It repeats on every later run, because the bad entry never leaves the queue.

The only signal is a missing healthcheck ping.

**Fix:** isolate failures per entry. On partial failure, rewrite the queue file atomically so it holds only the failed entries. Finish the run for everything else and exit non-zero without pinging.

Trade-off: a link that is permanently dead stays queued and keeps the healthcheck red until someone removes it by hand. I think that is correct, since the failure stays visible and the other images still flow. The alternative is a retry counter per entry that moves the entry to a `Failed/` file after N attempts.

**Decision (agreed 2026-09-27):**
- Evidence:
  - Tadpoles links expire after about 3 days, at a midnight cutoff in an unknown timezone. A link is guaranteed dead 96 hours after the email.
  - An expired link does not return an error. It returns `HTTP 200`, `content-type: image/png`, with a fixed placeholder image: 200x200 PNG, 27234 bytes, sha256 `04417fa1243ef225b1c4396dc87d5b579b583dd70e1a9e0cbfa48c81a68f05ce`.
  - Two stale links, one a few days old and one weeks old, returned the identical file.
  - As a result, today's code uploads the placeholder to Google Photos as a real photo, silently.
- Isolate failures per entry. Good entries are written and uploaded normally. A stalled pipeline is not acceptable, because every queued link expires during the stall.
- The original queue file always moves to `Done/`. Entries to retry go to a Python-owned retry file, which matches `*.json` and is picked up next run. The Apps Script's queue files are never rewritten. **Revised 2026-10-03 (see 3.2):** one write-once `retry-<run time>.json` per run, holding retries from every source file, and written after the upload stage.
- Age rule: an entry 96 hours or older (measured from its email timestamp) is not fetched. It goes to `queue/Failed/` as "expired by age".
- Placeholder rule: a fetched body goes to `Failed/` if either signal matches. The signal that matched is logged.
  - sha256 equal to the known placeholder hash (certain).
  - 200x200 dimensions **and** a size under 50 KB (high confidence; covers a re-encoded placeholder).
- HTTP 4xx, a malformed entry, or a payload that is not an image goes to `Failed/` immediately. No videos are expected.
- HTTP 5xx, a timeout, or a network error is retried on every run until the entry reaches 96 hours, then goes to `Failed/`. This replaces a fixed retry count.
- Any failure or pending retry makes the run exit non-zero and skip the healthcheck ping.
- Logging:
  - Each failure logs the queue file, email time, `msgId`, URL, reason and age.
  - An end-of-run summary gives counts of fetched, written, uploaded, retried and dead-lettered entries, plus the paths of the `Failed/` files.
  - Each `Failed/` JSON entry keeps its last error, so the details survive even if stdout is not captured.
  - Recovery: move a `Failed/` file back into `queue/` to retry it.
- Documentation:
  - README: links expire after about 3 days, so the pipeline must run at least daily. Explain what `Failed/` holds and how to recover a photo manually through Gmail or the Tadpoles app. Explain how to retry.
  - CLAUDE.md: the TTL evidence and the 96-hour rule. The placeholder signature and why status codes cannot detect expiry. Why the Apps Script's queue files are never rewritten.
- Outside the code (user action):
  - Check Google Photos for 200x200 placeholders that are already there.
  - Keep the pipeline schedule and the healthcheck grace period well under 96 hours, so a failure alerts while the links are still alive.
- Verification cases:
  - The placeholder bytes are dead-lettered (hash).
  - A 200x200 image at 28 KB with a different hash is dead-lettered (combination).
  - A 200x200 image at 200 KB is not flagged.
  - A real image is written.
  - An entry older than 96 hours is dead-lettered with no fetch.
  - A 503 at 50 hours goes to retry; a 503 at 97 hours is dead-lettered.
  - A 404 is dead-lettered.

### 1.4 Med: caption lookup key may not match the file on disk (conditional)
`process_file` stores captions as `file_metadata[filename]`, where `filename` is the basename from the redirect URL. `write_image_file` swaps the suffix to the sniffed type, and `_upload_images` looks captions up by `image.stem`.
- If Tadpoles URLs end without an extension (`abc`), the keys match.
- If they end with one (`abc.jpg`), the stored key is `abc.jpg`, the lookup key is `abc`, and **every caption is silently dropped**.

I don't know which form Tadpoles uses. **Fix:** key captions by `Path(filename).stem`, which is correct for both.

**Decision (agreed 2026-09-27):**
- Finding revised: this is not a live bug. Captions work today. Tadpoles URLs end in a bare ID (for example `/m/p/5J84arRcHWaYVss3jSQK7H`), and the file on disk is that ID plus an extension taken from the sniffed content type. The storing side keys by the URL name and the reading side keys by the file stem, so the two only agree because the URL has no extension. Severity: Low (hardening).
- Structural fix: key captions by the file that was actually written.
  - `write_image_file` returns the `Path` it wrote, or `None` when it skipped the payload.
  - `process_file` stores the caption under `written_path.name`.
  - `_upload_images` looks the caption up by `image.name`.
  - One source of truth, and no derivation from the URL shape. A skipped file gets no caption automatically.
- Dry run writes nothing, so it records no captions. That is fine, because a dry run does not upload.
- This key is reused by any future captions sidecar (see 3.3).
- Documentation: CLAUDE.md records that Tadpoles URLs end in a bare ID, that the extension comes from sniffing, and that captions are keyed by the name of the written file, with the reason. No README change.
- Verification: a URL with an extension and one without both deliver their captions to `mint`. A skipped non-image gets no caption entry.

### 1.5 Med: the same image can be uploaded twice across days
`src/code.js` searches `newer_than:1d` on a daily trigger. Apps Script daily triggers fire within an hour-wide window, so two runs are sometimes less than 24 h apart. The same message then lands in two days' queue files.
- Dedup in `enqueue` compares only against **today's** file.
- Dedup in Python works only **within** one queue file.

Result: a duplicate upload. If both files are processed in the same run, two threads also write the same output path at the same time, which can leave a corrupt file. I don't know whether the Photos API dedupes identical bytes; I would not rely on it.

The window also works the other way: runs more than 24 h apart can **miss** emails. See 3.1 for the structural fix.

**Minimum fix (Python):**
- Skip any image whose stem already exists in `images_dir/Done/`. This works as a free manifest of uploaded images.
- Write images atomically (unique temp file, then `os.replace`) so concurrent writers cannot corrupt a file.

**Decision (agreed 2026-09-28):**
- Evidence: Tadpoles emails sometimes group into Gmail threads, and are often in their own thread. `GmailApp.search` returns whole threads, and today's code reads every message in each matched thread. So days-old messages in a thread are re-queued. Their links are past the TTL, so they come back as placeholders (see 1.3). This is a live bug, and a likely source of placeholders already in the library.
- Replace `newer_than:1d` with a watermark kept in Script Properties:
  - `last_run_epoch`: the start time of the last successful non-dry run.
  - `recent_msg_ids`: the IDs of messages queued within the last hour of the previous window.
- Query: search `label:<label> after:<last_run_epoch - 3600>`. Then filter at **message** level: keep a message only if its date is after `last_run_epoch - 3600` and its ID is not in `recent_msg_ids`. Never filter at thread level.
- The 1-hour overlap covers the delay before Gmail search shows a new email. The ID list prevents duplicates in the overlap.
- Ordering: write the queue file first, then save the watermark and the ID list. A failure in between repeats the window (a duplicate, never a loss).
- Serialise runs with `LockService.getScriptLock()`. A run that cannot get the lock within 30 seconds logs "another run in progress" and exits. Without the lock, a manual run and the trigger could overlap, read the same watermark, and queue the same emails twice. (Added 2026-10-02 while deciding 1.7.) The lock API was described from memory, because the docs host was unreachable from the analysis environment. Verify `getScriptLock`, `tryLock` and `releaseLock` against https://developers.google.com/apps-script/reference/lock/lock-service before implementing.
- Dry run reads the watermark but never saves it.
- First run with no watermark: start from now minus 3 days, which is about the link TTL. Older links are dead anyway.
- No new permissions; `gmail.readonly` stays.
- Gmail search excludes Spam and Trash. Adding `in:anywhere` was considered and rejected: the user confirmed Tadpoles emails do not land in Spam. Quote the label in the query (`label:"<name>"`), as confirmed in Phase 0. A Gmail label as the state (the old 3.1 proposal) is rejected: it needs `gmail.modify`, and GmailApp labels whole threads, so a new message in an already-labelled thread would be missed.
- Python side: write images atomically (unique temp file, then rename), so two copies of the same image in one run cannot corrupt the output. No Python de-duplication state across runs, because the watermark removes the cause.
- Documentation:
  - README: the script remembers its last run, so a missed trigger is picked up by the next run. Emails older than about 3 days cannot be recovered, because the links expire.
  - CLAUDE.md: why a watermark instead of `newer_than`; the thread pitfall and the per-message filter; why labels were rejected; the write-then-save ordering.
- Verification (Apps Script dry run, logging decisions):
  - A thread holding an old and a new message queues only the new one.
  - Two runs in a row do not queue the same message twice.
  - A saved watermark from 3 days ago picks up everything since.

### 1.6 Med: expired OAuth refresh token crashes instead of re-authorizing
In `_load_credentials`, `creds.refresh(Request())` raises `google.auth.exceptions.RefreshError` when the refresh token is revoked or expired. That happens every 7 days if the OAuth app is still in "Testing" publishing status. The error is not caught, so the run crashes.

**Fix:** catch `RefreshError` and fall through to the browser flow.

Separate caveat: on a headless scheduled host, `run_local_server` blocks forever. It would be better to detect a non-interactive session and fail fast with a clear message.

**Decision (agreed 2026-09-28):**
- Context: the pipeline runs on a schedule, never interactively. The user has not needed to log in again in 2 years, so the OAuth app is effectively in production status and the 7-day refresh-token expiry does not apply. A rejected refresh is rare (revocation or a Google security event). Severity: Low.
- The real risk is the failure mode on a schedule:
  - A rejected refresh crashes with a raw `RefreshError` stack trace.
  - A missing or unreadable token file calls `run_local_server`, which hangs forever waiting for a browser.
- `_load_credentials` never opens a browser. When the token is missing or the refresh is rejected, it raises a clear error: "Google Photos login required: run `poetry run main login` in a terminal". The run exits non-zero and the healthcheck alerts.
- Add a `login` command (`poetry run main login`) that runs the browser flow and writes `token_photos.json`. It is run by hand, for first setup or after a revocation.
- Documentation:
  - README: first-time setup uses `login`, and so does recovery after a "login required" alert.
  - CLAUDE.md: scheduled runs must never start the browser flow (it hangs), which is why login is a separate command.
- Verification:
  - A missing token file gives a clear error, a non-zero exit, and no hang.
  - A token file with a rejected refresh token gives the same clear error.
  - `login` runs the flow and writes the token file.

### 1.7 Med: the Apps Script can overwrite the queue file and lose entries
In `enqueue`, the `try` wraps both reading and `JSON.parse`, and the `catch` assumes "no file". If the existing file is unreadable or has invalid JSON, `existingData` stays `[]`, and the next step `setContent`s the file with **only** the new URLs. The earlier entries are gone.

**Fix:** only treat "file does not exist" as a fresh start. Rethrow read and parse errors.

**Decision (agreed 2026-10-02):**
- Root cause: keying the queue file on the UTC date forces a read-merge-write cycle whenever two runs share a date. Read-modify-write on a shared file is not atomic. If the read fails (a transient Drive error, or invalid JSON), the `catch` treats it as "no file yet" and the write overwrites earlier entries.
- Write-once queue files: each run creates one new file named by its start time with second granularity, for example `2026-10-02T071503Z.json`. A run never reads, merges or updates an existing queue file. The merge code in `enqueue` is deleted.
- Second granularity is enough: the script lock from 1.5 makes runs strictly sequential, and each run takes longer than a second.
- A run with no new emails writes no file. A failed run writes no file and does not advance the watermark, so the next run covers the same window.
- Consequence: runs can happen any number of times a day (scheduled, manual, catch-up) with no special cases. Python already processes every `*.json` in `queue/`, so it needs no change.
- The same rule already holds on the Python side (1.3): queue files are only moved to `Done/`, never rewritten, and retries go into new `retry-*.json` files. The whole queue is write-once.
- Documentation:
  - README: one queue file per run, so runs can happen any number of times a day.
  - CLAUDE.md: queue files are write-once on both sides, and why (read-modify-write loses data). Replace "one JSON file per day" in the architecture section. Mention the script lock.
- Verification:
  - Two runs on the same day produce two files, and the first is untouched.
  - A run with no new emails writes no file.
  - Python processes both files.
  - A second run started while the first holds the lock waits, then proceeds or skips; it never interleaves.

### 1.8 Low: dry run still pings the healthcheck
`_main` pings the healthcheck unconditionally, so a dry run reports a successful real run to monitoring and can hide missed runs. It also forces a dry run to need `sops` and the age key.

**Fix:** ping only after a real run with zero failures.

**Decision (agreed 2026-10-02):**
- Ping the healthcheck only after a real (non-dry) run with zero failures, consistent with 1.1 and 1.3.
- A dry run ends by logging `DRY RUN: nothing written or uploaded, healthcheck not pinged`, so its log cannot be mistaken for a real run.
- A dry run no longer needs `sops` or the age key.
- Risk this closes: dry run is the default, so a schedule that omits `--no-dry-run` would do nothing every day while the healthcheck stayed green, and photos would pass their TTL unnoticed.
- Documentation:
  - README: dry run is the default; the schedule must pass `--no-dry-run`; a dry run never pings.
  - CLAUDE.md: replace the gotcha "it still pings the healthcheck URL unconditionally".
- Verification:
  - A dry run does not call the ping and does not need `sops`.
  - A real run with no failures pings.
  - A real run with any failure does not ping.

### 1.9 Low: dry run downloads full image bodies and discards them
A dry run only needs the redirect URL to get the filename. **Fix:** skip `resp.read()` in dry run.

**Decision (agreed 2026-10-02):**
- The original proposal (skip `resp.read()` in a dry run) is withdrawn. Since 1.3, a real run decides from the body whether an entry is a placeholder, not an image, or a real photo. A dry run without the body would report "OK" for entries a real run would dead-letter.
- Instead, a dry run reads the body and runs the full checks (age rule, placeholder detection, image type), but writes and uploads nothing. Its summary shows exactly what a real run would do, including which entries would go to `Failed/`.
- Cost: about 5 images a day, a few MB. Negligible.
- Implemented as part of the 1.3 work.
- Verification: a dry run over a queue containing the placeholder bytes reports that entry as "would dead-letter: placeholder", and writes no files.

### 1.10 Low: `caption: null` from JS becomes `None` in a `str` field
`extractCaption` returns `null`, and the Python side reads it with `entry.get("caption", "")`, which returns `None` when the key exists with a null value. It works by accident, because `mint` checks truthiness. **Fix:** `entry.get("caption") or ""`.

**Decision (agreed 2026-10-02):**
- Root cause: the value is `None` while the field is annotated `str`. `entry` is typed `dict[str, str]`, but JSON can hold `null`, so mypy cannot see the mismatch. It works today only because `mint` checks truthiness, which treats `None` and `""` alike. Any code that uses the caption as a string would crash or write the literal text "None".
- Python: normalise with `entry.get("caption") or ""`, so a missing key and `null` both become `""`. Keep this tolerance, because older queue files contain `null`.
- Apps Script: `extractCaption` returns `""` instead of `null`.
- Documentation: none needed (invisible to the user).
- Verification: entries with `"caption": null`, with the key missing, and with text. The first two become `""`; the third is kept.

### 1.11 High: the local wrapper script and the new design do not fit together (found 2026-10-03)
The scheduled job runs a fish wrapper that lives only on the user's machine:
1. `git pull`.
2. `rclone sync "gdrive:Data/tadpoles message queue" gdrive_queue/` (pull-only; the local copy is replaced each run).
3. `poetry sync`, then `poetry run main main --queue-dir gdrive_queue/ --images-dir (mktemp -d /tmp/tadpoles.XXXXXX) --no-dry-run`. It stops if Python fails.
4. `move_processed`: moves **every** `*.json` in the remote folder root to remote `Done/`.

Problems:
- a. Step 4 moves all remote JSON files, including any written during the run (for example by a manual Apps Script run). Those are moved to `Done/` without being processed: silent loss, today.
- b. Python's outputs never reach the remote. After 1.3 and 3.2, `retry-*.json` and `Failed/*.json` are written locally, and the next `rclone sync` deletes them.
- c. A non-zero Python exit stops the wrapper before step 4. After 3.2, a failed run has still uploaded the good photos, so the next run uploads them again (duplicates).
- d. The healthcheck covers only Python. A failure in step 4 after a green ping goes unnoticed and causes duplicates.
- e. `git pull` and `poetry sync` deploy every push to `main` automatically.

**Decision (agreed 2026-10-03):**
1. `rclone sync` down, as today.
2. Run Python and keep its exit code; do not stop yet.
3. Push outputs up whatever the exit code: `rclone copy` the run's local `retry-*.json` and `Failed/` into the remote folder.
4. Move only what Python retired: for each file in the local `Done/` that still exists in the remote root, `rclone moveto` it into the remote `Done/`. Files that arrived mid-run stay queued.
5. The healthcheck covers the whole cycle: Python sends `/start` and writes its summary to a file, but does not send the end ping. The wrapper ends with a new command, `poetry run main ping --exit-code <code> --body-file <summary>`, so the `sops` secret handling stays in Python. (This amends 3.4: the end ping moves from `main` to the wrapper.)
6. The wrapper is versioned in the repo (for example `bin/run-cycle.fish`), so it changes and is tested together with the logic it must match.
7. Deploy from a `latest` tag instead of `main`: release with `git tag -f latest <commit> && git push -f origin latest`, roll back by moving the tag back. The job runs `git fetch --tags --force && git checkout --detach latest`, then `poetry sync`. A small bootstrap that is not changed by the checkout does the fetch and checkout, then calls the repo wrapper, so the code that updates is never the code that is running.
- Documentation:
  - README: the full cycle (sync down, process, push outputs, move retired files, ping), the `latest` tag release and rollback steps, and the bootstrap.
  - CLAUDE.md: the remote folder is the real state; Python works on a local copy; the wrapper mirrors exactly what Python retired; why the end ping lives in the wrapper.
- Verification:
  - A queue file added to the remote mid-run stays in the remote root after the cycle.
  - A run with a retry and a dead-letter leaves `retry-*.json` and `Failed/*.json` in the remote, and they survive the next `rclone sync`.
  - A run where Python exits non-zero still pushes outputs and moves the retired files, then ends with a failure ping.
  - Moving `latest` back to an earlier commit makes the next run use that commit.

---

## 2. Security and credential handling

### 2.1 High: the OAuth token file is not gitignored
`.gitignore` lists `/token_photos.pickle` (stale), but the code writes `token_photos.json` in the working directory. That file holds `refresh_token` **and** `client_secret`. A single `git add .` publishes long-lived Google Photos credentials.

**Fix:**
- Gitignore `token_photos.json`.
- Write the file with `0o600` permissions.
- Consider moving both credential files to `PlatformDirs().user_config_path`, the same place the age key already lives, instead of the working directory. See 2.3.

**Decision (agreed 2026-10-02):**
- Checked: `token_photos.json` is not matched by `.gitignore`, and neither it nor `client.json` has ever been committed. Nothing leaked, and nothing needs rotating.
- `.gitignore`: add `/token_photos.json`; remove the stale `/token_photos.pickle` and `worker/__pycache__`; add `__pycache__/`, `.venv/`, `.mypy_cache/`, `.ruff_cache/`. This absorbs 4.2.
- Write the token file with owner-only permissions (`0600`).
- Anchor `client.json` and `token_photos.json` to the repo root (`Path(__file__).parents[1]`), the same way `secrets.yaml` is found. Reason: after 1.6, `login` is run by hand. With paths relative to the working directory, running `login` from another directory would put the token where the scheduled job never looks. The scheduled job runs from the repo root, so no file moves. This absorbs 2.3.
- Documentation:
  - README: `client.json` and `token_photos.json` live in the repo root, are secrets, are gitignored, and must never be committed. If one leaks, revoke access in the Google account and run `login`.
  - CLAUDE.md: replace the gotcha that says `token_photos.json` is not ignored. Note that credential paths are anchored to the repo root and why.
- Verification:
  - `git check-ignore token_photos.json` matches.
  - After `login`, `stat -c %a token_photos.json` prints `600`.
  - Running `login` and `main` from a directory other than the repo root reads and writes the token in the repo root.

### 2.2 Med: URLs from the queue are fetched without validation
`_fetch_entry` GETs whatever `entry["url"]` holds and follows redirects. The queue lives in Drive, so anyone who can write to that folder can make the CLI fetch arbitrary URLs. This is low likelihood, but the defense is cheap.

The output filename comes from the redirect path's `.name`. An empty result or `..` gives a bad target: `Path("images").with_suffix(".jpg")` writes `images.jpg` **next to** the images directory.

**Fix:**
- Require `https` and a `tadpoles.com` host (or a subdomain) on the queued URL.
- Reject an empty or `..` filename.

**Decision (agreed 2026-10-02):**
- Severity lowered to Low. The Drive queue folder lives in the user's accountB and is shared only with the user's accountA, where the Apps Script runs (corrected 2026-10-03). Exploiting this needs write access to the Drive queue folder, which in practice means the Google account is already compromised. The fix is cheap defence in depth.
- Tested: a final URL ending in `/` gives an empty name, and `(images_dir / "").with_suffix(".png")` writes `images.png` next to `images_dir`. A name of `..` gives `...png` inside it. `Path.name` prevents deeper traversal.
- Before fetching, require scheme `https` and a host of `tadpoles.com` or a subdomain of it. A failure is permanent and goes to `Failed/` (per 1.3).
- After the redirect, reject an empty, `.` or `..` filename; it goes to `Failed/`.
- Do not restrict the redirect target's host: a valid link may redirect to an unknown storage host.
- Documentation: CLAUDE.md notes that Python re-validates queue URLs because the queue is external input, and the Apps Script regex is not the only guard.
- Verification:
  - `http://`, `https://evil.com/...` and `https://tadpoles.com.evil.com/...` are rejected without a fetch.
  - `https://www.tadpoles.com/m/p/X` is accepted.
  - A final URL ending in `/` is rejected, and nothing is written outside `images_dir`.

### 2.3 Low: credential paths depend on the working directory
**Merged into 2.1 (2026-10-02):** paths are anchored to the repo root.

`CREDENTIALS_FILE` and `TOKEN_FILE` are relative paths. Running from cron in another directory breaks authentication or writes the token somewhere unexpected. `secrets()` has a related problem: it uses `Path(__file__).parents[1]`, which only works for an editable install.

Moving the files is a breaking change for your current setup, so it needs your call.

### 2.4 Low: Apps Script has full Drive scope
`appsscript.json` requests `https://www.googleapis.com/auth/drive`, which gives read/write access to **all** of Drive. `drive.file` is narrower, but it only covers files the script created or opened. The script would then need to create its own queue folder instead of opening an existing one by ID.

Trade-off: a one-time setup change in return for a much smaller blast radius if the script is ever compromised.

**Decision (revised and agreed 2026-10-04): move the pipeline's Gmail side to accountB, with `drive.file`.**
- Context:
  - The Apps Script used to run in accountA, where Tadpoles emails arrive. The queue folder lives in accountB and was shared with accountA.
  - accountA's address is handed out freely, so it attracts spam and phishing. Its Drive also holds sensitive data. So the script's reach into accountA must be minimised.
  - accountB must need no special setup: the files must simply exist in accountB.
- Phase 0 evidence (run in accountA, 2026-10-03):
  - `DriveApp` requires the full `drive` scope for any call: `Specified permissions are not sufficient to call DriveApp.createFolder. Required permissions: https://www.googleapis.com/auth/drive`.
  - The Advanced Drive service (`Drive.*`, v3) under `drive.file` can create a folder and a file, reopen the folder by ID in a later run, and list it.
  - A folder the script did not create is unreachable (`File not found`), even when shared with the account. Reaching it would need a Google Picker grant, which needs a standard Google Cloud project; rejected as too much setup and ongoing risk.
  - `LockService` behaves as 1.5 assumes. Label search accepts `label:"name with space"`; the email must not be in Spam.
- Design:
  - accountA: a Gmail filter applies the existing label **and forwards** Tadpoles emails to accountB (set up 2026-10-04). Forwarding of all mail stays disabled. accountA ends up with no script, no Drive access granted, and no share from accountB.
  - accountB: a Gmail filter with the same criteria applies a label (and may skip the inbox). A new Apps Script project in accountB reads that label and writes queue files.
  - Scopes in accountB: `gmail.readonly` and `drive.file`. Drive calls use the Advanced Drive service (`Drive.*`), never `DriveApp`.
  - The script creates its own queue folder in accountB's My Drive on first run, and stores its ID in the Script Property `queue_folder_id`. accountB's rclone sees it directly.
- Accepted trade-off (user, 2026-10-04): `gmail.readonly` cannot be limited to a label, so the script in accountB can read all of accountB's mail. In exchange, accountA's Drive is never exposed, and the script's Drive access is limited to its own folder.
- Open checks before cutover:
  1. Forwarding: the next real Tadpoles email arrives in accountB with the label, the original Tadpoles sender, the `https://www.tadpoles.com/m/p/...` links and caption intact, and a date matching accountA's copy within about a minute.
  2. Spike re-run in accountB: `adv1` to `adv3` and cleanup, plus `rclone ls "gdrive:tadpoles-queue-spike-adv"` from the Python machine listing the files.
- Cutover, in this order:
  1. Complete both open checks.
  2. Create the Apps Script project in accountB and deploy the rewritten script with `clasp`, logged in as accountB. Set `label_name` to accountB's label. Enable the Drive API advanced service (v3).
  3. Seed `last_run_epoch` with the time of the last accountA run, so nothing is queued twice or skipped.
  4. Disable the trigger in accountA, then create the daily 19:00 trigger in accountB. Never run both at once.
  5. Dry run in accountB, then a real run; note the new queue folder's name and path.
  6. Point the wrapper's `remote_dir` at the new folder (under accountB's `gdrive:` remote), and do one manual wrapper run.
  7. After a few clean runs: delete accountA's script project, revoke its access in accountA's Google account settings, and stop sharing `tadpoles message queue` with accountA. Keep accountA's forwarding filter.
- Rollback before step 7: re-enable accountA's trigger, disable accountB's, and restore the old `remote_dir`. Seed accountA's watermark the same way if 1.5 has already shipped there.
- Mitigations kept: `clasp push` only by hand from a reviewed commit; the script stays small and covered by `node --test`; 2-step verification on both accounts.
- Documentation:
  - README: the two-account setup (forwarding filter in A, label filter and script in B), the cutover and rollback steps, and that the script can read accountB's mail.
  - CLAUDE.md: never use `DriveApp` (it forces the full `drive` scope); the script only reaches folders it created; why the Picker route was rejected; why the script lives in accountB.

**Superseded decision (2026-10-03), kept for history:**
- Narrow the Apps Script's Drive scope from `drive` to `drive.file`. After 1.7, the script only creates files, so the full scope is unnecessary. This removes a permission, and it ships with the 1.5 and 1.7 Apps Script rewrite.
- Under `drive.file`, the script can only reach files it created. So it creates and owns its own queue folder, stored in a new Script Property, `queue_folder_id`.
- The API behaviour (`DriveApp` under `drive.file`, the consent wording) was described from memory. It must be confirmed by Phase 0 before any production change.

**Phase 0: test in a throwaway script (production untouched).** Create a new standalone Apps Script project whose manifest has only `drive.file`. Run a test function and check:
1. The consent screen asks only for access to files the app uses.
2. `DriveApp.createFolder("tadpoles-queue-spike")` succeeds, and the ID is logged.
3. `folder.createFile("test.json", "[]", MimeType.JSON)` succeeds.
4. In a later, separate run, `DriveApp.getFolderById(<saved id>).createFile(...)` succeeds. This is the critical check.
5. `DriveApp.getFolderById(<current production queue folder id>)` fails with an access error.
6. The spike folder and file appear in the local Drive sync, and Python can read them.

Decision rule:
- All pass: use `DriveApp` with `drive.file`.
- Check 2, 3 or 4 fails: repeat with the Advanced Drive service under `drive.file`.
- That also fails: keep `drive`, and record why in CLAUDE.md.

Delete the spike project and folder afterwards. The test function and checklist are written as part of implementation; the user runs it in their Google account.

**Phase 1: cutover (after Phase 0 passes), in this order:**
1. Drain the old queue: run Python once and confirm `queue/` holds no unprocessed `*.json`.
2. Record the last old-script run time from the Apps Script execution log.
3. Deploy with `clasp push`: the `drive.file` manifest, plus code that creates its own queue folder on first run and stores the ID in `queue_folder_id`. Re-authorise.
4. Seed `last_run_epoch` by hand with the time from step 2. Do not use the default "now minus 3 days" seed, which would re-queue emails the old script already handled and upload them twice.
5. Dry run, then a real run. Check the log for the new folder ID and the queued emails.
6. Wait for sync, update the scheduled `--queue-dir` to the new folder, and do one manual Python dry run against it.
7. Delete the old `drive_folder_id` property. Keep the old folder as an archive, or delete it.

Do the cutover between two scheduled runs. The seeded watermark prevents missed emails even if it slips a day; links last about 3 days.

**Rollback:** `clasp push` the previous commit (old manifest and code), and restore the old `--queue-dir`. Files already in the new folder are complete and write-once; point Python at it once to drain them.

- Documentation:
  - README: a one-time migration section with the steps above, and how to re-authorise.
  - CLAUDE.md: why `drive.file`, why the script owns its queue folder, the `queue_folder_id` property, and the watermark-seeding pitfall.

### 2.5 Low: CI hardening
- Add `permissions: contents: read` at the top of `ci.yml`.
- Pin third-party actions (`snok/install-poetry`) by commit SHA.
- Consider Dependabot for `pip`, `npm`, and `github-actions`.

**Decision (agreed 2026-10-03):**
- Context: CI runs `ruff check`, `ruff format --check` and `mypy`. The user pushes to branches without opening PRs, and also commits straight to `main`. The pre-commit hook covers ruff locally, so CI's unique value is mypy, plus a safety net when the hook is not installed.
- Add `permissions: contents: read` at the workflow top level.
- Add the Python version to the venv cache key, so a Python bump cannot restore a venv built for the old interpreter.
- Keep triggers as they are: `push` on all branches is the user's main feedback loop.
- Keep `snok/install-poetry` (and `actions/*`) pinned by tag, not SHA. With no secrets in CI and a read-only token, SHA pinning without an update mechanism buys little.
- No Dependabot.
- Documentation: CLAUDE.md notes the read-only token and that actions are pinned by tag on purpose.
- Verification: CI passes on the branch, and the job log shows `Contents: read` for the token.

---

## 3. Architecture

### 3.1 Replace the time window with Gmail label state (JS)
**Merged into 1.5 (2026-09-28).** The label-based design below is rejected; see the 1.5 decision.

The `newer_than:1d` window is the root cause of both the missed emails and the duplicate uploads in 1.5. A more robust design:
- Query `label:<tadpoles> -label:<tadpoles>/queued`.
- Enqueue the results.
- Apply the `queued` sub-label only after the Drive write succeeds.

This makes each run idempotent and self-healing after missed triggers.

Cost: it needs the `gmail.modify` scope instead of `gmail.readonly`, which is broader. My recommendation is to take it, because correctness outweighs the scope increase for a personal tool. Your call.

### 3.2 Treat the queue as one work set, not independent files (Python)
Today each queue file has its own dedup and its own `ClientSession`. With the defaults, 6 files × 15 fetches means up to 90 concurrent connections to Tadpoles. Also, `process_concurrency` is reused as the write concurrency, so writes nest to 6 × 6.

Proposal:
- Load every queue file, dedupe globally, and fetch through one shared session with one global connection cap.
- Write per-file state back only at the end: move to `Done/`, or rewrite with the failed entries.
- Give writes their own knob, or simply let `asyncio.to_thread` bound them.

This removes the cross-file write race by construction.

**Decision (agreed 2026-10-03):**
- Context: queue files are only a write partition for the Apps Script (write-once, see 1.7). They carry no meaning for processing; they matter only for bookkeeping. Uploads were already batched across files.
- Context: the scheduled invocation is `poetry run main main --queue-dir $queue_dir --images-dir (mktemp -d /tmp/tadpoles.XXXXXX) --no-dry-run` (fish). `images_dir` is a new temp directory every run, so it is scratch, not durable state. Today's code moves each queue file to `Done/` before uploading, so any upload failure already loses those photos.
- Restructure `process_queue.py` into stages over one combined list of entries:
  1. Load every `queue/*.json` (including retry files) into one list. Each entry remembers its source file.
  2. Pre-filter: URL validation (2.2) and the 96-hour age rule (1.3). Failures go to the dead-letter list.
  3. De-duplicate across all entries, keeping the earliest timestamp per image.
  4. Fetch with one shared HTTP session, up to `--fetch-concurrency` (default 15) at once.
  5. Classify per 1.3: placeholder, not an image, or 4xx is dead-lettered; 5xx or a network error is retried.
  6. Write EXIF and save atomically (1.2, 1.5) on the thread pool.
  7. Upload: up to `--upload-concurrency` (default 3) at once, then mint in chunks of 50 (1.1). Any entry whose upload or mint failed joins the retry list.
  8. Retire: write one `retry-<run time>.json` and one `Failed/<run time>.json` (both write-once, only if non-empty), **then** move every source file to `Done/`.
  9. Report: summary, exit code, healthcheck ping (1.3, 1.8).
- Upload happens before retirement. A crash before step 8 leaves the source files in `queue/`, so the next run redoes the work. A crash after mint but before retirement can upload the same photos twice; that window is small and accepted.
- The queue is the only durable state. Every failure (fetch or upload) takes the same path: a retry entry in the queue.
- The temp `images_dir` is deleted at the end of a fully successful run, and kept on failure with its path logged, for debugging and manual recovery via `upload-images`.
- CLI: remove `--process-concurrency`, which no longer means anything. The scheduled command does not pass it. `--fetch-concurrency` and `--upload-concurrency` each control one thing.
- Removes the CLAUDE.md gotcha about `process_concurrency`.
- Documentation:
  - README: the pipeline stages in brief; `images_dir` is scratch and may be a temp directory; failures are retried from the queue; a kept temp dir after a failure can be uploaded with `upload-images`.
  - CLAUDE.md: the stage order and why upload precedes retirement; queue files are bookkeeping partitions only; the queue is the only durable state.
- Verification:
  - Two queue files plus a retry file sharing one image produce one fetch and one written file.
  - All source files end in `Done/`, with at most one retry file and one `Failed/` file for the run.
  - A simulated upload failure puts that entry in the run's retry file, and the next run fetches and uploads it again.
  - A crash simulated before retirement leaves the source files in `queue/`.
  - A successful run deletes its temp `images_dir`; a failed run keeps it and logs the path.

### 3.3 Captions survive only in memory
Captions go from queue to upload through a dict held in memory for one run. They are lost if:
- the upload step fails and a later run uploads the leftover images, or
- `upload-images` is run on its own.

Options:
- (a) A sidecar `captions.json` in `images_dir`, updated on write and pruned on upload. Simple, and my recommendation.
- (b) Put the caption in an EXIF field and read it back at upload time. This is self-contained, but EXIF text fields are awkward with non-ASCII text such as emoji.

**Decision (agreed 2026-10-03):**
- After 3.2, a failed upload becomes a queue retry entry, and each entry carries its caption. So retried uploads keep their captions automatically.
- The only remaining gap is manual recovery with `upload-images` from a kept temp dir, which uploads without captions. The user does not plan to upload manually. No sidecar file is added.
- A retry run always fetches again; it never reuses images from a previous run's temp dir. Reuse would need a stable directory or a record of the previous temp path, plus checks per entry, which brings back a second durable store. Re-fetching a handful of images costs nothing at this volume.
- Documentation:
  - README: `upload-images` does not apply captions.
  - CLAUDE.md: retry runs always re-fetch by design; `images_dir` is scratch and must not become durable state.

### 3.4 Healthcheck failure signal
Only success is signalled now. If the provider is healthchecks.io-style, pinging `<url>/fail` on error (and optionally `<url>/start`) gives an immediate alert instead of waiting for the grace period. I can't see the provider because the URL is encrypted.

The use of `sops` for one low-sensitivity URL is also heavy. An environment variable override (for example `TADPOLES_HEALTHCHECK_URL`) with `sops` as the fallback would make CI and headless hosts simpler.

**Decision (agreed 2026-10-03):**
- Provider: healthchecks.io. Behaviour confirmed from the Pinging API reference (https://healthchecks.io/docs/http_api/), as pasted by the user:
  - Endpoints `/start`, `/fail`, `/log` and `/<exit-status>` (0 means success, anything else failure).
  - A POST body is stored up to 100 kB (`Ping-Body-Limit` response header).
  - `rid=<uuid>` pairs a start ping with its completion.
  - UUID endpoints return `200` with the body `OK (not found)` or `OK (rate limited)` when the ping is ignored, so `raise_for_status()` cannot detect a bad URL.
  - Rate limit: 5 pings a minute per check.
- At run start, generate `rid = uuid4()` and POST `<url>/start?rid=<rid>`.
- At run end, POST `<url>/<exit code>?rid=<rid>`, with the end-of-run summary as the body, truncated to the `Ping-Body-Limit` (100 kB). One call; the healthcheck always matches the process exit code.
- Check that each response body is exactly `OK`. Anything else, or a network error, logs a warning. A ping failure never changes the run's exit code (today a ping error crashes the run after the work is done).
- Dry run: no pings (1.8).
- No environment-variable override for the URL; keep `sops`.
- Dashboard (user action): cron schedule `0 20 * * *`, timezone `America/Los_Angeles`, grace period about 1 hour (currently 3 days). A 3-day grace period means a run that never starts alerts after about 4 days, which is past the 72-hour link TTL. `/fail` does not cover runs that never start, so the grace period still matters. With `/start`, the grace period also bounds the run time, which catches hangs.
- Timing analysis: Apps Script fires daily within the 19:00 hour, Python runs at 20:00 (both America/Los_Angeles). Worst-case delay from email to upload is about 25 hours, so one missed day (about 49 hours) still fits within the 72-hour TTL. Edge case: a trigger firing late in the hour may not have synced by 20:00, which adds a day (about 49 hours, still within the TTL). Optional: move Python to 20:30 or 21:00.
- Documentation:
  - README: what each healthcheck state means (started, success, fail, late); the recommended dashboard settings and why (alerts must arrive before the 72-hour TTL); the recommended schedule ordering (Apps Script before Python).
  - CLAUDE.md: the end ping uses the exit-code endpoint; `rid` pairs pings; a `200` whose body is not `OK` means the ping was ignored.
- Verification:
  - A successful run sends `/start`, then `/0` with the same `rid`.
  - A failed run sends `/start`, then `/1` with the summary body.
  - A response of `OK (not found)` logs a warning, and the exit code is unchanged.
  - A network error on the ping does not fail the run.
  - A dry run sends nothing.

---

## 4. Cleanup and tooling

| # | Item | Fix |
|---|------|-----|
| 4.1 | `HEALTHCHECK.md` describes `secrets/healthcheck-url.age` and the plain `age` CLI; the code uses `sops` and `secrets.yaml`. The rotation steps are wrong (`$XDG_CONFIG_HOME/.config/...`). | Rewrite it for `sops`: `sops updatekeys` / `sops edit secrets.yaml`. |
| 4.2 (merged into 2.1) | `.gitignore` has stale entries (`token_photos.pickle`, `worker/__pycache__`) and is missing `__pycache__/`, `.venv/`, `.mypy_cache/`, `.ruff_cache/`, `token_photos.json`. | Update it. |
| 4.3 | `eslint.config.mjs` uses `defineConfig` from `eslint/config`, which needs ESLint ≥ 9.22, while `package.json` pins `eslint ^8.57.1` (and `@eslint/js ^9`). It also declares `globals.browser` instead of the Apps Script globals (`GmailApp`, `DriveApp`, ...). JS lint probably does not run at all, and nothing lints JS in CI. | Needs dependency approval: bump eslint to 9, add Apps Script globals, add an `npm run lint` CI job. |
| 4.4 | `package.json` has boilerplate (`"main": "index.js"`, empty description, ISC license while the repo `LICENSE` differs). | Tidy. |
| 4.5 | `upload_to_google_photos` accepts `caption` only to return it unchanged. | Return only the token; attach the caption at mint time. |
| 4.6 | In `write_image_file`, the EXIF is built before the check for whether the data is an image. | Reorder. |
| 4.7 | `logging.basicConfig` runs at import time. | Move it to a Typer `@app.callback()`. |
| 4.8 | The mypy config is not strict, although CLAUDE.md says "strict-ish". | Try `strict = true` and fix the fallout (mostly missing `-> None` on commands). |
| 4.9 | The Apps Script uses a mix of `var` and `const` and a fixed retry delay. | Use `const`, and exponential backoff in `safeApiCall`. |
| 4.10 | Search is `label:${labelName}` without quotes. A label containing spaces may break the search (unverified). | Verify, then quote the label or swap spaces for hyphens. |
| 4.11 | `.beads/backup/*` is committed. | Keep it if intentional; otherwise gitignore it. |

**Decisions for section 4 (agreed 2026-10-03):**
- Already covered: 4.2 by 2.1; 4.5 by 1.1 (`upload_to_google_photos` returns only the token); 4.6 by the 1.2 rewrite of `write_image_file`.
- 4.1: rewrite `HEALTHCHECK.md` for `sops` and `secrets.yaml` (key location, `sops edit`, rotating the age key with `sops updatekeys`), and add the 3.4 dashboard settings.
- 4.3 (dependency change approved by the user): bump `eslint` to 9 in `package.json` and regenerate `package-lock.json` with npm. Replace `globals.browser` with Apps Script globals (`GmailApp`, `DriveApp`, `PropertiesService`, `LockService`, `Utilities`, `Logger`, `MimeType`). Add an `npm run lint` script and a CI job that runs it.
- 4.4: `package.json` license `ISC` becomes `MIT` (matching `LICENSE`); remove `"main": "index.js"`; add a description. Metadata only.
- 4.7: move `logging.basicConfig` into a Typer `@app.callback()`, so importing the module has no side effects.
- 4.8: set `strict = true` in the mypy config and fix what it flags. Update the CLAUDE.md "strict-ish" wording.
- 4.9: the Apps Script runtime is V8 (`appsscript.json`), which supports `const` and `let`. `var labelName` is assigned once, so it becomes `const`. `safeApiCall` uses exponential backoff (1 s, 2 s, 4 s) via `Utilities.sleep`. Lands in the 1.5 and 1.7 Apps Script rewrite.
- 4.10: the user's label has no space, but Gmail allows spaces, so handle it defensively. Unverified: whether Gmail search accepts `label:"name with space"`, or needs spaces replaced with hyphens (`label:name-with-space`). Check with a throwaway label containing a space and a dry run that logs the match count; use the form that matches.
- 4.11: delete `.beads/` from the repository and add `.beads/` to `.gitignore`. The backlog is stale; git history keeps it.

---

## 5. Testing gap
There is no test suite. The riskiest logic is pure or easy to isolate:
- dedup policy
- EXIF build and write (round-trip a generated JPEG and PNG, check that `DateTimeOriginal` is set and the JPEG pixels are byte-identical)
- chunking and per-item result parsing in `mint` (with a mocked session)
- URL and filename validation

**Needs dependency approval:** `pytest` and `pytest-asyncio` as dev dependencies, plus a `make test` target and a CI step. The alternative with no new dependency is `unittest` with `IsolatedAsyncioTestCase`, which also works.

**Decision (agreed 2026-10-03):**
- Python (dependency change approved by the user): add `pytest` and `pytest-asyncio` to `[tool.poetry.group.dev.dependencies]`, and update `poetry.lock` with Poetry. Add a `make test` target and a `pytest` step in CI. Fake HTTP servers (Tadpoles, Photos, healthchecks.io) use `aiohttp.test_utils.TestServer`, which ships with aiohttp. Every decision's verification cases become tests; tables of cases use `parametrize`.
- Apps Script: write the new logic as plain functions that take inputs and return outputs (watermark window and message filter from 1.5, run-file naming from 1.7, label quoting from 4.10, caption extraction from 1.10). Test them with Node's built-in runner (`node --test`), with no new dependency, plus an `npm test` script and CI step. Google API calls are covered by the dry runs in the plan, not by local tests.
- Documentation: README and CLAUDE.md list `make test` and `npm test` alongside lint and typecheck. CLAUDE.md drops "There is no test suite".

---

## 6. Feature ideas (optional)
- **Video support:** `filetype` IMAGE-only sniffing silently skips videos. Tadpoles sends videos too, and the Photos API accepts them (EXIF would be skipped for them).
- **Target album:** an optional `--album-id` passed to `batchCreate`.
- **Run summary:** log counts at the end (fetched, deduped, written, uploaded, failed), and include them in the healthcheck ping body.
- **Headless runner:** a systemd timer or launchd example, plus the credential-path change in 2.3.

**Decision (agreed 2026-10-03):**
- Video support: dropped. Tadpoles sends no videos.
- Run summary: already decided (1.3 summary, sent as the healthcheck body in 3.4).
- Scheduler example docs: skipped. The user already has a working schedule.
- Upload into an album: skipped. The user tried adding uploads to an album before, and it did not work.

---

## Implementation order
Ordered so that each step ships on its own, and the riskiest data-loss fixes land first. Every step follows the documentation policy, and adds tests for the verification cases listed in its decisions. `make lint`, `make format`, `make typecheck`, `make test` and `npm test` must pass before each push.

The new Python works with both the old and the new Apps Script, because it processes every `queue/*.json`. So Python ships first, and the Apps Script follows after Phase 0.

**Step 1: foundations (no behaviour change)**
- Test infrastructure: `pytest`, `pytest-asyncio`, `make test`, CI step (5).
- mypy `strict = true` and its fixes (4.8).
- `.gitignore` cleanup and `.beads/` deletion (2.1, 4.11).
- CI: read-only token and the cache-key fix (2.5).
- `logging.basicConfig` moved into a Typer callback (4.7).
- `package.json` metadata (4.4); `eslint` 9, Apps Script globals, `npm run lint`, `npm test` with `node --test`, and CI jobs (4.3, 5).

**Step 2: Google Photos and credentials (`cloud_storage.py`)**
- `batchCreate` chunking, per-item results, `mint` returns created tokens (1.1).
- No browser flow in scheduled runs; a separate `login` command (1.6).
- Credential paths anchored to the repo root; token file written with `0600` (2.1).

**Step 3: image writing (`write_image_file`)**
- Lossless EXIF for JPEG and WebP, EXIF merge with date tags replaced, Pillow path for others with DPI and ICC kept (1.2).
- Atomic writes (1.5); returns the written path (1.4).

**Step 4: staged pipeline (`process_queue.py`)**
- The stages from 3.2: load, pre-filter, de-duplicate, fetch, classify, write, upload, retire, report.
- Within them: URL and filename validation (2.2); the 96-hour age rule, placeholder detection, and retry or dead-letter classification (1.3); captions keyed by the written file (1.4); null captions normalised (1.10); dry run runs every check but writes nothing (1.9).
- Upload before retirement; one write-once retry file and `Failed/` file per run; temp `images_dir` deleted on success and kept on failure (3.2, 3.3).
- Remove `--process-concurrency`.

**Step 5: healthcheck, dry-run signalling and the wrapper**
- `/start` from Python, `rid`, summary body written to a file, `OK` body check, ping failures never fail the run (3.4).
- New `ping` command (`poetry run main ping --exit-code <code> --body-file <summary>`); the end ping moves to the wrapper (1.11, amending 3.4).
- No pings on a dry run (1.8).
- Wrapper `bin/run-cycle.fish` checked into the repo: sync down, run Python and keep its exit code, push `retry-*.json` and `Failed/` up, move only the files Python retired, then call `ping` (1.11).
- README documents the out-of-repo bootstrap (fetch the `latest` tag, check it out, run the wrapper) and the release and rollback steps.
- Steps 4 and 5 deploy together: retries and dead-letters only reach Drive once the wrapper pushes them.

**Step 6: accountB checks (user; see 2.4)**
- Verify forwarding with the next real Tadpoles email.
- Re-run the Drive spike (`docs/plans/phase0/`) in accountB, including the rclone listing.

**Step 7: Apps Script rewrite (after step 6)**
- Watermark in Script Properties with the per-message filter and the 1-hour overlap; script lock (1.5).
- Write-once queue files named by run time (1.7).
- `extractCaption` returns `""` (1.10); `const` and exponential backoff (4.9); quoted label in the search (4.10).
- Drive calls through the Advanced Drive service under `drive.file`; the script creates and owns its queue folder (2.4).
- Pure logic covered by `node --test`.

**Step 8: cutover to accountB (user, with guidance; see 2.4)**
- Follow the cutover order in 2.4: deploy to accountB, seed the watermark, switch triggers, point the wrapper at the new folder, then retire accountA's script and share.

**Step 9: documentation pass**
- Check that README, HEALTHCHECK.md and CLAUDE.md reflect every decision (4.1 and the documentation lines of each decision).

**User actions outside the code (any time)**
- Check Google Photos for 200x200 placeholder images already uploaded (1.3, 1.5).
- healthchecks.io dashboard: cron schedule `0 20 * * *` in `America/Los_Angeles`, grace period about 1 hour (3.4).
- Optional: move the Python run to 20:30 or 21:00 (3.4).
