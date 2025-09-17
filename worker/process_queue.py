import json
import mimetypes
from pathlib import Path
from urllib.parse import urlparse
import requests
import shutil

import typer


def process_file(file_path: Path, done_dir: Path, images_dir: Path, dry_run: bool):
    with open(file_path) as f:
        data = json.load(f)

    for url in data["urls"]:
        # Setting query param d=t causes redirect to backing image
        resp = requests.get(url["url"], params={"d": "t"})
        resp.raise_for_status()

        redirected_url = resp.url

        content_type = resp.headers["Content-Type"]
        if not (ext := mimetypes.guess_extension(content_type.split(";")[0])):
            ext = ".jpg"  # Default to .jpg if unknown
        filename = Path(urlparse(redirected_url).path).with_suffix(ext).name

        if not dry_run:
            with open(images_dir / file_path.name / filename, "wb") as out:
                out.write(resp.content)

        print(f"Downloaded: {filename}")

    # Move processed JSON to Done
    if not dry_run:
        shutil.move(str(file_path), done_dir / file_path.name)


def main(
    queue_dir: Path = typer.Option(..., help="Path to queue directory"),
    images_dir: Path = typer.Option(..., help="Path to images directory"),
    dry_run: bool = typer.Option(True, help="Print actions without making changes"),
):
    done_dir = queue_dir / "Done"

    # Ensure directories exist
    done_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)

    for file_path in queue_dir.glob("*.json"):
        process_file(file_path, done_dir, images_dir, dry_run)


if __name__ == "__main__":
    typer.run(main)
