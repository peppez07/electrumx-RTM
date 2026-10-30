'''Private backend transports. Backend error text is never sent to visitors.'''

import asyncio
import base64
import json
from decimal import Decimal

import aiohttp


class BackendError(Exception):
    def __init__(self, code='unavailable'):
        self.code = code
        super().__init__(code)


CORE_METHODS = frozenset({
    'getblockchaininfo', 'getblockhash', 'getblock', 'getrawtransaction',
    'getmempoolinfo', 'getassetdetailsbyid', 'getassetdetailsbyname',
    'listassets', 'listaddressesbyasset',
    'getblockstats', 'getmininginfo', 'getrawmempool', 'smartnode', 'smartnodelist',
    'decoderawtransaction', 'sendrawtransaction', 'spork',
})
ELECTRUM_METHODS = frozenset({
    'server.features', 'blockchain.scripthash.get_balance',
    'blockchain.scripthash.get_history', 'blockchain.scripthash.listunspent',
    'blockchain.scripthash.assets.list',
    'blockchain.scripthash.assets.listunspent',
    'blockchain.scripthash.assets.get_history',
    'blockchain.asset.get_metadata', 'blockchain.transaction.get_merkle',
    'blockchain.estimatefee', 'blockchain.relayfee',
})


def rpc_result(body):
    if not isinstance(body, dict):
        raise BackendError()
    error = body.get('error')
    if error:
        code = error.get('code') if isinstance(error, dict) else None
        raise BackendError({-5: 'not_found', -8: 'not_found',
                            -22:'invalid_transaction', -25:'missing_inputs',
                            -26:'transaction_rejected', -27:'already_known',
                            -32601: 'unsupported', -32001: 'synchronizing'}.get(code, 'unavailable'))
    if 'result' not in body:
        raise BackendError()
    return body['result']


class CoreClient:
    def __init__(self, url, username, password):
        self.url = url
        self.headers = {'Authorization': 'Basic '+base64.b64encode(
            (username+':'+password).encode('utf-8')).decode('ascii')}
        self.session = None

    async def call(self, method, params=None):
        if method not in CORE_METHODS:
            raise ValueError('Core method is not exposed by the explorer')
        if self.session is None:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20),
                connector=aiohttp.TCPConnector(limit=8))
        try:
            async with self.session.post(self.url, headers=self.headers, allow_redirects=False,
                    json={'jsonrpc': '1.0', 'id': 'explorer', 'method': method,
                          'params': params or []}) as response:
                if response.content_length and response.content_length > 16*1024*1024:
                    raise BackendError('too_large')
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > 16*1024*1024:
                        raise BackendError('too_large')
                return rpc_result(json.loads(raw, parse_float=Decimal))
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, UnicodeError) as e:
            raise BackendError() from None

    async def close(self):
        if self.session:
            await self.session.close()


class ElectrumClient:
    '''One persistent connection preserves the server's connection-bound cursors.'''
    def __init__(self, host='127.0.0.1', port=50001, admin_port=None):
        self.host, self.port, self.admin_port = host, port, admin_port
        self.reader = self.writer = None
        self.lock = asyncio.Lock()
        self.serial = 0

    async def index_progress(self):
        '''Initial-sync progress from the local admin RPC, which ElectrumX serves before
        its client ports open. Only `getinfo` is ever sent; returns None if unavailable.'''
        if not self.admin_port:
            return None
        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.admin_port, limit=1024*1024), 5)
            writer.write(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'getinfo',
                                     'params': []}).encode()+b'\n')
            await writer.drain()
            info = rpc_result(json.loads(await asyncio.wait_for(reader.readline(), 5)))
            db_height, daemon_height = info['db height'], info['daemon height']
            if type(db_height) is not int or type(daemon_height) is not int:
                return None
            return {'indexed_height': db_height, 'daemon_height': daemon_height}
        except (OSError, asyncio.TimeoutError, ValueError, KeyError, TypeError, BackendError):
            return None
        finally:
            if writer:
                writer.close()

    async def call(self, method, params=None):
        if method not in ELECTRUM_METHODS:
            raise ValueError('Electrum method is not exposed by the explorer')
        async with self.lock:
            try:
                return await asyncio.wait_for(self._request(method, params or []), 20)
            except (OSError, asyncio.TimeoutError, ValueError, UnicodeError, asyncio.LimitOverrunError):
                await self.close()
                raise BackendError() from None

    async def _request(self, method, params):
        if self.writer is None or self.writer.is_closing():
            self.reader, self.writer = await asyncio.open_connection(
                self.host, self.port, limit=8*1024*1024)
        self.serial += 1
        serial = self.serial
        self.writer.write(json.dumps({'jsonrpc': '2.0', 'id': serial,
                                      'method': method, 'params': params}).encode()+b'\n')
        await self.writer.drain()
        while True:
            line = await self.reader.readline()
            if not line:
                raise OSError('Electrum connection closed')
            body = json.loads(line, parse_float=Decimal)
            if isinstance(body, dict) and body.get('id') == serial:
                return rpc_result(body)

    async def close(self):
        if self.writer:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except OSError:
                pass
        self.writer = self.reader = None
