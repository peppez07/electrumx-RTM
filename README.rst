===========================
ElectrumX for Raptoreum RTM
===========================

  :Licence: MIT
  :Language: Python (>= 3.7)
  :Original Author: Neil Booth

**GitHub project:** `peppez07/electrumx-RTM <https://github.com/peppez07/electrumx-RTM>`_

An Electrum protocol server for Raptoreum mainnet, with native RTM coin indexing
and an additive asset extension. It connects to a Raptoreum Core full node and
indexes transactions for efficient wallet queries.

This repository contains the server. A compatible wallet client is required to
display assets and construct and sign asset transfers locally. Private keys and
seed phrases are never needed by ElectrumX.

Features
========

* Native RTM balances, transaction history, unspent outputs, subscriptions, and
  raw transaction broadcasting through the existing Electrum protocol.
* RTM-specific asset transaction parsing, independent of Dash transaction types.
* Fungible and unique asset output indexing, including unique token ranges.
* Confirmed asset metadata, supply, mint count, root and child asset names,
  ownership changes, and metadata updates.
* Asset balances, paginated discovery, unspent outputs, and transaction history.
  Discovery includes assets whose holdings have been fully spent.
* Pending asset transfers, mempool spends, balance deltas, and subscriptions.
* Asset state and undo records committed alongside native UTXOs, with rollback
  support for chain reorganizations.
* Asset quantities kept separate from RTM amounts. Asset-bearing zero-RTM outputs
  are excluded from native coin selection.

VPS installation: mainnet Core + ElectrumX + explorer
=====================================================

This guide installs the ElectrumX server and explorer on a Linux VPS running
systemd, using an **existing Raptoreum mainnet Core** on the same host. It does
not download, upgrade, restart or reindex Core. A fresh VPS needs Core installed
and running before completing this guide. Use your normal SSH account with
``sudo``; root can run the same commands without ``sudo``.

The recommended route is Docker: the repository image pins Python 3.10 and
includes the native LevelDB build dependencies. This avoids depending on the
host's Python version for the server. The host only needs standard-library
Python 3 for provisioning. Ubuntu 22.04/24.04 LTS are suitable host examples;
choose an official Core binary compatible with your host and CPU architecture.

Use SSD storage with space for the unpruned Core blockchain **and a separate
ElectrumX index**. Synchronization and indexing need additional working space.
Check ``df -h``, ``free -h`` and ``swapon --show`` before starting. Small VPS
instances can use the low profile below; its limits apply to ElectrumX, not to
Core or the entire VPS. No fixed minimum disk size or full-chain RAM requirement
has been benchmarked by this project.

1. Prepare the mainnet Core node
--------------------------------

The implementation is tested against the official
`Raptoreum Core 2.0.3.01 mainnet release
<https://github.com/Raptor3um/raptoreum/releases/tag/2.0.3.01-mainnet>`_.
For a new Core installation, select the binary for your system and verify it
against the release's ``checksums.txt``. Use mainnet release assets; do not
substitute a testnet build. Existing working mainnet nodes do not need to be
reinstalled for this ElectrumX setup.

Core must run with transaction and asset indexes enabled and pruning disabled.
The following shows the required settings in its actual ``raptoreum.conf``::

    chain=main
    testnet=0
    regtest=0
    server=1
    txindex=1
    assetindex=1
    prune=0
    rpcbind=127.0.0.1
    rpcallowip=127.0.0.1
    rpcconnect=127.0.0.1
    rpcport=10225
    rpcuser=rtm_rpc
    rpcpassword=REPLACE_WITH_A_LONG_RANDOM_SECRET

Replace the password before starting Core; ``openssl rand -hex 32`` can generate
one. Keep the config readable only by the Core operator and the users who need
it. The installer requires explicit ``rpcuser``/``rpcpassword`` in this config;
cookie-only authentication and ``includeconf`` are not supported. Keep RPC bound
to loopback. No wallet keys or seed phrases belong in the ElectrumX setup.

