"""Credential-free HTTP access to public Electrum RPC over a loopback socket.

Run with ``python -m electrumx.http_gateway`` behind a TLS reverse proxy.
X-Electrum-Session is an opaque browser-generated UUID, not authentication.
Sessions retain TCP connections because RTM pagination cursors belong to them.
"""

import argparse
import asyncio
from contextlib import suppress
import ipaddress
import json
import math
import os
import re
import time

import aiohttp
from aiohttp import web


PUBLIC_METHODS = frozenset({
    'server.version', 'server.features', 'server.ping',
    'blockchain.headers.subscribe', 'blockchain.block.header',
    'blockchain.scripthash.get_balance', 'blockchain.scripthash.get_history',
    'blockchain.scripthash.listunspent', 'blockchain.scripthash.subscribe',
    'blockchain.transaction.get', 'blockchain.transaction.get_in_block',
    'blockchain.transaction.broadcast', 'blockchain.relayfee',
    'blockchain.estimatefee', 'blockchain.scripthash.assets.get_balance',
    'blockchain.scripthash.assets.listunspent',
    'blockchain.scripthash.assets.get_history',
    'blockchain.scripthash.assets.subscribe', 'blockchain.asset.get_metadata',
})
SESSION_RE = re.compile(r'(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\Z')
SYNC_MESSAGE = 'Electrum service is unavailable or synchronizing. Please wait for synchronization.'
UNKNOWN_BROADCAST = ('Transaction submission outcome is unknown. Check the transaction ID '
                     'and wallet history before taking further action.')
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def reject_constant(value):
    raise ValueError('Nonfinite JSON number')


def finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('Nonfinite JSON number')
    return result


def load_json(value):
    return json.loads(value, parse_constant=reject_constant, parse_float=finite_float)


def rpc_error(request_id, code, message, status=200):
    return web.json_response({'jsonrpc': '2.0', 'id': request_id,
                              'error': {'code': code, 'message': message}}, status=status)


def safe_rpc_message(code):
    # Upstream exceptions can embed internal URLs, paths and daemon credentials.
    return {
        -32001: SYNC_MESSAGE,
        -32601: 'Public Electrum method is unavailable on this server.',
        -32602: 'Invalid Electrum parameters or expired pagination cursor.',
        -32600: 'Invalid Electrum request.',
    }.get(code, 'The Electrum server rejected this request.')


def loopback_host(host, service):
    if host == 'localhost':
        return '127.0.0.1'
    try:
        if ipaddress.ip_address(host).is_loopback:
            return host
    except ValueError:
        pass
    raise ValueError('{} must be a loopback IP address'.format(service))


class ElectrumSession:
    def __init__(self, host, port):
        self.host, self.port = host, port
        self.lock = asyncio.Lock()
        self.writer = None
        self.reader_task = None
        self.pending = None
        self.next_id = 0
        self.active = 0
        self.touched = time.monotonic()

    def close(self):
        if self.writer:
            self.writer.close()
            self.writer = None
        if self.reader_task:
            self.reader_task.cancel()
            self.reader_task = None

    async def read_responses(self, reader):
        try:
            while True:
                line = await reader.readline()
                if not line:
                    raise ConnectionError('Electrum connection closed')
                if len(line) > MAX_RESPONSE_BYTES:
                    raise ValueError('Electrum response too large')
                message = load_json(line)
                if not isinstance(message, dict):
                    raise ValueError('Invalid Electrum response')
                # Notifications and unmatched responses are never exposed to HTTP.
                if ('method' in message or type(message.get('id')) is not int or
                        message['id'] != self.next_id):
                    continue
                if self.pending and not self.pending.done():
                    if message.get('error') is None and 'result' not in message:
                        raise ValueError('Invalid Electrum response')
                    self.pending.set_result(message)
        except (OSError, ValueError, RecursionError, asyncio.CancelledError):
            if self.writer:
                self.writer.close()
                self.writer = None
            if self.pending and not self.pending.done():
                self.pending.set_exception(ConnectionError('Electrum transport failed'))

    async def call(self, method, params):
        async with self.lock:
            try:
                if self.writer is None or self.writer.is_closing():
                    reader, self.writer = await asyncio.open_connection(
                        self.host, self.port, limit=MAX_RESPONSE_BYTES + 1)
                    self.reader_task = asyncio.create_task(self.read_responses(reader))
                self.next_id += 1
                self.pending = asyncio.get_running_loop().create_future()
                message = {'jsonrpc': '2.0', 'id': self.next_id,
                           'method': method, 'params': params}
                # Deliberately one write, one response wait, and no retry path.
                self.writer.write(json.dumps(message, allow_nan=False).encode() + b'\n')
                await self.writer.drain()
                return await self.pending
            except (OSError, ValueError, asyncio.CancelledError):
                self.close()
                raise
            finally:
                self.pending = None


