# Extra trusted CAs (optional)

Only needed if a server **the app itself calls** (your Keycloak/IdP, later the
LLM endpoint) uses a certificate from an **internal or self-signed CA**.

Drop the CA certificate here as a `.pem` or `.crt` file (PEM format, the one
that starts with `-----BEGIN CERTIFICATE-----`) and restart:

    ./darkside up

Public CAs (Let's Encrypt, DigiCert, Sectigo, ...) are trusted already. Put
nothing here for them.

This folder is for CA *certificates* only — never put private keys here.
