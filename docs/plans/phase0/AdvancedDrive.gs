/**
 * Phase 0 fallback: the same Drive checks through the Advanced Drive service (Drive API v3).
 *
 * Needed because DriveApp.createFolder refused to run under drive.file:
 *   "Specified permissions are not sufficient to call DriveApp.createFolder.
 *    Required permissions: https://www.googleapis.com/auth/drive"
 *
 * Setup: Editor > Services (+) > Drive API, version v3, identifier "Drive".
 * Uses `props` from Code.gs.
 */

const FOLDER_MIME = "application/vnd.google-apps.folder";

/** Check 2 and 3: create a folder and a file under drive.file. */
function adv1_createFolderAndFile() {
  const folder = Drive.Files.create({ name: "tadpoles-queue-spike-adv", mimeType: FOLDER_MIME });
  props.setProperty("spike_adv_folder_id", folder.id);
  Logger.log(`Created folder ${folder.name} id=${folder.id}`);

  const blob = Utilities.newBlob(JSON.stringify([{ step: 1 }]), "application/json");
  const file = Drive.Files.create({ name: "adv1.json", parents: [folder.id], mimeType: "application/json" }, blob);
  Logger.log(`Created file ${file.name} id=${file.id}`);
}

/** Check 4 (critical): in a separate run, reopen the folder by ID, write to it, and list it. */
function adv2_reopenAndWrite() {
  const id = props.getProperty("spike_adv_folder_id");
  if (!id) throw new Error("Run adv1_createFolderAndFile first");

  const folder = Drive.Files.get(id);
  const blob = Utilities.newBlob(JSON.stringify([{ step: 2 }]), "application/json");
  const file = Drive.Files.create(
    { name: `adv2-${Date.now()}.json`, parents: [id], mimeType: "application/json" },
    blob,
  );
  Logger.log(`Reopened folder ${folder.name} and created ${file.name}`);

  const listing = Drive.Files.list({ q: `'${id}' in parents and trashed = false`, fields: "files(name)" });
  Logger.log(`Files visible in folder: ${listing.files.map((f) => f.name).join(", ")}`);
}

/** Check 5: the production folder must NOT be reachable under drive.file. */
function adv3_productionFolderIsNotReachable() {
  const productionFolderId = props.getProperty("production_folder_id");
  if (!productionFolderId) {
    throw new Error("Set the Script Property production_folder_id first (Project Settings > Script Properties)");
  }
  try {
    const folder = Drive.Files.get(productionFolderId);
    Logger.log(`UNEXPECTED: production folder is reachable: ${folder.name}`);
  } catch (error) {
    Logger.log(`EXPECTED: production folder not reachable: ${error.message}`);
  }
}

/** Cleanup: trash the spike folder and remove the spike Script Properties. */
function adv6_cleanup() {
  const id = props.getProperty("spike_adv_folder_id");
  if (id) {
    Drive.Files.update({ trashed: true }, id);
    Logger.log(`Trashed folder id=${id}`);
  }
  props.deleteProperty("spike_adv_folder_id");
  props.deleteProperty("production_folder_id");
  Logger.log("Removed spike Script Properties");
}