def create_app(*, electrum_host='127.0.0.1', electrum_port=50002,
               explorer_port=8080, max_sessions=32, idle_timeout=300,
               request_timeout=15, max_concurrency=32,
               max_request_bytes=1024 * 1024):
    electrum_host = loopback_host(electrum_host, 'Electrum upstream')
    if (type(electrum_port) is not int or not 1 <= electrum_port <= 65535 or
            type(explorer_port) is not int or not 0 <= explorer_port <= 65535):
        raise ValueError('Invalid loopback service port')
    if (type(max_sessions) is not int or not 1 <= max_sessions <= 64 or
            type(max_concurrency) is not int or not 1 <= max_concurrency <= 64 or
            type(max_request_bytes) is not int or not 1 <= max_request_bytes <= 4*1024*1024 or
            not math.isfinite(idle_timeout) or not 0 < idle_timeout <= 3600 or
            not math.isfinite(request_timeout) or not 0 < request_timeout <= 60):
        raise ValueError('Invalid gateway resource limits')

    sessions = {}
    in_flight = 0
    status_client = None

    @web.middleware
    async def cors_and_limits(request, handler):
        nonlocal in_flight
        counted = False
        if request.method == 'OPTIONS':
            response = web.Response(status=204)
        else:
            try:
                if request.path in ('/api/v1/electrum', '/api/v1/status', '/api/v1/servers'):
                    if in_flight >= max_concurrency:
                        response = rpc_error(None, -32001, 'Gateway is busy. Please wait.', 503)
                    else:
                        in_flight += 1
                        counted = True
                        response = await handler(request)
                else:
                    response = await handler(request)
            except web.HTTPRequestEntityTooLarge:
                response = rpc_error(None, -32600, 'Request body exceeds the gateway limit.', 413)
            except web.HTTPException as error:
                response = rpc_error(None, -32600, 'Invalid HTTP request.', error.status)
            finally:
                if counted:
                    in_flight -= 1
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-Electrum-Session'
        response.headers['Cache-Control'] = 'no-store'
        return response

    app = web.Application(client_max_size=max_request_bytes, middlewares=[cors_and_limits])

    def evict_idle():
        now = time.monotonic()
        for token, session in list(sessions.items()):
            if not session.active and now - session.touched >= idle_timeout:
                session.close()
                del sessions[token]

    async def reap():
        while True:
            await asyncio.sleep(min(idle_timeout / 2, 30))
            evict_idle()

    async def lifecycle(app):
        nonlocal status_client
        status_client = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=min(request_timeout, 3)),
            cookie_jar=aiohttp.DummyCookieJar(), trust_env=False)
        reaper = asyncio.create_task(reap())
        yield
        reaper.cancel()
        with suppress(asyncio.CancelledError):
            await reaper
        readers = [s.reader_task for s in sessions.values() if s.reader_task]
        writers = [s.writer for s in sessions.values() if s.writer]
        for session in sessions.values():
            session.close()
        if readers:
            await asyncio.gather(*readers, return_exceptions=True)
        for writer in writers:
            with suppress(OSError):
                await writer.wait_closed()
        sessions.clear()
        await status_client.close()

    app.cleanup_ctx.append(lifecycle)

    async def electrum(request):
        if request.content_type != 'application/json':
            return rpc_error(None, -32600, 'Content-Type must be application/json.', 415)
        try:
            body = await asyncio.wait_for(request.read(), timeout=request_timeout)
            payload = load_json(body)
        except asyncio.TimeoutError:
            return rpc_error(None, -32600, 'Request body timed out.', 408)
        except (ValueError, UnicodeError, RecursionError):
            return rpc_error(None, -32700, 'Invalid JSON.')
        if (not isinstance(payload, dict) or payload.get('jsonrpc') != '2.0' or
                type(payload.get('id')) not in (int, str) or
                not isinstance(payload.get('method'), str) or
                not isinstance(payload.get('params', []), list)):
            return rpc_error(None, -32600, 'Expected one JSON-RPC 2.0 request with an ID and positional parameters.')
        request_id, method = payload['id'], payload['method']
        if method not in PUBLIC_METHODS:
            return rpc_error(request_id, -32601, 'Method is not available through the public gateway.')
        token = request.headers.get('X-Electrum-Session', '')
        if not SESSION_RE.fullmatch(token):
            return rpc_error(request_id, -32600, 'X-Electrum-Session must be a browser-generated UUID.', 400)
        token = token.replace('-', '').lower()
        evict_idle()
        session = sessions.get(token)
        if session is None:
            if len(sessions) >= max_sessions:
                return rpc_error(request_id, -32001, 'Gateway session capacity reached. Please wait.', 503)
            session = sessions[token] = ElectrumSession(electrum_host, electrum_port)
        session.active += 1
        try:
            response = await asyncio.wait_for(
                session.call(method, payload.get('params', [])), timeout=request_timeout)
            error = response.get('error')
            if error is not None:
                code = error.get('code') if isinstance(error, dict) else None
                if type(code) is not int:
                    return rpc_error(request_id, -32603, 'Invalid response from Electrum service.')
                return rpc_error(request_id, code, safe_rpc_message(code))
            return web.json_response({'jsonrpc': '2.0', 'id': request_id, 'result': response['result']})
        except (OSError, ValueError, asyncio.TimeoutError):
            if method == 'blockchain.transaction.broadcast':
                return rpc_error(request_id, -32000, UNKNOWN_BROADCAST)
            return rpc_error(request_id, -32001, SYNC_MESSAGE)
        finally:
            session.active -= 1
            session.touched = time.monotonic()

    async def health(request):
        return web.json_response({'ok': True})

    async def explorer_json(path):
        async with status_client.get(
                'http://127.0.0.1:{}{}'.format(explorer_port, path),
                allow_redirects=False) as response:
            if response.status != 200:
                raise ValueError('Explorer unavailable')
            body = bytearray()
            async for chunk in response.content.iter_chunked(8192):
                body.extend(chunk)
                if len(body) > 262144:
                    raise ValueError('Explorer response too large')
            return load_json(body)

    async def status(request):
        if not explorer_port:
            return web.json_response({'ready': False, 'phase': 'status_disabled'}, status=503)
        try:
            return web.json_response(public_status(await explorer_json('/api/v1/status')))
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError, RecursionError):
            return web.json_response({'ready': False, 'phase': 'status_unavailable'}, status=503)

    async def servers(request):
        if not explorer_port:
            return web.json_response({'servers': [], 'discovery': False})
        try:
            return web.json_response(public_servers(await explorer_json('/api/v1/servers')))
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError, RecursionError):
            return web.json_response({'servers': [], 'discovery': False}, status=503)

    app.router.add_post('/api/v1/electrum', electrum)
    app.router.add_get('/api/v1/health', health)
    app.router.add_get('/api/v1/status', status)
    app.router.add_get('/api/v1/servers', servers)
    return app


