"""Screenshot storage abstraction. Local disk now, S3 later."""

from pathlib import Path


class ScreenshotStore:
    """Read screenshots from local disk.

    Screenshots are stored in per-scrape subdirectories:
        {base_dir}/{scrape_id}/{domain}_{timestamp}.png

    When S3 is needed, create an S3ScreenshotStore with the same interface
    and select via SCREENSHOT_BACKEND env var.
    """

    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir)

    def list_for_scrape(self, scrape_id: int) -> list[dict]:
        """Return [{filename}] for a scrape's screenshots."""
        scrape_dir = self.base_dir / str(scrape_id)
        if not scrape_dir.is_dir():
            return []
        return [
            {"filename": f.name}
            for f in sorted(scrape_dir.glob("*.png"))
        ]

    def read(self, scrape_id: int, filename: str) -> Path | None:
        """Return full path if file exists, None otherwise."""
        if ".." in filename or "/" in filename:
            return None
        path = self.base_dir / str(scrape_id) / filename
        return path if path.is_file() else None

    def save(self, scrape_id: int, filename: str, file_obj) -> Path:
        """Save an uploaded file to {base_dir}/{scrape_id}/{filename}."""
        if ".." in filename or "/" in filename:
            raise ValueError("Invalid filename")
        scrape_dir = self.base_dir / str(scrape_id)
        scrape_dir.mkdir(parents=True, exist_ok=True)
        path = scrape_dir / filename
        with open(path, "wb") as f:
            for chunk in file_obj.chunks():
                f.write(chunk)
        return path
