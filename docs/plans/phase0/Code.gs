/**
 * Phase 0 spike: verify Apps Script behaviour before changing production (audit findings 1.5, 2.4, 4.10).
 *
 * Run in a NEW, throwaway Apps Script project, never in the production one.
 * Run each function separately from the editor, in order, and record the log output.
 * See README.md in this folder for the checklist.
 */


// A throwaway Gmail label containing a space, applied to at least one email (checklist step 6).
const SPACED_LABEL = "spike test";

const props = PropertiesService.getScriptProperties();

/** Check 2 and 3: create a folder and a file under drive.file. */
function step1_createFolderAndFile() {
  const folder = DriveApp.createFolder("tadpoles-queue-spike");
  props.setProperty("spike_folder_id", folder.getId());
  Logger.log(`Created folder ${folder.getName()} id=${folder.getId()}`);

  const file = folder.createFile("step1.json", JSON.stringify([{ step: 1 }]), MimeType.JSON);
  Logger.log(`Created file ${file.getName()} id=${file.getId()}`);
}

/** Check 4 (critical): in a separate run, reopen the folder by ID and write to it. */
function step2_reopenAndWrite() {
  const id = props.getProperty("spike_folder_id");
  if (!id) throw new Error("Run step1_createFolderAndFile first");

  const folder = DriveApp.getFolderById(id);
  const file = folder.createFile(`step2-${Date.now()}.json`, JSON.stringify([{ step: 2 }]), MimeType.JSON);
  Logger.log(`Reopened folder ${folder.getName()} and created ${file.getName()}`);

  const names = [];
  const files = folder.getFiles();
  while (files.hasNext()) names.push(files.next().getName());
  Logger.log(`Files visible in folder: ${names.join(", ")}`);
}

/**
 * Check 5: the production folder must NOT be reachable under drive.file.
 * Reads the folder ID from this spike project's Script Property `production_folder_id`,
 * so the ID never appears in code. Nothing is written to that folder.
 */
function step3_productionFolderIsNotReachable() {
  const productionFolderId = props.getProperty("production_folder_id");
  if (!productionFolderId) {
    throw new Error("Set the Script Property production_folder_id first (Project Settings > Script Properties)");
  }
  try {
    const folder = DriveApp.getFolderById(productionFolderId);
    Logger.log(`UNEXPECTED: production folder is reachable: ${folder.getName()}`);
  } catch (error) {
    Logger.log(`EXPECTED: production folder not reachable: ${error.message}`);
  }
}

/** Finding 4.10: which Gmail search form matches a label containing a space. */
function step4_labelQuoting() {
  const forms = {
    quoted: `label:"${SPACED_LABEL}"`,
    hyphenated: `label:${SPACED_LABEL.replace(/ /g, "-")}`,
    bare: `label:${SPACED_LABEL}`,
  };
  for (const [name, query] of Object.entries(forms)) {
    try {
      Logger.log(`${name}: ${query} -> ${GmailApp.search(query).length} thread(s)`);
    } catch (error) {
      Logger.log(`${name}: ${query} -> ERROR ${error.message}`);
    }
  }
}

/** Finding 4.10 diagnostic: check the label exists and has threads, without using search. */
function step4b_labelDiagnostic() {
  const names = GmailApp.getUserLabels().map((label) => label.getName());
  Logger.log(`Labels containing "spike": ${JSON.stringify(names.filter((n) => n.toLowerCase().includes("spike")))}`);

  const label = GmailApp.getUserLabelByName(SPACED_LABEL);
  if (!label) {
    Logger.log(`No label named exactly "${SPACED_LABEL}"`);
    return;
  }
  Logger.log(`getUserLabelByName("${SPACED_LABEL}").getThreads(0, 10) -> ${label.getThreads(0, 10).length} thread(s)`);
  Logger.log(`label:"${SPACED_LABEL}" in:anywhere -> ${GmailApp.search(`label:"${SPACED_LABEL}" in:anywhere`).length} thread(s)`);
}

/** Finding 1.5: confirm the LockService calls the design relies on. */
function step5_lockService() {
  const lock = LockService.getScriptLock();
  const acquired = lock.tryLock(1000);
  Logger.log(`tryLock(1000) returned ${acquired}; hasLock() = ${lock.hasLock()}`);
  if (acquired) {
    lock.releaseLock();
    Logger.log(`After releaseLock(): hasLock() = ${lock.hasLock()}`);
  }
}

/** Cleanup: trash the spike folder and its files. */
function step6_cleanup() {
  const id = props.getProperty("spike_folder_id");
  if (!id) {
    Logger.log("Nothing to clean up");
    return;
  }
  DriveApp.getFolderById(id).setTrashed(true);
  props.deleteProperty("spike_folder_id");
  props.deleteProperty("production_folder_id");
  Logger.log(`Trashed folder id=${id}; removed spike Script Properties`);
}