def public_status(data):
    """Project public readiness data; never relay arbitrary explorer fields."""
    phases = {'ready', 'core_unavailable', 'core_syncing', 'index_unavailable',
              'index_syncing', 'wrong_network'}
    if not isinstance(data, dict):
        raise ValueError('Invalid explorer status')
    result = {'ready': data.get('ready') is True,
              'phase': data.get('phase') if data.get('phase') in phases else 'status_unavailable'}
    for key in ('core_ready', 'index_ready', 'core_stale'):
        if type(data.get(key)) is bool:
            result[key] = data[key]
    for section, fields in (
            ('core', ('blocks', 'headers', 'verificationprogress')),
            ('electrumx', ('indexed_height', 'target_height', 'percent'))):
        source = data.get(section)
        if not isinstance(source, dict):
            continue
        result[section] = {key: source[key] for key in fields
                           if type(source.get(key)) in (int, float) and
                           0 <= source[key] < 10**30 and math.isfinite(source[key])}
        for key in ('ready', 'initial_sync', 'initialblockdownload'):
            if type(source.get(key)) is bool:
                result[section][key] = source[key]
    return result


PEER_URL_RE = re.compile(r'https://(?:\d{1,3}(?:\.\d{1,3}){3}|[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?)/api/v1/electrum\Z')


def public_servers(data):
    """Project discovered gateways; only fixed-shape HTTPS gateway URLs are relayed."""
    if not isinstance(data, dict) or not isinstance(data.get('servers'), list):
        raise ValueError('Invalid peer list')
    servers = []
    for item in data['servers'][:256]:
        if not isinstance(item, dict) or not isinstance(item.get('url'), str):
            continue
        if not PEER_URL_RE.fullmatch(item['url']):
            continue
        height, seen = item.get('height'), item.get('seen_at')
        if type(height) is not int or type(seen) is not int or not 0 < height < 10**9 or seen <= 0:
            continue
        servers.append({'url': item['url'], 'height': height, 'seen_at': seen,
                        'assets_ready': item.get('assets_ready') is True,
                        'smartnode': item.get('smartnode') is True})
    updated = data.get('updated_at')
    return {'discovery': data.get('discovery') is True, 'servers': servers,
            'updated_at': updated if type(updated) is int else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default=os.environ.get('WALLET_HTTP_HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=os.environ.get('WALLET_HTTP_PORT', '8090'))
    parser.add_argument('--electrum-host', default=os.environ.get('WALLET_ELECTRUM_HOST', '127.0.0.1'))
    parser.add_argument('--electrum-port', type=int, default=os.environ.get('WALLET_ELECTRUM_PORT', '50002'))
    parser.add_argument('--explorer-port', type=int, default=os.environ.get('WALLET_EXPLORER_PORT', '8080'))
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('HTTP port must be in 1..65535')
    try:
        http_host = loopback_host(args.host, 'HTTP listener')
        app = create_app(electrum_host=args.electrum_host, electrum_port=args.electrum_port,
                         explorer_port=args.explorer_port)
    except ValueError as error:
        parser.error(str(error))
    # Access logs would disclose browser session identifiers if customized to log headers.
    web.run_app(app, host=http_host, port=args.port, access_log=None)


if __name__ == '__main__':
    main()
