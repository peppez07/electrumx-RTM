'''Load local mainnet RPC settings without exposing them to the public API.'''

from pathlib import Path


def load_core_config(path):
    settings = {}
    section = ''
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith(('#', ';')):
            continue
        if line.startswith('[') and line.endswith(']'):
            section = line[1:-1]
            continue
        if section not in ('', 'main'):
            continue
        if '=' in line:
            key, value = line.split('=', 1)
            settings[key.strip()] = value.strip()
    if (settings.get('chain', 'main') != 'main' or
        settings.get('testnet', '0') not in ('0', 'false') or
        settings.get('regtest', '0') not in ('0', 'false') or 'includeconf' in settings):
        raise ValueError('Explorer requires an explicit mainnet configuration without includes.')
    host = settings.get('rpcconnect', '127.0.0.1')
    if host not in ('127.0.0.1', 'localhost', '::1'):
        raise ValueError('Explorer requires private local Core RPC.')
    try:
        port = int(settings.get('rpcport', '10225'))
        if not 1 <= port <= 65535:
            raise ValueError()
    except ValueError:
        raise ValueError('Invalid Core RPC port.') from None
    user, password = settings.get('rpcuser'), settings.get('rpcpassword')
    if not user or not password:
        raise ValueError('Core config must provide rpcuser and rpcpassword.')
    if host == '::1':
        host = '[::1]'
    return {'url': 'http://{}:{}/'.format(host, port), 'username': user, 'password': password}