For an existing Core node, check its current configuration before changing it.
Enabling missing indexes can require a controlled Core restart and rebuild;
complete that using Core's procedures. The ElectrumX installer cannot rebuild
missing Core indexes. Do not delete an existing Core data directory.

Use your installed ``raptoreum-cli`` with the same config and data-directory
arguments used by your Core service to inspect ``getblockchaininfo``. Core must
report ``chain: main``. Initial synchronization is complete when
``initialblockdownload`` is false and ``blocks`` has caught up with ``headers``.
ElectrumX setup can run while Core is synchronizing: its start timer waits for
these conditions before launching the backend.

2. Install host tools and Docker
--------------------------------

On Ubuntu::

    sudo apt-get update
    sudo apt-get install -y git curl ca-certificates python3 openssl

Install Docker Engine using the
`official Ubuntu installation guide <https://docs.docker.com/engine/install/ubuntu/>`_.
If Docker is already installed and working, keep that installation. The Docker
instructions explain package conflicts; do not mix distribution and Docker
repository packages. Then verify the daemon::

    sudo systemctl enable --now docker
    sudo docker version
    sudo docker run --rm hello-world

The stack uses Docker host networking on Linux so the containers can reach
Core's localhost RPC. Docker Desktop is not required on the VPS.

3. Download the default branch and build the image
--------------------------------------------------

On a new checkout::

    sudo git clone --branch master --single-branch \
        https://github.com/peppez07/electrumx-RTM.git /opt/electrumx-RTM
    cd /opt/electrumx-RTM
    sudo docker build -f contrib/Dockerfile -t electrumx-rtm:mainnet .

If that directory already contains this repository, use the update procedure
below instead of cloning over it. ``master`` is this repository's default branch.
The image is built from this checkout; it is not pulled from an unrelated image
publisher. Keep the checkout outside protected home directories when using the
native service alternative.

4. Configure the complete stack
-------------------------------

Set these two values for your server. The Core config path below is an example;
use the file actually read by your running Core service::

    CORE_CONFIG=/var/lib/rtm-mainnet/core/raptoreum.conf
    PUBLIC_HOST=YOUR_SERVER_IP
    sudo test -f "$CORE_CONFIG"
    cd /opt/electrumx-RTM

Preview the service configuration without reading RPC credentials or changing
the host::

    sudo python3 -S contrib/install_stack.py \
        --image electrumx-rtm:mainnet \
        --core-config "$CORE_CONFIG" \
        --with-explorer --public-host "$PUBLIC_HOST" \
        --profile low --wallet-api --dry-run

Then run the same command without ``--dry-run``::

    sudo python3 -S contrib/install_stack.py \
        --image electrumx-rtm:mainnet \
        --core-config "$CORE_CONFIG" \
        --with-explorer --public-host "$PUBLIC_HOST" \
        --profile low --wallet-api

``--wallet-api`` enables HTTP decoding and relay of client-signed transactions.
Omit it on a fresh install for a read-only HTTP explorer. Keys, transaction
building and signing remain in the wallet client. This repository does not
install a browser-extension wallet. Configure HTTPS/WSS before connecting a
production wallet, as described below.

The low profile sets a 16 MB index cache, 256 MiB backend RAM limit, 256 MiB
additional swap allowance, half a CPU and 50 sessions. ``--profile auto`` chooses
low below 4 GiB host RAM and standard otherwise. The explorer has a separate
192 MiB Docker memory limit and half-CPU limit. These ceilings do not reserve
RAM; Core and the OS need their own resources. Swap allowance requires swap
already available on the host; the installer does not create it.

``--db-dir PATH`` selects a different fresh database directory. Custom listeners
use ``--port`` (HTTP), ``--tcp-port``, ``--ws-port``, ``--rpc-port`` (admin RPC)
and ``--wss-port``. Ports must be distinct and separate from Core RPC. Record
these options and repeat them during future setup runs.

Setup creates dedicated service accounts and these files/services:

* ``rtm-electrumx-assets.service`` -- one asset-capable RTM backend.
* ``rtm-electrumx-assets.timer`` -- retries its startup while Core synchronizes.
* ``rtm-explorer.service`` -- the web interface and HTTP API.
* ``/etc/rtm-electrumx/`` -- restricted backend environment, RPC settings and
  optional TLS copies. ``/opt/rtm-electrumx/core_ready.py`` is the startup gate.
