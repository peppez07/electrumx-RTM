# MetaYoshi public HTTPS connection

MetaYoshi connects to `https://144.202.18.233/api/v1/electrum` without a username,
password, cookie, or SSH tunnel. The wallet derives keys, constructs transactions,
and signs on the user's device. Only public chain queries and signed transactions
cross the HTTPS connection. Core's RPC credentials remain private on the server.

## Install on the existing server

Run these commands in the Vultr web console, from the existing server checkout
(normally `/opt/electrumx-RTM`). The public IPv4 address must belong to this server.
Allow inbound TCP **80 and 443** in the Vultr firewall before requesting a
certificate. Port 80 remains necessary for automated certificate renewal.

```sh
cd /opt/electrumx-RTM
git pull --ff-only origin master
sudo docker build -f contrib/Dockerfile -t electrumx-rtm:mainnet .
sudo python3 -S contrib/install_wallet_https.py \
  --image electrumx-rtm:mainnet \
  --public-host 144.202.18.233 \
  --electrum-port 50002 \
  --explorer-port 8080 \
  --agree-acme-tos
```

`--agree-acme-tos` accepts the [Let's Encrypt subscriber agreement](https://letsencrypt.org/repository/).
An optional `--email you@example.com` sets the ACME contact. A DNS name pointing
to the server can replace the IPv4 address. Change the Electrum/explorer ports if
the existing deployment uses different local ports; `--explorer-port 0` disables
the optional sync-status endpoint.

This adds a separate loopback gateway and HTTPS proxy. It does not reinstall or
restart the running ElectrumX indexer, modify Core, or reset the blockchain/index
database. The gateway reads no Core configuration. The installer supports
Debian/Ubuntu with systemd and installs nginx and an isolated Certbot environment.
It refuses to replace unrelated nginx listeners, services, certificate state or
virtual environments. A server already hosting other sites needs an administrator
to arrange the listener configuration before using this dedicated installer.

Public-IP certificates use Let's Encrypt's `shortlived` profile and Certbot 5.4
or newer. The managed renewal timer checks twice daily and reloads nginx after
renewal. It uses normal certificate validation; no certificate-warning exception
or insecure TLS setting is needed in the wallet.

## Update after a pull

The installer saves the selected host, ports, image/native environment and paths
in `/etc/rtm-wallet-http/installation.json`. Reinstall with those saved choices:

```sh
cd /opt/electrumx-RTM
git pull --ff-only origin master
sudo docker build -f contrib/Dockerfile -t electrumx-rtm:mainnet .
sudo python3 -S contrib/install_wallet_https.py --reinstall
```

For a new combined installation, `contrib/install_stack.py --wallet-https`
integrates the same setup. See the repository README for the complete stack
options. Keep using the standalone installer when adding HTTPS to an indexer
that is already synchronizing.

## Verify before using balances

```sh
curl --fail https://144.202.18.233/api/v1/health
curl https://144.202.18.233/api/v1/status
curl --fail https://144.202.18.233/api/v1/electrum \
  -H 'Content-Type: application/json' \
  -H 'X-Electrum-Session: 00000000-0000-4000-8000-000000000001' \
  --data '{"jsonrpc":"2.0","id":1,"method":"server.features","params":[]}'
```

Health is `{"ok":true}` even while indexing. Chain data is usable only after the
index catches up. An Electrum retry error (`-32001`) means unavailable/synchronizing;
inspect `/api/v1/status` and the indexer service rather than changing credentials.
The wallet also verifies the Raptoreum mainnet genesis hash.

```sh
sudo systemctl status rtm-wallet-http rtm-wallet-cert-renew.timer --no-pager
sudo journalctl -u rtm-wallet-http -n 50 --no-pager
sudo systemctl status rtm-electrumx-assets --no-pager
sudo journalctl -u rtm-electrumx-assets -n 80 --no-pager
```

If `phase` is `index_unavailable`, the HTTP explorer cannot reach the indexer;
this is distinct from confirmed synchronization progress. Diagnose the indexer
before relying on balances. A working HTTPS health endpoint alone does not prove
the index is ready.

If the kernel log reports `Memory cgroup out of memory` and kills
`electrumx_server` (sometimes displayed as `electrum_serve`), inspect the backend
limits as well as free host RAM:

```sh
sudo journalctl -k -g 'Memory cgroup out of memory|Killed process' -n 30 --no-pager
sudo systemctl show rtm-electrumx-assets -p MemoryMax -p MemorySwapMax
sudo docker inspect rtm-electrumx-assets --format '{{.HostConfig.Memory}} {{.HostConfig.MemorySwap}}'
```

Omit the Docker command for a native installation. Earlier installer profiles
capped backend RAM at 256 MiB (low) or 512 MiB (standard); RTM indexing can exceed
those limits. Current profiles allow 1024/1536 MiB RAM respectively, each with
512 MiB additional swap allowance. The small index cache is only part of the
process's memory use. Swap must already exist on the host.

After pulling and rebuilding the image (or updating the installed native
environment), update the backend's managed limits with:

```sh
sudo python3 -S contrib/install_stack.py --reinstall
```

This retains the saved profile, Core settings, database and ports, and restarts
the backend with the new limits. Repeat `--config-dir PATH` if it used a custom
directory. An older installation without complete saved options needs its
original stack install arguments once. Reinstalling only wallet HTTPS leaves
backend limits unchanged.

## Server discovery

The explorer finds other wallet gateways so MetaYoshi does not depend on a single
server. Every hour it reads the ENABLED entries of Core's `smartnodelist` (public
IPv4 only) and calls `server.features` and `blockchain.headers.subscribe` at
`https://IP/api/v1/electrum` on each, with normal certificate validation. Gateways
on Raptoreum mainnet with a positive height are listed; known gateways are
rechecked every 10 minutes and dropped two hours after they stop answering.

