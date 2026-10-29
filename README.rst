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

The first release provides server support for viewing and transferring existing
assets. Wallet creation, minting, and metadata-editing workflows are outside this
release, although their on-chain events are indexed. Asset transfer construction,
change handling, and signing belong in the compatible wallet client.

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