* ``/etc/rtm-explorer/`` -- restricted explorer environment and minimal Core
  credential copy. HTTP relay activation is saved here.
* ``/var/lib/rtm-electrumx/assets-v1`` -- a new, separate ElectrumX database.

**Do not reuse an old coin-only database.** Asset indexing rebuilds from genesis;
keep old databases separate for rollback. If migrating from a coin-only service,
stop and disable that specific old service yourself before running the new
indexer. For an old unit actually named ``rtm-electrumx.service``, that is::

    sudo systemctl disable --now rtm-electrumx.service

Only use that command if it is your obsolete indexer. Do not stop Core or a
service with an unrelated purpose. The installer rejects unrelated units using
its managed service names and does not automatically retire other indexers.

5. Check synchronization and open the explorer
----------------------------------------------

After setup::

    sudo systemctl status rtm-explorer.service --no-pager
    sudo systemctl list-timers rtm-electrumx-assets.timer --no-pager
    sudo systemctl status rtm-electrumx-assets.service --no-pager
    curl -fsS http://127.0.0.1:8080/api/v1/health
    curl -fsS http://127.0.0.1:8080/api/v1/status | python3 -m json.tool

``health`` confirms the explorer process responds; it does not prove the chain
is ready. While Core is downloading, an inactive backend with
``Result=exec-condition`` is expected: the timer checks again about every minute.
After Core finishes, ElectrumX still needs to build its own index. Do not treat
that second synchronization stage as an installation failure.

The status response reports ``core_syncing``, ``index_unavailable`` or
``index_syncing`` until both services agree on the indexed mainnet tip. Wait
for ``ready: true`` and ``phase: ready`` before checking balances or using coin
and asset functionality. ``core_unavailable`` means Core RPC could not be read;
``core_stale: true`` marks last-known progress rather than a current reading.

Open ``http://YOUR_SERVER_IP:8080/`` for the explorer, or
``http://YOUR_SERVER_IP:8080/#/api`` for the API reference. Check the VPS provider's
firewall as well as the host firewall. The installer adds an explorer rule only
when UFW is already active; it does not enable UFW or change provider firewalls.
Inspect listeners and host rules with::

    sudo ss -lntp
    sudo ufw status

Default ports:

.. list-table::
   :header-rows: 1
   :widths: 14 28 58

   * - TCP port
     - Service
     - Exposure
   * - 8080
     - Explorer / HTTP API
     - Public for initial HTTP access; proxy with HTTPS for production.
   * - 50004
     - Wallet WSS
     - Public only when a trusted certificate/key is supplied.
   * - 50002
     - Electrum TCP
     - Loopback; no public firewall rule is needed.
   * - 50003
     - Electrum WebSocket
     - Loopback; can be used behind a TLS WebSocket proxy.
   * - 8001
     - ElectrumX admin RPC
     - Loopback only; never publish this port.
   * - 10225
     - Core RPC
     - Loopback only; never publish this port.

Keep your existing SSH access allowed. Port 10226, if used by your Core node for
peer connections, is separate from this stack's HTTP/Electrum ports.

6. Add secure wallet and browser access
---------------------------------------

For direct wallet WSS, obtain a browser-trusted certificate for your domain,
then rerun the complete setup command with the same settings plus::

    --tls-cert /path/to/fullchain.pem --tls-key /path/to/privkey.pem

This adds ``wss://YOUR_DOMAIN:50004`` and opens 50004 in an already active UFW.
Also permit that TCP port in the VPS provider firewall. The installer validates
and copies the pair; it does not obtain or automatically renew certificates.
After renewal, rerun setup with the updated certificate/key paths to refresh
the copies and restart the listener. ``--no-tls`` explicitly disables WSS.

