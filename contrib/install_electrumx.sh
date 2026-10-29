#!/bin/sh
# Install the current checkout into an isolated Python environment.
set -eu

if [ "${1:-}" = "--help" ]; then
    printf '%s\n' \
        'Usage: sh contrib/install_electrumx.sh' \
        'Optional environment: PYTHON=/path/to/python3 VENV_DIR=/path/to/venv' \
        'Linux prerequisites: Python with venv and matching development headers,' \
        'a C/C++ compiler and libleveldb-dev (Debian/Ubuntu: python3-dev).' \
        'Python 3.10 is the tested runtime. This script does not alter Core or OS settings.'
    exit 0
fi
if [ "$#" -ne 0 ]; then
    printf '%s\n' 'Unexpected arguments; use --help.' >&2
    exit 2
fi

SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PYTHON=${PYTHON:-python3}
VENV_DIR=${VENV_DIR:-"${HOME:?HOME must be set}/.local/share/electrumx-rtm/venv"}

command -v "$PYTHON" >/dev/null 2>&1 || {
    printf '%s\n' 'Python executable not found; set PYTHON to its path.' >&2
    exit 1
}
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 7) else "Python 3.7 or newer is required; Python 3.10 is recommended")'
"$PYTHON" -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install "$SOURCE_DIR"

# Isolated mode proves these components came from the installed package,
# not imports accidentally resolved from the source checkout.
"$VENV_DIR/bin/python" -I -c '
from electrumx.lib.coins import Raptoreum
expected = {
    "DESERIALIZER": "DeserializerRaptoreum",
    "DBCLS": "RaptoreumDB",
    "BLOCK_PROCESSOR": "RaptoreumBlockProcessor",
    "MEMPOOLCLS": "RaptoreumMemPool",
    "SESSIONCLS": "RaptoreumElectrumX",
}
for attribute, name in expected.items():
    if getattr(Raptoreum, attribute).__name__ != name:
        raise RuntimeError("Installed RTM component mismatch: " + attribute)
print("Installed RTM mainnet coin and asset components successfully.")
'
printf '\n%s\n' \
    "Server executable: $VENV_DIR/bin/electrumx_server" \
    'Set COIN=Raptoreum, NET=mainnet, DAEMON_URL and a fresh DB_DIRECTORY before starting.' \
    'Keep Core RPC private. Wait for Core synchronization and the asset index rebuild.'
