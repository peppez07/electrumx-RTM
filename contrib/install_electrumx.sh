#!/bin/sh
# Install this checkout; optionally configure a complete mainnet service stack.
set -eu
SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PYTHON=${PYTHON:-python3}
SETUP=0
if [ "${1:-}" = "--help" ]; then
    printf '%s\n' \
        'Usage: sh contrib/install_electrumx.sh [--setup [service options]]' \
        '       sh contrib/install_electrumx.sh --explorer [service options]' \
        'No arguments: install this checkout into an isolated Python environment.' \
        '--setup: configure one mainnet coin/asset indexer; --with-explorer adds the UI.' \
        '--explorer: shortcut for --setup --with-explorer.' \
        'Use --setup --help for options, or --setup --dry-run to inspect.' \
        'Optional environment: PYTHON=/path/to/python3 VENV_DIR=/path/to/venv' \
        'Linux prerequisites: Python with venv and matching development headers,' \
        'a C/C++ compiler and libleveldb-dev. Python 3.10 is the tested runtime.' \
        'Core must already be mainnet, unpruned, txindex=1 and assetindex=1.' \
        'The installer does not download, restart, reindex or alter Core.'
    exit 0
fi
case "${1:-}" in
    --setup) SETUP=1; shift ;;
    --explorer) SETUP=1; shift; set -- --with-explorer "$@" ;;
    '') ;;
    *) printf '%s\n' 'Unexpected arguments; use --help.' >&2; exit 2 ;;
esac
if [ "$SETUP" = 1 ]; then
    VENV_DIR=${VENV_DIR:-/opt/rtm-electrumx/venv}
else
    VENV_DIR=${VENV_DIR:-"${HOME:?HOME must be set}/.local/share/electrumx-rtm/venv"}
fi
command -v "$PYTHON" >/dev/null 2>&1 || {
    printf '%s\n' 'Python executable not found; set PYTHON to its path.' >&2
    exit 1
}
if [ "$SETUP" = 1 ]; then
    DRY_RUN=0
    for argument do
        if [ "$argument" = "--help" ]; then
            exec "$PYTHON" "$SOURCE_DIR/contrib/install_stack.py" --venv "$VENV_DIR" --help
        fi
        if [ "$argument" = "--dry-run" ]; then DRY_RUN=1; fi
    done
    if [ "$DRY_RUN" = 1 ]; then
        exec "$PYTHON" "$SOURCE_DIR/contrib/install_stack.py" --venv "$VENV_DIR" "$@"
    fi
    "$PYTHON" "$SOURCE_DIR/contrib/install_stack.py" --venv "$VENV_DIR" --dry-run "$@" >/dev/null
    if [ "$(id -u)" != 0 ]; then
        printf '%s\n' '--setup requires root for dedicated service accounts and systemd.' >&2
        exit 2
    fi
fi
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 7) else "Python 3.7 or newer is required; Python 3.10 is recommended")'
"$PYTHON" -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install "$SOURCE_DIR"
"$VENV_DIR/bin/python" -I -c '
from pathlib import Path
from electrumx.lib.coins import Raptoreum
import electrumx.explorer as explorer
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
if not (Path(explorer.__file__).parent / "static/index.html").is_file():
    raise RuntimeError("Installed explorer assets are missing")
print("Installed RTM mainnet coin, asset and explorer components successfully.")
'
if [ "$SETUP" = 1 ]; then
    exec "$VENV_DIR/bin/python" "$SOURCE_DIR/contrib/install_stack.py" --venv "$VENV_DIR" "$@"
fi
printf '\n%s\n' \
    "Server executable: $VENV_DIR/bin/electrumx_server" \
    'Configure COIN=Raptoreum, NET=mainnet, DAEMON_URL and a fresh DB_DIRECTORY manually,' \
    'or rerun with --setup to create the managed service, private settings and sync gate.'
