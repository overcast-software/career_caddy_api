"""Screenshot storage abstraction. Local disk now, S3 later."""

from datetime import datetime, timezone
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
        """Return [{filename, size, taken_at}] for a scrape's screenshots.

        ``size`` is the file size in bytes; ``taken_at`` is the file's
        modification time as an ISO-8601 UTC string. Both fields are
        cheap stat() reads — no PNG parsing.
        """
        scrape_dir = self.base_dir / str(scrape_id)
        if not scrape_dir.is_dir():
            return []
        out = []
        for f in sorted(scrape_dir.glob("*.png")):
            try:
                st = f.stat()
                taken_at = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
                size = st.st_size
            except OSError:
                taken_at = None
                size = None
            out.append({"filename": f.name, "size": size, "taken_at": taken_at})
        return out

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
