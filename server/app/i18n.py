"""Translations: one JSON file per language in app/locales/.

Adding a language = dropping `xx.json` next to `en.json` (same keys) and
setting `_meta.name` to the language's own name. `en.json` is the reference:
missing keys fall back to English, then to the key itself.
"""

import json
from functools import lru_cache
from pathlib import Path

LOCALES_DIR = Path(__file__).parent / "locales"
FALLBACK = "en"


@lru_cache
def load_locales() -> dict[str, dict[str, str]]:
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(LOCALES_DIR.glob("*.json"))
    }


def available_locales() -> dict[str, str]:
    """{code: native name}, for the language switcher."""
    return {code: data.get("_meta.name", code) for code, data in load_locales().items()}


def translate(locale: str, key: str, **params: object) -> str:
    locales = load_locales()
    text = locales.get(locale, {}).get(key) or locales.get(FALLBACK, {}).get(key) or key
    return text.format(**params) if params else text


def negotiate(accept_language: str | None, default: str) -> str:
    """Pick the best available locale from an Accept-Language header."""
    available = load_locales()
    for part in (accept_language or "").split(","):
        code = part.split(";")[0].strip().lower().split("-")[0]
        if code in available:
            return code
    return default if default in available else FALLBACK
