================================
ElectrumX for Raptoreum RTM
================================

  :Licence: MIT
  :Language: Python (>= 3.7)
  :Original Author: Neil Booth

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

On Linux with systemd, an existing mainnet Core and the prerequisites below,
install this checkout and automatically configure the explorer::

    sudo sh contrib/install_electrumx.sh --explorer \
        --core-config /var/lib/rtm-mainnet/core/raptoreum.conf \
        --public-host YOUR_SERVER_IP

This shortcut installs into ``/opt/rtm-electrumx/venv`` and configures both
``rtm-electrumx-assets.service`` and ``rtm-explorer.service``. Core must already
run mainnet with ``txindex=1``, ``assetindex=1`` and ``prune=0``. Setup never
restarts, downloads, upgrades or reindexes Core. A timer waits for Core to finish
synchronizing before starting the asset-capable indexer. Stop an obsolete
coin-only indexer explicitly to avoid two indexers competing for memory; its
existing database should be preserved.

The complete setup is also available for an existing Docker image::

    sudo python3 contrib/install_stack.py --image electrumx-rtm \
        --core-config /path/to/raptoreum.conf --with-explorer \
        --public-host YOUR_SERVER_IP

``sh contrib/install_electrumx.sh --setup --with-explorer`` provides the same
native service setup. No arguments still performs package installation only.
Use ``--dry-run`` to inspect setup without reading credentials or changing files.
The default ``--profile auto`` selects the low profile below 4 GiB RAM: 16 MB
index cache, 256 MiB backend memory limit, 256 MiB additional swap, half a CPU
and 50 sessions. ``--profile low`` or ``standard`` chooses explicitly. These are
service limits; Core and the operating system need additional memory and disk.

Settings live in ``/etc/rtm-electrumx`` and ``/etc/rtm-explorer`` with restricted
permissions. The backend uses a fresh ``/var/lib/rtm-electrumx/assets-v1`` database;
``--db-dir`` selects another path. Reinstalling preserves RPC credentials,
database selection, ports, unknown environment options and configured TLS.
Repeat any custom port/path options. Conflicting configurations or unrelated
service units are rejected rather than overwritten.

Private listeners default to TCP 50002, WebSocket 50003 and admin RPC 8001.
Supply ``--tls-cert CERT --tls-key KEY`` to add public WSS on 50004; the
installer validates and copies the matching certificate/key. ``--no-tls``
explicitly disables a previous WSS listener. Certificates are not issued
automatically. Use a trusted HTTPS reverse proxy for the browser API. The
explorer defaults to HTTP 8080; active UFW receives a rule for that port
and optional WSS. Other host/provider firewalls must permit these ports.
Use ``--port``, ``--tcp-port``, ``--ws-port``, ``--rpc-port`` and ``--wss-port``
to choose ports. Core and admin RPC remain private.

For an existing installed environment, or the Docker image built below::

    sudo python3 contrib/install_explorer.py --venv /opt/rtm-explorer/venv \
        --core-config /path/to/raptoreum.conf --public-host YOUR_SERVER_IP

    sudo python3 contrib/install_explorer.py --image electrumx-rtm \
        --core-config /path/to/raptoreum.conf --public-host YOUR_SERVER_IP

Add ``--dry-run`` to inspect the configuration without changing the host.
The Docker service uses host networking, an unprivileged container user, a
read-only filesystem and resource limits. Run it separately from ElectrumX.
For HTTPS, supply a domain to your reverse proxy, proxy to localhost:8080 and
configure the explorer with ``--bind 127.0.0.1``. Keep Core and ElectrumX admin RPC
bound to localhost. The installer does not obtain domain certificates or alter
provider firewall rules.

For manual/container operation, ``electrumx_explorer`` or
``python -m electrumx.explorer`` reads ``EXPLORER_CORE_CONFIG`` (required),
``EXPLORER_HOST`` (default 0.0.0.0), ``EXPLORER_PORT`` (8080),
``EXPLORER_ELECTRUM_HOST`` (127.0.0.1) and ``EXPLORER_ELECTRUM_PORT`` (50001).
Core config must contain explicit ``rpcuser`` and ``rpcpassword``, mainnet
settings and a loopback ``rpcconnect``; config includes are rejected. The explorer
uses explicit allowlisted methods and never exposes a general RPC proxy. HTTP
transaction relay is disabled unless explicitly enabled as described below.

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

Mainnet setup
=============

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

