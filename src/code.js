/**
 * Tadpoles Image Downloader Script
 *
 * This script searches Gmail for Tadpoles image links, extracts URLs from messages,
 * and queues.
 *
 * Usage:
 * - Configure search query and run in Google Apps Script environment
 */

// API retry configuration
const RATE_LIMIT = {
  MAX_RETRIES: 3,
  RETRY_DELAY_MS: 1000,
};

const config = PropertiesService.getScriptProperties().getProperties();

function forRealsies() {
  processEmails(false);
}

const extractCaption = (body) => {
  if (!body) {
    return null;
  }
  const lowerBody = body.toLowerCase();
  const signature = "sent via tadpoles";
  if (lowerBody.includes(signature)) {
    return null;
  }
  return body.trim();
};

function validateConfig() {
  if (!config.label_name || config.label_name.trim() === "") {
    throw new Error('Configuration missing required property: "label_name"');
  }
  if (!config.drive_folder_id || config.drive_folder_id.trim() === "") {
    throw new Error('Configuration missing required property: "drive_folder_id"');
  }
}

const sleep = (ms) => Utilities.sleep(ms);

function safeApiCall(apiCall, operationName) {
  let lastError;
  for (let attempt = 1; attempt <= RATE_LIMIT.MAX_RETRIES; attempt++) {
    try {
      return apiCall();
    } catch (error) {
      lastError = error;
      Logger.log(`Attempt ${attempt}/${RATE_LIMIT.MAX_RETRIES} failed for ${operationName}: ${error.message}`);
      if (attempt < RATE_LIMIT.MAX_RETRIES) {
        sleep(RATE_LIMIT.RETRY_DELAY_MS);
      }
    }
  }
  throw new Error(`${operationName} failed after ${RATE_LIMIT.MAX_RETRIES} attempts: ${lastError.message}`);
}

function processEmails(dryRun = true) {
  validateConfig();

  var labelName = config.label_name;

  const label = safeApiCall(
    () => GmailApp.getUserLabelByName(labelName),
    "getUserLabelByName"
  );
  if (!label) {
    throw new Error('Label "' + labelName + '" does not exist in Gmail.');
  }

  const query = `label:${labelName} newer_than:1d`;
  const threads = safeApiCall(
    () => GmailApp.search(query),
    "GmailApp.search"
  );

  const urls = threads.flatMap((thread) =>
    thread.getMessages().flatMap((msg) => {
      const body = msg.getBody();
      const plainBody = msg.getPlainBody();
      const caption = extractCaption(plainBody);
      const urlMatches = [
        ...body.matchAll(/href="(https:\/\/www\.tadpoles\.com\/m\/p\/[^"]+)"/g),
      ];
      return urlMatches.map((m) => ({
        url: m[1],
        msgId: msg.getId(),
        timestamp: msg.getDate().toISOString(),
        caption: caption,
      }));
    }),
  );

  // Deduplicate URLs, keeping the earliest timestamp
  const uniqueUrls = Array.from(
    urls
      .reduce((map, item) => {
        const existing = map.get(item.url);
        if (!existing || new Date(item.timestamp) < new Date(existing.timestamp)) {
          map.set(item.url, item);
        }
        return map;
      }, new Map())
      .values(),
  );
  enqueue(uniqueUrls, dryRun);
}

function enqueue(urls, dryRun = true) {
  const filename = `${new Date().toLocaleDateString("en-CA")}.json`;
  const folder = safeApiCall(
    () => DriveApp.getFolderById(config.drive_folder_id),
    "getFolderById"
  );

  if (dryRun) {
    const fullPath = folder.getName() + "/" + filename;
    Logger.log("Dry run: would save task to " + fullPath);
    Logger.log(JSON.stringify(urls, null, 2));
  } else {
    folder.createFile(filename, JSON.stringify(urls), MimeType.PLAIN_TEXT);
  }
}
