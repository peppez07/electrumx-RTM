import os
import sys

from aiohttp import web

from .clients import CoreClient, ElectrumClient
from .config import load_core_config
from .service import create_app


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
    except (KeyError, ValueError, OSError):
        print('Explorer configuration invalid. Set EXPLORER_CORE_CONFIG to a readable mainnet RPC config and valid local ports.', file=sys.stderr)
        return 2
    app = create_app(CoreClient(**config), ElectrumClient(electrum_host, electrum_port),
                     enable_wallet_api=os.environ.get('EXPLORER_WALLET_API', '0') == '1')
    web.run_app(app, host=os.environ.get('EXPLORER_HOST', '0.0.0.0'), port=port,
                access_log=None, print=None)
    return 0


if __name__ == '__main__':
    sys.exit(main())