For the browser API, configure a trusted HTTPS reverse proxy for your domain
pointing to ``127.0.0.1:8080``. On a new installation use ``--bind 127.0.0.1``
with the complete setup command when only the proxy should reach HTTP. The
proxy's public ports, usually 80/443, need their own firewall rules. Forward
both ``/api/`` and the UI/static paths. A proxy for the internal 50003 WebSocket
listener must support WebSocket upgrades, long-lived connections and
subscription notifications. HTTPS for the explorer does not itself configure
a wallet WSS endpoint.

If changing bind address or ports on an existing installation, first make the
matching explicit changes in its managed environment files, then rerun setup
with the same options. The installer refuses conflicting existing settings.
``--public-host`` changes the printed URL only; it does not configure DNS or TLS.

7. Update, diagnose or restart
------------------------------

To update an existing Docker installation, keep your original config path,
custom ports, database path and binding options. From the checkout::

    cd /opt/electrumx-RTM
    sudo git pull --ff-only origin master
    sudo docker build -f contrib/Dockerfile -t electrumx-rtm:mainnet .
    sudo python3 -S contrib/install_stack.py \
        --image electrumx-rtm:mainnet \
        --core-config "$CORE_CONFIG" \
        --with-explorer --public-host "$PUBLIC_HOST" \
        --profile low --wallet-api

Set ``CORE_CONFIG`` and ``PUBLIC_HOST`` again when using a new SSH session.
Repeat all custom options from your initial installation. Reinstalling preserves
RPC credentials, the selected database, unknown environment settings and copied
TLS files. Omitting the wallet flags preserves an existing relay choice;
``--no-wallet-api`` explicitly disables it. Core data is not modified.

Useful logs and checks::

    sudo journalctl -u rtm-explorer.service -n 100 --no-pager
    sudo journalctl -u rtm-electrumx-assets.service -n 100 --no-pager
    sudo systemctl status docker --no-pager
    sudo docker stats --no-stream
    df -h
    free -h

If the explorer cannot start, inspect its journal and check for a conflicting
port or incorrect Docker image. If Core is unavailable, check that Core is
running and its loopback RPC/config path and credentials match the managed
copies. Do not paste private environment/config contents into public issues.
If the asset backend waits, inspect synchronization first. HTTP 503 during sync
is expected; HTTP 429 means the request limit was reached. Supply/rich-list/
market-cap aliases return 501 because this server does not build those extra
statistics indexes.

To restart only the explorer::

    sudo systemctl restart rtm-explorer.service

To retry the backend start condition immediately::

    sudo systemctl start rtm-electrumx-assets.service

Neither command restarts Core. The services and timer are enabled to return
after a VPS reboot, subject to the same Core synchronization gate.

Native alternative (without Docker)
-----------------------------------

Use an installed Python 3.10 with matching ``venv`` and development packages,
plus a compiler and LevelDB headers. For Ubuntu 22.04's Python 3.10::

    sudo apt-get install -y build-essential libleveldb-dev python3.10-venv python3.10-dev
    cd /opt/electrumx-RTM
    sudo env PYTHON=/usr/bin/python3.10 VENV_DIR=/opt/rtm-electrumx/venv \
        sh contrib/install_electrumx.sh --setup --with-explorer \
        --core-config "$CORE_CONFIG" --public-host "$PUBLIC_HOST" \
        --profile low --wallet-api

Use this instead of the Docker route, not as a second indexer. The wrapper
installs this checkout into the isolated environment and configures the same
services, private listeners and startup gate. ``--explorer`` is a shortcut for
``--setup --with-explorer``. Running the wrapper without arguments installs the
Python package only; it does not configure a complete VPS service stack.

Bundled mainnet explorer
========================

The optional browser explorer provides an Etherscan-inspired overview, search,
blocks, transaction inputs and outputs, RTM address balances and history, asset
balances and outputs, an asset directory, metadata and holder tabs, a transaction
list, smartnodes and network metrics. It includes
a responsive layout, dark mode, copy buttons, public read APIs and opt-in non-custodial relay. No wallet or
private keys are required. Metadata is displayed as text; external content is
not fetched automatically.

