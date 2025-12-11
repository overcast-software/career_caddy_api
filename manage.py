#!/usr/bin/env python
import os
import sys

REQUIRED_PY_VERSION = (3, 12, 12)
if sys.version_info[:3] != REQUIRED_PY_VERSION:
    sys.stderr.write(f"Error: This project requires Python {REQUIRED_PY_VERSION[0]}.{REQUIRED_PY_VERSION[1]}.{REQUIRED_PY_VERSION[2]}, but running {sys.version.split()[0]}\n")
    sys.exit(1)


def main():
    """Run administrative tasks."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "job_hunting.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    
    # Auto-add --noinput for tests in CI to prevent interactive prompts
    if "test" in sys.argv and "--noinput" not in sys.argv:
        ci_env = os.environ.get("CI", "").lower() in ("true", "1")
        github_actions = os.environ.get("GITHUB_ACTIONS", "").lower() in ("true", "1")
        if ci_env or github_actions:
            sys.argv.append("--noinput")
            # Optionally keep DB for faster CI runs
            if os.environ.get("CI_KEEPDB", "").lower() in ("true", "1") and "--keepdb" not in sys.argv:
                sys.argv.append("--keepdb")
    
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