The list is public at `GET /api/v1/servers` (through the gateway, only fixed
`https://host/api/v1/electrum` URLs, heights and flags are relayed):

```sh
curl https://144.202.18.233/api/v1/servers
```

A smartnode operator becomes discoverable by running this gateway on the node's
registered IP. The node itself needs no change. Settings, in
`/etc/rtm-explorer/explorer.env`:

- `EXPLORER_PEER_DISCOVERY=0` disables probing (the endpoint then lists nothing).
- `EXPLORER_PEER_SEEDS` sets extra comma-separated gateway hosts that are not
  smartnodes (default `144.202.18.233`).

The wallet treats the list as candidates and verifies each server itself.

## Build the extension

In the MetaYoshi checkout:

```sh
git pull --ff-only origin main
npm ci
npm run build:chromium
```

Reload the unpacked extension from `dist-chromium`. The previous built-in
`wss://144.202.18.233:50004` setting migrates to HTTPS; custom server choices are
preserved. WebSocket servers remain supported. HTTP is available for explicit
local testing; use HTTPS for the public server. The HTTPS client polls change
subscriptions and never automatically replays a transaction broadcast.

## Protocol and exposure

`POST /api/v1/electrum` accepts one JSON-RPC 2.0 request with positional parameters
and `X-Electrum-Session` containing a browser-generated UUID. This token retains a
bounded, expiring loopback TCP connection for Electrum pagination; it grants no
authentication privileges. Only explicitly allowed public Electrum methods are
forwarded. Core RPC, Electrum admin RPC, arbitrary proxy destinations and wallet
signing are unavailable through the gateway. Requests have bounded size,
concurrency and duration. Cross-origin callers need no cookies.

If a signed transaction's response is lost, the gateway and wallet report an
unknown submission outcome. Check its transaction ID/history before deciding to
submit again; the transport does not automatically repeat broadcasts.

Certificate support reference: [Let's Encrypt's public-IP Certbot instructions](https://letsencrypt.org/2026/03/11/shorter-certs-certbot/).
