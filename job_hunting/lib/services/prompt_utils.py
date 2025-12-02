import os
from datetime import datetime
from typing import Optional


def write_prompt_to_file(
    prompt: str, kind: str, identifiers: Optional[dict] = None, directory: Optional[str] = None
) -> str:
    """
    Write a prompt to disk with consistent naming and location.

    Args:
        prompt: The prompt text to write
        kind: Type of prompt (e.g., "answer", "cover_letter", "summary")
        identifiers: Dict of ID values for filename generation
        directory: Override directory (defaults to PROMPT_LOG_DIR env or runtime/prompts)

    Returns:
        Full path to written file, or empty string on failure
    """
    try:
        # Determine directory
        if directory is None:
            directory = os.getenv("PROMPT_LOG_DIR", "runtime/prompts")

        # Ensure directory exists
        os.makedirs(directory, exist_ok=True)

        # Generate timestamp
        now = datetime.utcnow()
        timestamp = now.strftime("%Y%m%d_%H%M%S") + f"{now.microsecond // 1000:03d}"

        # Build ID suffix
        id_suffix = ""
        if identifiers:
            # Map common keys to short tokens
            key_map = {
                "question_id": "q",
                "application_id": "a",
                "user_id": "u",
                "job_post_id": "jp",
                "resume_id": "r",
                "company_id": "c",
                "scrape_id": "s"
            }

            parts = []
            for key, value in identifiers.items():
                if value is not None:
                    # Get short key or sanitize original
                    short_key = key_map.get(key, key)
                    # Sanitize key to only keep alphanumeric, dash, underscore
                    sanitized_key = "".join(c for c in short_key if c.isalnum() or c in "-_")
                    if sanitized_key:
                        parts.append(f"{sanitized_key}{value}")

            if parts:
                id_suffix = "_" + "_".join(parts)

        # Build filename
        filename = f"{timestamp}_{kind}{id_suffix}.md"
        filepath = os.path.join(directory, filename)

        # Write file
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(prompt)

        return filepath

    except Exception:
        # Fail-safe: never break request handling
        return ""
