'''Browser-compatible transport checks with offline RPC fixtures; no Core access.'''

import asyncio

import aiohttp
import pytest
from aiorpcx import RPCSession, serve_ws

from tests.server.test_raptoreum_protocol import ASSET, HASHX, SCRIPTHASH
from tests.server.test_raptoreum_requests import CASES, routed_session


@pytest.mark.asyncio
@pytest.mark.parametrize('method,params', CASES, ids=[method for method, _ in CASES])
async def test_browser_websocket_coin_and_asset_requests(method, params):
    routed = routed_session()
    if method == 'blockchain.scripthash.assets.unsubscribe':
        await routed.assets_subscribe(SCRIPTHASH)
    if method == 'blockchain.scripthash.unsubscribe':
        await routed.scripthash_subscribe(SCRIPTHASH)

    class WalletSession(RPCSession):
        async def handle_request(self, request):
            return await routed.handle_request(request)

    server = await serve_ws(WalletSession, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect('http://127.0.0.1:'+str(port)) as ws:
                await ws.send_json({'jsonrpc':'2.0', 'id':1, 'method':method, 'params':params})
                message = await asyncio.wait_for(ws.receive(), 3)
                assert message.type == aiohttp.WSMsgType.TEXT
                response = message.json()
                assert response['id'] == 1 and 'error' not in response
                if method == 'blockchain.scripthash.assets.get_balance':
                    assert next(iter(response['result']['assets'].values()))['confirmed'] == '9007199254740993'
                elif method == 'blockchain.transaction.broadcast':
                    assert response['result'] == 'dd'*32
                    assert routed.recorded_broadcast == '01000000000000000000'
                elif method == 'server.features':
                    assert response['result']['rtm_assets']['ready'] is True
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize('asset', [False, True], ids=['coins', 'assets'])
async def test_browser_receives_coin_and_asset_subscription_notifications(asset):
    routed = routed_session()
    routed.mns = set()
    method = 'blockchain.scripthash.'+('assets.' if asset else '')+'subscribe'

    class WalletSession(RPCSession):
        def __init__(self, transport):
            super().__init__(transport)
            routed.send_notification = self.send_notification

        async def handle_request(self, request):
            return await routed.handle_request(request)

    server = await serve_ws(WalletSession, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect('http://127.0.0.1:'+str(port)) as ws:
                await ws.send_json({'jsonrpc':'2.0', 'id':1, 'method':method, 'params':[SCRIPTHASH]})
                initial = (await asyncio.wait_for(ws.receive(), 3)).json()['result']
                if asset:
                    routed.db.view['metadata'][ASSET]['name'] = 'UPDATED'
                    routed.mempool.asset_generation += 1
                else:
                    async def changed_history(*args):
                        return [(bytes.fromhex('ee'*32), 81)], 1
                    routed.session_mgr.limited_history = changed_history
                await routed.notify({HASHX}, False)
                notification = (await asyncio.wait_for(ws.receive(), 3)).json()
                assert notification['method'] == method
                assert notification['params'][0] == SCRIPTHASH
                assert notification['params'][1] != initial
                assert len(notification['params'][1]) == 64
    finally:
        server.close()
        await server.wait_closed()
