"""Errors that reach a user's screen carry a code (translated in the browser,
key `err.<code>`), optional params for the text, and an optional technical
detail shown as-is. Stored errors (Transcript.error, Insight.error) are kept
as JSON of the same shape so the page can show them in the reader's language."""

import json


class CodedError(Exception):
    def __init__(self, code: str, detail: str = "", **params: object):
        self.code, self.detail, self.params = code, detail, params
        super().__init__(f"{code}: {detail}" if detail else code)

    def stored(self) -> str:
        return encode(self.code, self.detail, **self.params)


def encode(code: str, detail: str = "", **params: object) -> str:
    return json.dumps({"code": code, "detail": detail[:1000], "params": params})


def decode(text: str | None) -> dict | None:
    """Stored error -> {"code", "detail", "params"}; plain old text -> code "other"."""
    if not text:
        return None
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "code" in data:
            return {
                "code": data["code"],
                "detail": data.get("detail", ""),
                "params": data.get("params", {}),
            }
    except ValueError:
        pass
    return {"code": "other", "detail": text, "params": {}}
