import re
from pathlib import Path

from app import i18n
from tests.conftest import csrf_from, login

TEMPLATES = Path(i18n.__file__).parent / "templates"


def test_all_locales_have_the_same_keys():
    locales = i18n.load_locales()
    assert {"en", "tr"} <= locales.keys()
    reference = set(locales["en"])
    for code, data in locales.items():
        assert set(data) == reference, f"{code}.json keys differ from en.json"


def test_every_template_key_exists():
    used = set()
    for path in TEMPLATES.glob("*.html"):
        used |= set(re.findall(r't\(\s*["\']([\w.]+)["\']\s*[,)]', path.read_text()))
    missing = used - set(i18n.load_locales()["en"])
    assert not missing, f"keys used in templates but missing in en.json: {missing}"


def test_translate_fallbacks_and_params():
    assert i18n.translate("tr", "login.title") == "Giriş yap"
    assert i18n.translate("xx", "login.title") == "Sign in"
    assert i18n.translate("en", "no.such.key") == "no.such.key"
    assert i18n.translate("en", "home.welcome", name="Ada") == "Welcome, Ada."


def test_negotiate():
    assert i18n.negotiate("tr-TR,tr;q=0.9,en;q=0.8", "en") == "tr"
    assert i18n.negotiate("de-DE", "en") == "en"
    assert i18n.negotiate(None, "tr") == "tr"


def test_accept_language_and_switch(client, admin, db):
    assert "Giriş yap" in client.get("/login", headers={"accept-language": "tr"}).text

    login(client, "admin", "admin-password-123")
    home = client.get("/")
    client.post("/lang", data={"locale": "tr", "csrf": csrf_from(home.text), "next": "/"})
    assert "Kayıtlar" in client.get("/").text
    db.refresh(admin)
    assert admin.locale == "tr"
