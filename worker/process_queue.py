import os
import json
import requests
from bs4 import BeautifulSoup
from pathlib import Path
import shutil

import typer


    with open(file_path, "r", encoding="utf-8") as f:
def process_file(file_path: Path, done_dir: Path, images_dir: Path, dry_run: bool):
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


def main(
    queue_dir: Path = typer.Option(..., help="Path to queue directory"),
    done_dir: Path = typer.Option(..., help="Path to done directory"),
    images_dir: Path = typer.Option(..., help="Path to images directory"),
    dry_run: bool = typer.Option(True, help="Print actions without making changes"),
):
    # Ensure directories exist
    done_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)

    for file_path in queue_dir.glob("*.json"):
        process_file(file_path, done_dir, images_dir, dry_run)


if __name__ == "__main__":
    typer.run(main)
