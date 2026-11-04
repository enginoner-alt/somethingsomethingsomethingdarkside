# Official Postgres image + the egress guard (iptables). Nothing else changes:
# same data directory, same environment variables, same entrypoint afterwards.
FROM postgres:16-alpine
RUN apk add --no-cache iptables ip6tables
COPY guard/egress-guard.sh /usr/local/bin/egress-guard.sh
ENTRYPOINT ["/usr/local/bin/egress-guard.sh", "docker-entrypoint.sh"]
CMD ["postgres"]
