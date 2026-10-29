import asyncio
import json
from decimal import Decimal

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from electrumx.explorer.clients import BackendError, CoreClient, ElectrumClient


@pytest.mark.asyncio
async def test_core_preserves_decimal_and_hides_error_credentials():
    async def rpc(request):
        body = await request.json()
        if body['method'] == 'getblock':
            return web.Response(text='{"result":{"value":90071992.54740993},"error":null}')
        return web.json_response({'error': {'code': -1, 'message': 'secret-password localhost'}})
    app = web.Application()
    app.router.add_post('/', rpc)
    async with TestServer(app) as server:
        client = CoreClient(str(server.make_url('/')), 'user', 'secret-password')
        try:
            assert (await client.call('getblock'))['value'] == Decimal('90071992.54740993')
            with pytest.raises(BackendError) as exc:
                await client.call('getblockchaininfo')
            assert str(exc.value) == 'unavailable'
            with pytest.raises(ValueError):
                await client.call('sendtoaddress')
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_core_handles_streamed_json_without_partial_read_failure():
    async def rpc(request):
        response = web.StreamResponse(headers={'Content-Type': 'application/json'})
        await response.prepare(request)
        await response.write(b'{"result":')
        await asyncio.sleep(0.01)
        await response.write(b'{"blocks":100},"error":null}')
        await response.write_eof()
        return response
    app = web.Application()
    app.router.add_post('/', rpc)
    async with TestServer(app) as server:
        client = CoreClient(str(server.make_url('/')), 'user', 'password')
        try:
            assert await client.call('getblockchaininfo') == {'blocks': 100}
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_asset_pages_reuse_connection_for_cursors_and_recover_after_disconnect():
    connections = []
    async def rpc(reader, writer):
        connections.append(writer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                body = json.loads(line)
                result = {'cursor': 'connection-bound'}
                writer.write(json.dumps({'id': body['id'], 'result': result}).encode()+b'\n')
                await writer.drain()
        finally:
            writer.close()
    server = await asyncio.start_server(rpc, '127.0.0.1', 0)
    client = ElectrumClient('127.0.0.1', server.sockets[0].getsockname()[1])
    try:
        await client.call('blockchain.scripthash.assets.list', ['00'*32])
        await client.call('blockchain.scripthash.assets.list', ['00'*32, 'connection-bound'])
        assert len(connections) == 1
        with pytest.raises(ValueError):
            await client.call('blockchain.transaction.broadcast', ['00'])
        await client.close()
        await client.call('server.features')
        assert len(connections) == 2
    finally:
        await client.close()
        server.close()
        await server.wait_closed()
