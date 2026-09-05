# Oracle Cloud VM deployment

Everything here lived only on one VM until 2026-09-05. It is the layer that makes
`garmin-givemydata` reachable as a claude.ai connector at
`https://aroncsikos.duckdns.org/mcp` — without it the FitBodAI dashboard's live half
and its sync watchdog both stop working, and rebuilding it means re-running two
root-cause investigations (see the FitBodAI repo's `docs/architecture-overview.md`,
sections 7b and 7c).

Host: Oracle Cloud Ampere A1 (ARM64), Oracle Linux 9, user `opc`,
repo at `/home/opc/garmin-givemydata`, virtualenv at `venv/`.

## What goes where

| File here | Destination on the VM |
|---|---|
| `../run_mcp_http.py` | `/home/opc/garmin-givemydata/run_mcp_http.py` (repo root) |
| `systemd/garmin-sync.service` | `/etc/systemd/system/` |
| `systemd/garmin-sync.service.d/override.conf` | `/etc/systemd/system/garmin-sync.service.d/` |
| `systemd/garmin-sync.timer` | `/etc/systemd/system/` |
| `systemd/garmin-mcp-http.service` | `/etc/systemd/system/` |
| `selinux/garmin_mcp_http.te` | built and loaded, see below |
| `caddy/Caddyfile` | `/etc/caddy/Caddyfile` |

## The two pieces that are not obvious

**`override.conf` is not optional.** The sync launches a snap-packaged Chromium, and
`snap-confine` requires a real user runtime directory. Without
`XDG_RUNTIME_DIR=/run/user/1000` the chromedriver dies instantly on every run, while
working fine from an interactive shell — which is exactly how it went unnoticed for
half a day. It also needs `sudo loginctl enable-linger opc` so `/run/user/1000` exists
without a login session; that is host state, not a file, so it cannot live in this repo:

```bash
sudo loginctl enable-linger opc
```

**The SELinux module is what lets systemd start the server at all.** `init_t` has to be
allowed to read a symlink under `user_home_t`. Build and load it:

```bash
cd deploy/selinux
checkmodule -M -m -o garmin_mcp_http.mod garmin_mcp_http.te
semodule_package -o garmin_mcp_http.pp -m garmin_mcp_http.mod
sudo semodule -i garmin_mcp_http.pp
```

The compiled `.pp` is deliberately not committed — it is generated from the `.te`
above, and a binary in git ages worse than the two commands that produce it.

## Bringing a fresh VM up

```bash
sudo cp deploy/systemd/garmin-sync.service deploy/systemd/garmin-sync.timer \
        deploy/systemd/garmin-mcp-http.service /etc/systemd/system/
sudo mkdir -p /etc/systemd/system/garmin-sync.service.d
sudo cp deploy/systemd/garmin-sync.service.d/override.conf /etc/systemd/system/garmin-sync.service.d/
sudo loginctl enable-linger opc
sudo systemctl daemon-reload
sudo systemctl enable --now garmin-sync.timer garmin-mcp-http.service
```

Check it: `systemctl status garmin-mcp-http.service` and
`journalctl -u garmin-sync.service -n 50 --no-pager`.

## The whole chain, end to end

```
claude.ai connector
      │  https://aroncsikos.duckdns.org/mcp
      ▼
DuckDNS  ──►  the VM's public IP (static, an Oracle reserved address —
      │        there is no updater scheduled, and none is needed)
      ▼
Caddy (:443)  ── TLS, ACME HTTP-01 ──►  reverse_proxy 127.0.0.1:8765
      ▼
run_mcp_http.py  ──►  garmin_mcp.server  ──►  garmin.db (SQLite, mode=ro for queries)
```

The Caddyfile is three lines and holds no secret: certificates come from the default
HTTP-01 challenge, so no DNS API token is involved. Ports **80 and 443 must be open**
— 80 for the ACME challenge, not only 443 — in both the OS firewall and the Oracle
Cloud security list. That is host and cloud-console state, not a file, so it cannot
live here.

## Not captured, and why

The DuckDNS hostname registration is a one-off in Áron's own DuckDNS account, and the
Garmin credentials live in a gitignored `.env` on the VM. Neither is here and neither
will be — see the FitBodAI repo's `docs/integration-credentials.md` for the rule.
