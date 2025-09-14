const config = PropertiesService.getScriptProperties().getProperties();

function forRealsies() {
  processEmails(false);
}

function processEmails(dryRun = true) {
  var labelName = config.label_name;

  const label = GmailApp.getUserLabelByName(labelName);
  if (!label) {
    throw new Error('Label "' + labelName + '" does not exist in Gmail.');
  }

  const query = `label:${labelName} newer_than:1d`;
  GmailApp.search(query).forEach((thread) => {
    const messages = thread.getMessages();
    messages.forEach((msg) => {
      const body = msg.getBody();
      const urlMatch = body.match(
        new RegExp('href="(https://www\\.tadpoles\\.com/m/p/[^"]+)"'),
      );
      if (!urlMatch) {
        return;
      }
      const url = urlMatch[1];
      enqueue(url, msg.getId(), dryRun);
    });
  });
}

function enqueue(url, id, dryRun = true) {
  const task = {
    url: url,
    timestamp: new Date().toISOString(),
  };
  const fileName = `task_${id}.json`;
  const folder = DriveApp.getFolderById(config.drive_folder_id);

  if (dryRun) {
    const fullPath = folder.getName() + "/" + fileName;
    Logger.log("Dry run: would save task to " + fullPath);
    Logger.log(JSON.stringify(task, null, 2));
  } else {
    folder.createFile(fileName, JSON.stringify(task), MimeType.PLAIN_TEXT);
  }
}