The explorer uses local Core RPC for blocks, transactions and the asset directory,
and local ElectrumX for address and asset queries. Both must index the same
mainnet tip. While Core or the asset index is synchronizing, the site displays
progress and data APIs return HTTP 503. It does not display partial balances as
complete results. The installed asset-capable ElectrumX service must use a fresh
asset database as described below; an old coin-only service cannot serve these pages.

For installation, follow the VPS guide above. The complete stack installer
creates both the asset-capable ElectrumX backend and this companion explorer.
``contrib/install_explorer.py`` is an advanced explorer-only installer for an
already configured backend; its default Electrum port is 50001, while the
complete stack uses 50002.

For manual/container operation, ``electrumx_explorer`` or
``python -m electrumx.explorer`` reads ``EXPLORER_CORE_CONFIG`` (required),
``EXPLORER_HOST`` (default 0.0.0.0), ``EXPLORER_PORT`` (8080),
``EXPLORER_ELECTRUM_HOST`` (127.0.0.1) and ``EXPLORER_ELECTRUM_PORT`` (50001).
Core config must contain explicit ``rpcuser`` and ``rpcpassword``, mainnet
settings and a loopback ``rpcconnect``; config includes are rejected. The explorer
uses explicit allowlisted methods and never exposes a general RPC proxy. HTTP
transaction relay is disabled unless explicitly enabled as described below.

The explorer logo is the official Raptoreum mark used on https://raptoreum.com/,
bundled locally without a third-party image request.

The UI uses bundled HTML, CSS and JavaScript without a frontend framework,
external fonts or a separate statistics database. Hidden tabs pause polling;
visible pages refresh status every 30 seconds with failure backoff. Dashboard,
network and smartnode snapshots share a process-local cache capped at 2 MiB
and 32 entries, invalidated by the indexed tip and a 30-second lifetime.
Snapshot timestamps show when these public views were collected. Wallet
balances, fees, policies and broadcasts are not cached. Holders load only when
the holder tab is selected. Lost RPC connections show explicitly stale
last-known progress; unknown progress is displayed as unavailable.

Explorer HTTP API
-----------------

The explorer data routes are GET under ``/api/v1/``. The browser's API page documents them:

* ``health`` -- explorer process health without a Core call; not chain readiness.
* ``overview?limit=5`` -- bounded shared public dashboard snapshot (maximum 10).
* ``network`` and ``smartnodes?limit=25&offset=0`` -- public network snapshots.
* ``status`` -- readiness and synchronization; available during initial sync.
* ``search?q=QUERY`` -- address, transaction, block or asset name/ID lookup.
* ``blocks?start=HEIGHT&limit=10`` and ``blocks/HEIGHT_OR_HASH``.
* ``transactions?block=HEIGHT`` and ``transactions/TXID``.
* ``addresses/ADDRESS`` -- confirmed and pending RTM atomic balances.
* ``addresses/ADDRESS/history`` and ``addresses/ADDRESS/utxos``.
* ``addresses/ADDRESS/assets`` -- paginated asset balances.
* ``addresses/ADDRESS/assets/ASSET_ID/history`` and
  ``addresses/ADDRESS/assets/ASSET_ID/utxos``.
* ``assets`` -- asset directory; ``assets/ASSET_ID`` -- Core metadata.
* ``assets/ASSET_ID/holders`` -- confirmed holders (requires Core ``assetindex=1``).

Coin and asset atomic amounts use decimal strings with a scale of 100,000,000.
Core metadata fields retain their source units; decimal JSON numbers become
strings. Fees resolve at most 32 transaction inputs and are marked unavailable
when resolution fails. There is no global asset-transfer feed; transfers can be
inspected per address and in transaction outputs.

Lists accept ``limit`` (1--100; blocks and transaction summaries: 1--25) and
``offset`` (0--100,000). Responses use ``items`` with ``next_offset``;
descending block lists use ``next_start``. Asset address pages instead use an
opaque ``cursor`` tied to the persistent backend connection and chain/mempool
snapshot. Restart pagination after a stale cursor, index change or reconnect.
Coin history is subject to ElectrumX's upstream history/response limits;
oversized responses return an error. Offset pages can change as the chain grows.

