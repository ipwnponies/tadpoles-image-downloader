# Audit findings and proposed changes

Status: **proposal, awaiting approval**. Nothing here has been implemented.

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
- `mint` returns the set of created upload tokens. `_upload_images` keeps a map from token to path and moves only created images to `images_dir/Done/`.
- Failed images stay in `images_dir`. The next `main` run uploads everything in `images_dir`, so it retries them automatically.
- Any upload failure makes the run exit non-zero and skip the healthcheck ping. A missed ping is the alert.
- `upload_to_google_photos` returns only the upload token. The caption is attached at mint time.
- Deferred: a quarantine for images that fail every time is decided with 1.3. An immediate `/fail` ping and a failure summary are decided with 3.4. Keeping captions for retried images is decided with 3.3.
- Documentation: README says failed uploads stay in `images_dir` and retry on the next run, and that a failure skips the healthcheck ping. CLAUDE.md records the 50-item `batchCreate` limit and the per-item result check.
- Verification: use a fake session with 120 tokens and one failed item. Expect 3 requests of 50, 50 and 20 items. Expect 119 files moved to `Done/`, 1 file left in `images_dir`, and a non-zero exit.

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
- The original queue file always moves to `Done/`. Entries to retry go to a Python-owned `queue/retry-<name>.json`, which matches `*.json` and is picked up next run. The Apps Script's queue files are never rewritten.
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
- No new permissions; `gmail.readonly` stays. A Gmail label as the state (the old 3.1 proposal) is rejected: it needs `gmail.modify`, and GmailApp labels whole threads, so a new message in an already-labelled thread would be missed.
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

### 2.3 Low: credential paths depend on the working directory
**Merged into 2.1 (2026-10-02):** paths are anchored to the repo root.

`CREDENTIALS_FILE` and `TOKEN_FILE` are relative paths. Running from cron in another directory breaks authentication or writes the token somewhere unexpected. `secrets()` has a related problem: it uses `Path(__file__).parents[1]`, which only works for an editable install.

Moving the files is a breaking change for your current setup, so it needs your call.

### 2.4 Low: Apps Script has full Drive scope
`appsscript.json` requests `https://www.googleapis.com/auth/drive`, which gives read/write access to **all** of Drive. `drive.file` is narrower, but it only covers files the script created or opened. The script would then need to create its own queue folder instead of opening an existing one by ID.

Trade-off: a one-time setup change in return for a much smaller blast radius if the script is ever compromised.

### 2.5 Low: CI hardening
- Add `permissions: contents: read` at the top of `ci.yml`.
- Pin third-party actions (`snok/install-poetry`) by commit SHA.
- Consider Dependabot for `pip`, `npm`, and `github-actions`.

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

### 3.3 Captions survive only in memory
Captions go from queue to upload through a dict held in memory for one run. They are lost if:
- the upload step fails and a later run uploads the leftover images, or
- `upload-images` is run on its own.

Options:
- (a) A sidecar `captions.json` in `images_dir`, updated on write and pruned on upload. Simple, and my recommendation.
- (b) Put the caption in an EXIF field and read it back at upload time. This is self-contained, but EXIF text fields are awkward with non-ASCII text such as emoji.

### 3.4 Healthcheck failure signal
Only success is signalled now. If the provider is healthchecks.io-style, pinging `<url>/fail` on error (and optionally `<url>/start`) gives an immediate alert instead of waiting for the grace period. I can't see the provider because the URL is encrypted.

The use of `sops` for one low-sensitivity URL is also heavy. An environment variable override (for example `TADPOLES_HEALTHCHECK_URL`) with `sops` as the fallback would make CI and headless hosts simpler.

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

---

## 5. Testing gap
There is no test suite. The riskiest logic is pure or easy to isolate:
- dedup policy
- EXIF build and write (round-trip a generated JPEG and PNG, check that `DateTimeOriginal` is set and the JPEG pixels are byte-identical)
- chunking and per-item result parsing in `mint` (with a mocked session)
- URL and filename validation

**Needs dependency approval:** `pytest` and `pytest-asyncio` as dev dependencies, plus a `make test` target and a CI step. The alternative with no new dependency is `unittest` with `IsolatedAsyncioTestCase`, which also works.

---

## 6. Feature ideas (optional)
- **Video support:** `filetype` IMAGE-only sniffing silently skips videos. Tadpoles sends videos too, and the Photos API accepts them (EXIF would be skipped for them).
- **Target album:** an optional `--album-id` passed to `batchCreate`.
- **Run summary:** log counts at the end (fetched, deduped, written, uploaded, failed), and include them in the healthcheck ping body.
- **Headless runner:** a systemd timer or launchd example, plus the credential-path change in 2.3.

---

## Proposed implementation batches
Each batch is independently shippable and needs no new dependencies unless marked.

1. **Data safety:**
   - 1.1 mint chunking and per-item results
   - 1.2 lossless EXIF
   - 1.4 caption key
   - 2.1 gitignore and token file permissions
   - 4.2 `.gitignore` cleanup
2. **Resilience:**
   - 1.3 per-entry failure isolation and queue rewrite
   - 1.5 skip already-uploaded images and atomic writes
   - 1.6 `RefreshError`
   - 1.8 and 1.9 dry-run behavior
   - 1.10 null caption
   - 2.2 URL and filename validation
3. **Apps Script:** 1.7 no silent overwrite, 4.9, 4.10. Optionally 3.1 (scope change, needs a decision).
4. **Docs and CI:** 4.1, 2.5, CLAUDE.md gotcha updates.
5. **Needs approval:** tests (section 5), the eslint fix (4.3), and the architecture items 3.2, 3.3, 3.4 and 2.3.

## Decisions needed
- [ ] Approve batches 1–4?
- [ ] 1.3: keep failed entries queued forever, or add a retry cap and a `Failed/` file?
- [ ] 3.1: accept the `gmail.modify` scope for label-based state?
- [ ] 2.3: move credentials to the config directory (breaks your current path)?
- [ ] Section 5: `pytest` or stdlib `unittest`?
