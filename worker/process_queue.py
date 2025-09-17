import os
import json
import requests
from bs4 import BeautifulSoup
from pathlib import Path
import shutil

# Adjust these paths to your synced Google Drive folders
QUEUE_DIR = Path.home() / "GoogleDrive/EmailTasks"
DONE_DIR = QUEUE_DIR / "processed"
IMAGES_DIR = Path.home() / "Downloads/email_images"

# Ensure directories exist
DONE_DIR.mkdir(parents=True, exist_ok=True)
IMAGES_DIR.mkdir(parents=True, exist_ok=True)


def process_file(file_path: Path):
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    urls = data.get("urls", [])
    email_id = data.get("email_id", file_path.stem)

    for idx, url in enumerate(urls, start=1):
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()

            soup = BeautifulSoup(resp.text, "html.parser")
            # TODO: Update CSS selector for your image
            img_tag = soup.select_one("img.target-class")
            if not img_tag or not img_tag.get("src"):
                print(f"No image found at {url}")
                continue

            img_url = img_tag["src"]
            if not img_url.startswith("http"):
                from urllib.parse import urljoin

                img_url = urljoin(url, img_url)

            filename = f"{email_id}_{idx}_{os.path.basename(img_url.split('?')[0])}"
            out_path = IMAGES_DIR / filename

            img_data = requests.get(img_url, timeout=10).content
            with open(out_path, "wb") as out:
                out.write(img_data)

            print(f"Downloaded: {filename}")

        except Exception as e:
            print(f"Error processing {url}: {e}")

    # Move processed JSON to Done
    shutil.move(str(file_path), DONE_DIR / file_path.name)


def main():
    for file_path in QUEUE_DIR.glob("*.json"):
        process_file(file_path)


if __name__ == "__main__":
    main()