The API defaults to 120 requests/minute per connecting IP and eight concurrent
requests. It ignores forwarded-IP headers; requests through a reverse proxy share
that limit. HTTP 400 means invalid input, 404 missing data, 429 rate limit,
502 unavailable backend and 503 sync/busy/unsupported service. Retry-After is
included for 429/503. The separate opt-in wallet routes below handle submissions.

Reference explorer compatibility
--------------------------------

Aliases follow the route vocabulary and observed response envelopes of the
`Raptoreum explorer API <https://explorer.raptoreum.com/api>`_:

* ``/api/getblockcount`` -- plain count, or ``?json=true`` for a JSON envelope.
* ``/api/getblockchaininfo`` -- full Core status under the legacy ``blockcount`` key.
* ``/api/getblock?height=HEIGHT`` or ``?hash=HASH`` -- verbose block under ``getblock``.
* ``/api/getblockstats?height=HEIGHT`` or ``?hash=HASH``.
* ``/api/getaddressbalance/ADDRESS`` -- fixed-eight-decimal RTM text;
  ``?json=true`` includes exact ``balanceRTM`` and ``balanceSatoshis`` strings.
  Query-style ``?address=ADDRESS`` and the older ``ADDRESS&json=true`` form work too.
* ``/api/asset/ASSET_ID`` -- ``success``/``asset`` envelope with asset name,
  display fields and Core metadata. Qualified subasset names use ``ROOT|CHILD``.
* ``/api/gettransaction/TXID`` and ``/api/getrawtransaction?txid=TXID`` --
  verbose transaction under ``transaction``, including its raw ``hex``.
* ``/api/getmininginfo``, ``/api/getmempoolinfo`` and ``/api/getrawmempool``.
* ``/api/getsmartnodecount`` and ``/api/getsmartnodes`` -- chain data from Core.
* ``/api/project`` -- basic project identity; ``/api`` opens the API reference.

Compatibility is intentionally partial. All monetary values retain exact string
encoding; legacy numeric values are not copied where they would lose precision.
Some reference documentation differs from its actual responses; the legacy
``getmempoolinfo`` and ``getsamrtnodecount`` envelope keys are preserved.
Smartnode lists contain chain fields without invented geolocation or ISP data.
Complete legacy responses are limited to 4 MiB and 5,000 records for mempool and
smartnode lists; oversized results fail explicitly rather than being truncated.
The status/count routes remain usable during sync; balance and transaction data
retain the full synchronization gate.

``/api/supply``, ``/api/marketcap`` and ``/api/getrichlist`` return HTTP 501
with ``not_indexed``. The reference serves those from a separate statistics and
market-data database; this server does not substitute UTXO totals for circulating
supply or fabricate a global rich list.

Non-custodial HTTP wallet API
-----------------------------

Read-only wallet data is available through:

* ``GET /api/v1/wallet/capabilities`` -- network, readiness, supported asset types,
  signing location, relay activation and raw transaction size limit.
* ``GET /api/v1/wallet/fees?blocks=6`` -- exact fee/relay rates in RTM per kB.
  An unavailable estimate is ``null`` with ``estimate_available: false``.
* ``GET /api/v1/wallet/asset-policy`` -- current special transaction fee from
  Core's active spork, root-owner authorization and subasset naming rules.
* ``GET /api/v1/wallet/assets/ASSET_ID`` -- exact indexed asset metadata and
  distinct parent-root context for subassets, with Core display metadata.
* ``GET /api/v1/assets/by-name/ROOT%7CCHILD`` -- qualified asset-name lookup.
* ``GET /api/v1/transactions/TXID/raw`` and
  ``GET /api/v1/transactions/TXID/proof?height=HEIGHT`` -- raw bytes and merkle proof
  for verification inside the wallet.

Explicitly enable the POST routes with ``--wallet-api`` during explorer setup,
or set ``EXPLORER_WALLET_API=1`` in its external environment file::

    sudo python3 contrib/install_explorer.py --image electrumx-rtm \
        --core-config /path/to/raptoreum.conf --public-host YOUR_SERVER_IP --wallet-api

