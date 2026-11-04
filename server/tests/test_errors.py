from app import errors, i18n


def test_stored_errors_round_trip_and_old_text():
    stored = errors.CodedError("llm_http", "nope", status=401).stored()
    want = {"code": "llm_http", "detail": "nope", "params": {"status": 401}}
    assert errors.decode(stored) == want
    assert errors.decode("plain old message")["code"] == "other"
    assert errors.decode(None) is None


def test_every_error_code_has_both_languages():
    en, tr = i18n.load_locales()["en"], i18n.load_locales()["tr"]
    assert set(en) == set(tr)
    assert i18n.translate("tr", "err.llm_http", status=401) == "LLM 401 hatasıyla yanıt verdi."
