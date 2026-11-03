#!/bin/sh
# Make a throwaway certificate chain shaped like a real one:
#   root CA  →  intermediate CA  →  wildcard *.DOMAIN (+ DOMAIN)
#
# Output (in OUT_DIR, default ./test-certs):
#   fullchain.pem  wildcard + intermediate  → deploy/certs/fullchain.pem
#   privkey.pem    wildcard key             → deploy/certs/privkey.pem
#   ca.pem         root CA certificate      → deploy/ca/ (so the app trusts it)
#                                             and into browsers/phones you test with
#
# FOR TESTING ONLY. With a real (public) wildcard certificate you don't need this.
#
#   DOMAIN=darkside.test OUT_DIR=./test-certs ./make-test-certs.sh
set -eu

DOMAIN=${DOMAIN:-darkside.test}
OUT_DIR=${OUT_DIR:-./test-certs}
mkdir -p "$OUT_DIR"
cd "$OUT_DIR"

# Root CA
openssl req -x509 -newkey rsa:3072 -nodes -days 30 -sha256 \
  -subj "/CN=Darkside Test Root CA" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -keyout ca.key -out ca.pem 2>/dev/null

# Intermediate CA
openssl req -newkey rsa:3072 -nodes -subj "/CN=Darkside Test Intermediate CA" \
  -keyout intermediate.key -out intermediate.csr 2>/dev/null
openssl x509 -req -in intermediate.csr -CA ca.pem -CAkey ca.key -CAcreateserial \
  -days 30 -sha256 -out intermediate.pem \
  -extfile /dev/stdin 2>/dev/null <<EOF
basicConstraints=critical,CA:TRUE,pathlen:0
keyUsage=critical,keyCertSign,cRLSign
EOF

# Wildcard leaf
openssl req -newkey rsa:2048 -nodes -subj "/CN=*.$DOMAIN" \
  -keyout privkey.pem -out wildcard.csr 2>/dev/null
openssl x509 -req -in wildcard.csr -CA intermediate.pem -CAkey intermediate.key \
  -CAcreateserial -days 30 -sha256 -out wildcard.pem \
  -extfile /dev/stdin 2>/dev/null <<EOF
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=DNS:*.$DOMAIN,DNS:$DOMAIN
EOF

cat wildcard.pem intermediate.pem > fullchain.pem
rm -f ./*.csr ./*.srl
openssl verify -CAfile ca.pem -untrusted intermediate.pem wildcard.pem
echo "Done: $(pwd)/fullchain.pem, privkey.pem, ca.pem (for *.$DOMAIN)"