``--no-wallet-api`` disables them. Reinstalling without either flag preserves
the existing choice. The native ``install_electrumx.sh --explorer`` command also
accepts these options. Use HTTPS/WSS with a browser-trusted certificate for
production wallet connections; opening the HTTP explorer does not configure TLS.

The opt-in routes accept only an ``application/json`` object containing ``hex``::

    POST /api/v1/wallet/decode
    {"hex": "CLIENT_TRANSACTION_HEX"}

    POST /api/v1/wallet/broadcast
    {"hex": "CLIENT_SIGNED_TRANSACTION_HEX"}

Decoding is explicitly ``syntax_only``: Core 2.0.3.01 has no
``testmempoolaccept`` RPC, so decoding does not verify signatures, unspent inputs
or eventual acceptance. Broadcast calls Core's ``sendrawtransaction`` with its
default fee ceiling and policy checks, without high-fee or policy bypass options.
Only the client builds transactions and signs input and asset-owner
authorizations. The same relay supports RTM transfers and Core-supported root,
subasset, mint and metadata-update transactions. No Core wallet/key/unlock RPC
is exposed, and key/seed fields and query overrides are rejected.

Raw transactions are bounded to 100,000 bytes. An accepted relay returns its
``txid`` and ``accepted: true``; it does not promise confirmation. If submission
loses its connection or times out, the API returns ``submission_unknown``,
``accepted: null`` and the locally calculated transaction ID. Check that ID
before submitting again. Explicit consensus/policy rejections use HTTP 422;
missing inputs or already-known transactions use 409. The server never
automatically retries a broadcast. Browser CORS/preflight requests are supported
without cookies or credentialed cross-origin access.

Asset RPC extension
===================

``server.features`` advertises the ``rtm_assets`` extension, version 1, its
supported methods, indexed height and tip, and readiness state.

The extension adds:

* ``blockchain.asset.get_metadata(asset_id)``
* ``blockchain.scripthash.assets.get_balance(scripthash)``
* ``blockchain.scripthash.assets.list(scripthash, cursor=None, limit=100)``
* ``blockchain.scripthash.assets.listunspent(scripthash, asset_id, cursor=None, limit=100)``
* ``blockchain.scripthash.assets.get_history(scripthash, asset_id, cursor=None, limit=100)``
* ``blockchain.scripthash.assets.subscribe(scripthash)``
* ``blockchain.scripthash.assets.unsubscribe(scripthash)``

These are project-specific extensions. Asset amounts, supply values, and unique
IDs use decimal strings to preserve integer precision. Asset amounts use
100,000,000 atomic units per token; metadata supplies the display precision.
Asset IDs are lowercase creation transaction hashes.

Paged responses include ``items`` and a ``cursor``. Limits range from 1 to 1,000.
Reuse cursors on the same connection; restart pagination if the indexed tip or
mempool generation changes. Error ``-32001`` indicates that the client should
retry after synchronization or restart stale pagination.

Manual installation reference
=============================

Install this checkout
---------------------

On Linux, install Python with ``venv`` support and matching development headers,
a C/C++ compiler, and the LevelDB headers (``python3-dev`` and ``libleveldb-dev``
on Debian/Ubuntu; match the development package to your chosen Python). From the repository
root, install into an isolated environment::

    sh contrib/install_electrumx.sh

Set ``PYTHON`` or ``VENV_DIR`` to choose the Python executable or installation
directory. The installer checks that all RTM asset components are present and
prints the server executable path. It does not install or configure Core.
When using ``contrib/systemd/electrumx.service``, change ``ExecStart`` to that
printed executable path and ensure the ``electrumx`` service account can access
the virtual environment and write to its configured database directory.

Alternatively, build the Docker image from this checkout::

    docker build -f contrib/Dockerfile -t electrumx-rtm .

The image defaults to RTM mainnet and runs as an unprivileged user. Supply
``DAEMON_URL`` through an external environment file. On Linux, use
``--network host`` when Core RPC is bound to the host's localhost address.
Bind-mounted database directories must be writable by UID 10001.

Core and server configuration
-----------------------------

