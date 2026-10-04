# Phase 0: Apps Script behaviour checks

Throwaway checks to run before changing the production Apps Script. They cover three assumptions in the audit plan (`docs/plans/2026-09-26-audit-findings.md`) that could not be verified from the analysis environment:

- **2.4:** `DriveApp` works under the narrow `drive.file` scope.
- **4.10:** how Gmail search matches a label that contains a space.
- **1.5:** the `LockService` calls used by the run lock.

Nothing here touches the production project or the production queue folder.

## Setup
1. Open https://script.new to create a new, standalone Apps Script project. Name it "tadpoles phase0 spike".
2. Project Settings: tick "Show appsscript.json manifest file in editor".
3. Replace the contents of `appsscript.json` with this folder's `appsscript.json`.
4. Replace the contents of `Code.gs` with this folder's `Code.gs`.
5. Project Settings > Script Properties > Add script property: name `production_folder_id`, value copied from the production project's `drive_folder_id` property. The ID stays in project settings, never in code. Cleanup removes it.
6. In Gmail, create a label named `spike test` (with the space) and apply it to any one email.

## Run each function separately, in order, and record the result

| # | Function | Expected result | Result |
|---|---|---|---|
| 1 | (first run of anything) | The consent screen lists Drive access only to "specific Google Drive files that you use with this app", plus read-only Gmail. Note the exact wording. | |
| 2 | `step1_createFolderAndFile` | The log shows the created folder ID and file. | |
| 3 | `step2_reopenAndWrite` | Run it separately, after step 2. The log shows the folder reopened, a new file created, and both files listed. **This is the critical check.** | |
| 4 | `step3_productionFolderIsNotReachable` | The log says `EXPECTED: production folder not reachable`. | |
| 5 | Local Drive sync | The `tadpoles-queue-spike` folder and its JSON files appear on the machine that runs Python, and can be read. | |
| 6 | `step4_labelQuoting` | The log shows which of `quoted`, `hyphenated` and `bare` finds at least 1 thread. | |
| 7 | `step5_lockService` | The log shows `tryLock(1000) returned true; hasLock() = true`, then `hasLock() = false` after release. | |
| 8 | `step6_cleanup` | The spike folder is in the Drive trash, and the spike's Script Properties are removed. | |

Afterwards: delete the spike project, and remove the `spike test` label.

## Result so far (2026-10-03)
- Row 1 passed: the consent screen showed Drive access to "only the specific Google Drive files you use with this app", and Gmail "View your email messages and settings" (read-only).
- Row 2 failed with `DriveApp`: `Exception: Specified permissions are not sufficient to call DriveApp.createFolder. Required permissions: https://www.googleapis.com/auth/drive`. So `DriveApp` needs the full `drive` scope.

## Fallback: Advanced Drive service
1. In the editor, click **Services (+)**, choose **Drive API**, version **v3**, identifier `Drive`, then **Add**.
2. Add a new script file named `AdvancedDrive` and paste this folder's `AdvancedDrive.gs`.
3. Run, each separately and in order: `adv1_createFolderAndFile`, `adv2_reopenAndWrite`, `adv3_productionFolderIsNotReachable`, then check Drive sync for `tadpoles-queue-spike-adv`. Run `adv6_cleanup` last (instead of `step6_cleanup`).
4. `step4_labelQuoting` and `step5_lockService` do not use Drive; run them as before.

The same decision rule applies to rows 2 to 4, using the `adv*` functions.

## Results in accountA (2026-10-03)
- Rows 2 to 4 pass with the Advanced Drive service (`adv1` to `adv3`); `DriveApp` needs the full scope.
- Row 6: all three label forms match; use `label:"name"`. Search skips Spam.
- Row 7: `LockService` passes.
- Row 5 was not meaningful: the spike folder was created in accountA, which accountB's rclone does not see.

## Re-run in accountB (decision 2.4, revised 2026-10-04)
The pipeline's Gmail side is moving to accountB. Repeat the Drive part there:
1. Signed in as **accountB**, create a new project at https://script.new, and paste `appsscript.json`, `Code.gs` and `AdvancedDrive.gs` as before. Add the Drive API service (v3, identifier `Drive`).
2. Run `adv1_createFolderAndFile`, then `adv2_reopenAndWrite` as a separate run.
3. On the Python machine: `rclone ls "gdrive:tadpoles-queue-spike-adv"` must list `adv1.json` and the `adv2-...json` file.
4. Run `adv6_cleanup` (skip `adv3`; there is no cross-account folder to test any more). It also deletes the `production_folder_id` property if one was set.
5. Delete the spike project.

Paste the logs and the `rclone ls` output back.
