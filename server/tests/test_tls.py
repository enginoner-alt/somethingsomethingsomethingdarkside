import subprocess

from app import tls


def _make_ca(tmp_path, name="Darkside Test CA"):
    key, cert = tmp_path / "ca.key", tmp_path / "ca.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            f"/CN={name}",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    return cert


def _subjects(ctx):
    return {dict(x[0] for x in c["subject"]).get("commonName") for c in ctx.get_ca_certs()}


def test_extra_ca_dir_is_trusted_and_garbage_ignored(tmp_path, cfg, monkeypatch):
    ca_dir = tmp_path / "ca"
    ca_dir.mkdir()
    (ca_dir / "company.pem").write_bytes(_make_ca(tmp_path).read_bytes())
    (ca_dir / "broken.pem").write_text("not a certificate")
    (ca_dir / "README.md").write_text("ignored")
    monkeypatch.setattr(cfg, "extra_ca_dir", str(ca_dir))
    tls.ssl_context.cache_clear()
    try:
        ctx = tls.ssl_context()
        assert "Darkside Test CA" in _subjects(ctx)
        assert len(ctx.get_ca_certs()) > 50  # public CAs still trusted
    finally:
        tls.ssl_context.cache_clear()


def test_missing_dir_is_fine(tmp_path, cfg, monkeypatch):
    monkeypatch.setattr(cfg, "extra_ca_dir", str(tmp_path / "nope"))
    tls.ssl_context.cache_clear()
    try:
        assert "Darkside Test CA" not in _subjects(tls.ssl_context())
    finally:
        tls.ssl_context.cache_clear()
