#!/bin/sh
# Compatibility entry point: install this checkout without changing the OS.
set -eu
REPOSITORY_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
exec sh "$REPOSITORY_ROOT/contrib/install_electrumx.sh" "$@"
