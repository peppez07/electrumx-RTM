import os
import sys

from aiohttp import web

from .clients import CoreClient, ElectrumClient
from .config import load_core_config
from .peers import PeerDirectory
from .service import create_app

# Bootstrap gateways that are not smartnodes; smartnode gateways are found via Core.
DEFAULT_PEER_SEEDS = '144.202.18.233'


def main():
    try:
        config = load_core_config(os.environ['EXPLORER_CORE_CONFIG'])
        port = int(os.environ.get('EXPLORER_PORT', '8080'))
        electrum_port = int(os.environ.get('EXPLORER_ELECTRUM_PORT', '50001'))
        if not (1 <= port <= 65535 and 1 <= electrum_port <= 65535):
            raise ValueError()
        electrum_host = os.environ.get('EXPLORER_ELECTRUM_HOST', '127.0.0.1')
        if electrum_host not in ('127.0.0.1', 'localhost', '::1'):
            raise ValueError()
        # ElectrumX admin RPC (install_stack default 8001); 0 disables sync-progress reporting.
        admin_port = int(os.environ.get('EXPLORER_ELECTRUM_RPC_PORT', '8001'))
        if not 0 <= admin_port <= 65535:
            raise ValueError()
    except (KeyError, ValueError, OSError):
        print('Explorer configuration invalid. Set EXPLORER_CORE_CONFIG to a readable mainnet RPC config and valid local ports.', file=sys.stderr)
        return 2
    core = CoreClient(**config)
    peers = None
    # Probes registered smartnodes for public wallet gateways (HTTPS 443 only).
    if os.environ.get('EXPLORER_PEER_DISCOVERY', '1') == '1':
        seeds = os.environ.get('EXPLORER_PEER_SEEDS', DEFAULT_PEER_SEEDS).split(',')
        peers = PeerDirectory(core, seeds=[s for s in seeds if s.strip()])
    app = create_app(core, ElectrumClient(electrum_host, electrum_port, admin_port or None),
                     enable_wallet_api=os.environ.get('EXPLORER_WALLET_API', '0') == '1',
                     metrics_file=os.environ.get('EXPLORER_METRICS_FILE'), peers=peers)
    web.run_app(app, host=os.environ.get('EXPLORER_HOST', '0.0.0.0'), port=port,
                access_log=None, print=None)
    return 0


if __name__ == '__main__':
    sys.exit(main())
