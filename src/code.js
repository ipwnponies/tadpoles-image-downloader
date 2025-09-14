const DRIVE_FOLDER_ID = "YOUR_DRIVE_FOLDER_ID";

function processEmails() {
  var config = PropertiesService.getScriptProperties().getProperties();
  var labelName = config.LABEL_NAME;

  const label = GmailApp.getUserLabelByName(labelName);
  if (!label) {
    throw new Error('Label "' + labelName + '" does not exist in Gmail.');
  }
  const threads = label.getThreads(0, 50);
  threads.forEach((thread) => {
    const messages = thread.getMessages();

    messages.forEach((element) => {
      const msg = messages[j];
      const body = msg.getBody();
      const urlMatch = body.match(
        /href="(https:\/\/www\.tadpoles\.com\/[^"]+)"/,
      );
      if (!urlMatch) {
        return;
      }
      const url = urlMatch[1];
      enqueue(url, msg.getId());
    });
  });
}

function enqueue(url, id, dryRun = true) {
  const task = {
    url: url,
    timestamp: new Date().toISOString(),
  };
  const fileName = `task_${id}.json`;
  if (dryRun) {
    Logger.log("Dry run: would save task to " + fileName);
    Logger.log(JSON.stringify(task, null, 2));
  } else {
    // TODO: Actual save logic here
  }
}
