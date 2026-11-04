"""Optional logo for the login page, from BRANDING_DIR (deploy/branding/)."""

from pathlib import Path

from app.config import get_config

NAMES = ("logo.svg", "logo.png", "logo.webp")


def logo_path() -> Path | None:
    folder = Path(get_config().branding_dir)
    for name in NAMES:
        if (folder / name).is_file():
            return folder / name
    return None