The implementation targets Raptoreum Core ``2.0.3.01-mainnet`` and has been tested
with Python 3.10 and LevelDB on Linux. Use a mainnet Core full node with
``server=1``, ``txindex=1``, ``assetindex=1``, and ``prune=0``. Keep Core RPC
restricted to localhost or a private network.

Example ElectrumX environment::

    export COIN=Raptoreum
    export NET=mainnet
    export DB_DIRECTORY=/data/electrumx-rtm-assets-v1
    export DAEMON_URL=http://rpc_user:rpc_password@127.0.0.1:10225/
    export SERVICES=tcp://127.0.0.1:50001,rpc://127.0.0.1:8000

Replace the example credentials with your Core RPC credentials and create a
writable database directory for the service account.

**A fresh ElectrumX database and rebuild from genesis are required.** Existing
coin-only databases are rejected to avoid serving incomplete asset state. Keep
the previous database separate if you need to roll back. Your Core blockchain
data does not need to be deleted or downloaded again for this rebuild.

Asset readiness remains disabled while Core is in initial block download or the
index and mempool are out of sync. Wait for Core synchronization and the
ElectrumX rebuild before relying on wallet balances.

Scope and validation
====================

Browser-extension wallet integration
------------------------------------

The wallet client may be a browser extension. The wallet owns its keys, selects
coin and asset inputs, constructs transactions and signs locally. It sends only
the signed raw transaction to ``blockchain.transaction.broadcast``; that same
method relays both RTM coin and asset transactions to Core for validation. The
explorer's HTTP data routes are separate from the wallet's Electrum connection;
the opt-in HTTP wallet relay offers the same client-signing boundary.

Extensions connect using WebSocket; a production public endpoint should use
``wss`` with a browser-trusted TLS certificate. ElectrumX supports ``ws`` and
``wss`` services through ``SERVICES``. For example, an internal
``ws://127.0.0.1:50003`` listener can sit behind a TLS reverse proxy. Direct WSS
requires ``SSL_CERTFILE`` and ``SSL_KEYFILE``. Configure the proxy for WebSocket
upgrades, persistent connections and subscription notifications. A reachable
explorer URL does not imply that a public wallet WSS endpoint is configured.

Browser-compatible transport tests exercise coin and asset request dispatch
over a real local WebSocket connection using offline fixtures; no transaction
is sent to mainnet. A reference client can test server integration before the
extension exists. These checks do not validate wallet input selection, signing,
asset change, key storage or actual mainnet acceptance. The runtime constrains
``websockets`` to versions compatible with aiorpcX 0.18's legacy transport.

Release scope
-------------

The server indexes root assets, subassets and unique assets, including creation,
minting, transfers and metadata updates. Subassets retain distinct asset IDs,
supplies, balances, histories and root IDs; they do not share their root's UTXOs.
The server relays locally signed lifecycle transactions to Core. Transaction
construction, coin/asset change handling, root-owner authorization and signing
belong in the compatible wallet client; this repository contains no extension
wallet or private-key store.

Supported asset destinations are P2PKH. Future-transaction asset outputs and
noncanonical or unsupported unique ranges remain visible but are marked
``spendable: false``.

Offline tests cover parsing, storage and rollback, mempool behavior, individual
wallet request dispatch, and clean package installation. Live mainnet coin and
asset functionality checks remain pending Core synchronization; wallet transfer
behavior has not yet been validated end to end.

Support development
===================

Donations help support development, maintenance, and testing.

* **RTM (Raptoreum mainnet):** ``RVLFQj4RiMCDDrWGZXRd2ui1NKGQ9ieooz``
* **ETH (Ethereum):** ``0xf6d9b356D15dBB089B2033173A3993c14a8955CD``

Documentation
=============

See `readthedocs <https://electrumx-spesmilo.readthedocs.io/>`_.

This project builds on `spesmilo/electrumx <https://github.com/spesmilo/electrumx>`_
and the original `kyuupichan/electrumx <https://github.com/kyuupichan/electrumx>`_.
Upstream documentation describes the base server; the RTM asset methods above
are specific to this fork.

