"""Screenshot storage abstraction. Local disk now, S3 later."""

from pathlib import Path


class ScreenshotStore:
    """Read screenshots from local disk.

    When S3 is needed, create an S3ScreenshotStore with the same interface
    and select via SCREENSHOT_BACKEND env var.
    """

    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir)

    def list_for_scrape(self, scrape_id: int) -> list[dict]:
        """Return [{filename, path}] for a scrape's screenshots."""
        if not self.base_dir.is_dir():
            return []
        pattern = f"scrape_{scrape_id}_*.png"
        return [
            {"filename": f.name}
            for f in sorted(self.base_dir.glob(pattern))
        ]

    def read(self, filename: str) -> Path | None:
        """Return full path if file exists, None otherwise."""
        if ".." in filename or "/" in filename:
            return None
        path = self.base_dir / filename
        return path if path.is_file() else None
