"""Exercise the public HTTP gateway against real loopback TCP/HTTP servers."""

import asyncio
from contextlib import asynccontextmanager
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import socket
import sys

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


MODULE = Path(__file__).parents[1] / 'electrumx' / 'http_gateway.py'
TOKEN = '0123456789abcdef0123456789abcdef'
OTHER_TOKEN = 'abcdef0123456789abcdef0123456789'
# Legacy environment tests clear os.environ without restoring Windows loader
# variables. Capture OS startup settings during collection, before tests run;
# deliberately omit node configuration and unrelated credentials.
MODULE_IMPORT_ENV = {
    key: value for key, value in os.environ.items()
    if key.upper() in {
        'SYSTEMROOT', 'WINDIR', 'PATH', 'PATHEXT', 'TEMP', 'TMP', 'COMSPEC',
        'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'HOME', 'LANG', 'LC_ALL',
        'PYTHONPATH', 'LD_LIBRARY_PATH', 'DYLD_LIBRARY_PATH',
    }
}


def gateway():
    assert MODULE.exists(), 'Standalone public HTTP gateway is not implemented'
    spec = importlib.util.spec_from_file_location('wallet_http_gateway_test', MODULE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@asynccontextmanager
async def running_gateway(responder=None, **options):
    module = gateway()
    calls, connections, writers, tasks = [], [], [], set()

    async def serve(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        writers.append(writer)
        connection = len(connections)
        connections.append(connection)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                request = json.loads(line)
                calls.append((connection, request))
                if responder:
                    result = await responder(connection, request, writer)
                else:
                    result = {'result': {'connection': connection}}
                if result is not None:
                    response = dict(jsonrpc='2.0', id=request['id'], **result)
                    writer.write(json.dumps(response).encode() + b'\n')
                    await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(task)

    server = await asyncio.start_server(serve, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    client = TestClient(TestServer(module.create_app(
        electrum_port=port, explorer_port=0, **options)))
    await client.start_server()
    try:
        yield client, calls, connections
    finally:
        await client.close()
        server.close()
        await server.wait_closed()
        for writer in writers:
            writer.close()
        for task in list(tasks):
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


async def rpc(client, method='server.features', params=None, token=TOKEN, id=7):
    response = await client.post('/api/v1/electrum', json={
        'jsonrpc': '2.0', 'id': id, 'method': method, 'params': params or []},
        headers={'X-Electrum-Session': token})
    return response, await response.json()


@pytest.mark.asyncio
async def test_health_and_cors_have_no_cookie_or_session_disclosure():
    async with running_gateway() as (client, calls, _):
        response = await client.get('/api/v1/health')
        assert response.status == 200
        assert await response.json() == {'ok': True}
        preflight = await client.options('/api/v1/electrum', headers={
            'Origin': 'chrome-extension://test',
            'Access-Control-Request-Headers': 'content-type,x-electrum-session'})
        assert preflight.status == 204
        assert preflight.headers['Access-Control-Allow-Origin'] == '*'
        assert 'x-electrum-session' in preflight.headers['Access-Control-Allow-Headers'].lower()
        assert 'Access-Control-Allow-Credentials' not in preflight.headers
        assert 'Set-Cookie' not in preflight.headers
        assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['stop', 'getinfo', 'daemon_url', 'server.add_peer',
                                   'wallet.signtransaction', 'blockchain.unknown'])
async def test_disallowed_methods_never_reach_tcp(method):
    async with running_gateway() as (client, calls, _):
        _, body = await rpc(client, method)
        assert body['error']['code'] == -32601
        assert body['id'] == 7
        assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [[], {}, {'method': 'server.features'},
    {'jsonrpc': '1.0', 'id': 1, 'method': 'server.features'},
    {'jsonrpc': '2.0', 'id': True, 'method': 'server.features'},
    {'jsonrpc': '2.0', 'id': 1, 'method': 'server.features', 'params': {}}])
async def test_malformed_rpc_never_reaches_tcp(payload):
    async with running_gateway() as (client, calls, _):
        response = await client.post('/api/v1/electrum', json=payload,
                                     headers={'X-Electrum-Session': TOKEN})
        assert (await response.json())['error']['code'] == -32600
        assert calls == []


@pytest.mark.asyncio
async def test_bad_json_missing_session_and_oversize_are_bounded():
    async with running_gateway(max_request_bytes=256) as (client, calls, _):
        response = await client.post('/api/v1/electrum', data='{bad',
            headers={'Content-Type': 'application/json', 'X-Electrum-Session': TOKEN})
        assert (await response.json())['error']['code'] == -32700
        response = await client.post('/api/v1/electrum', json={
            'jsonrpc': '2.0', 'id': 1, 'method': 'server.features'})
        assert response.status == 400
        response = await client.post('/api/v1/electrum', data='x' * 257,
            headers={'Content-Type': 'application/json', 'X-Electrum-Session': TOKEN})
        assert response.status == 413
        assert calls == []


@pytest.mark.asyncio
async def test_pagination_reuses_connection_and_notifications_never_escape():
    async def respond(connection, request, writer):
        writer.write(json.dumps({'jsonrpc': '2.0', 'method': 'blockchain.headers.subscribe',
                                  'params': [{'height': 999}]}).encode() + b'\n')
        writer.write(json.dumps({'jsonrpc': '2.0', 'id': -99, 'result': 'unrelated'}).encode() + b'\n')
        cursor = request['params'][2]
        if cursor:
            assert cursor == 'page-on-connection-{}'.format(connection)
            return {'result': {'items': ['second'], 'cursor': None}}
        return {'result': {'items': ['first'], 'cursor': 'page-on-connection-{}'.format(connection)}}

    async with running_gateway(respond) as (client, calls, connections):
        _, first = await rpc(client, 'blockchain.scripthash.assets.listunspent', ['ab'*32, 'cd'*32, None, 1000])
        _, second = await rpc(client, 'blockchain.scripthash.assets.listunspent',
                              ['ab'*32, 'cd'*32, first['result']['cursor'], 1000])
        assert second == {'jsonrpc': '2.0', 'id': 7,
                          'result': {'items': ['second'], 'cursor': None}}
        assert len(connections) == 1
        assert len(calls) == 2


@pytest.mark.asyncio
async def test_different_tokens_isolate_connections_and_one_token_serializes_calls():
    async def respond(connection, request, writer):
        await asyncio.sleep(0.01)
        return {'result': [connection, request['params']]}

    async with running_gateway(respond) as (client, calls, connections):
        replies = await asyncio.gather(*[rpc(client, params=[i], id=i) for i in range(5)])
        assert [body['result'] for _, body in replies] == [[0, [i]] for i in range(5)]
        _, other = await rpc(client, token=OTHER_TOKEN)
        assert other['result'][0] == 1
        assert len(connections) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('code', [-32001, -32601, -32602, 1])
async def test_upstream_numeric_errors_survive_without_backend_text(code):
    async def respond(connection, request, writer):
        return {'error': {'code': code, 'message': 'secret password=x http://user:pass@core:10225'}}
    async with running_gateway(respond) as (client, _, _):
        _, body = await rpc(client)
        assert body['error']['code'] == code
        assert 'secret' not in json.dumps(body)
        assert 'pass' not in json.dumps(body)
        if code == -32001:
            assert 'synchroniz' in body['error']['message'].lower()


@pytest.mark.asyncio
async def test_disconnect_after_broadcast_is_unknown_and_sent_only_once():
    async def respond(connection, request, writer):
        writer.close()
        return None
    async with running_gateway(respond) as (client, calls, _):
        _, body = await rpc(client, 'blockchain.transaction.broadcast', ['deadbeef'])
        assert body['error']['code'] != -32001
        assert 'unknown' in body['error']['message'].lower()
        assert 'retry' not in body['error']['message'].lower()
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_timeout_after_broadcast_is_unknown_without_replay():
    async def respond(connection, request, writer):
        await asyncio.sleep(5)
        return {'result': 'txid'}
    async with running_gateway(respond, request_timeout=0.5) as (client, calls, _):
        _, body = await rpc(client, 'blockchain.transaction.broadcast', ['deadbeef'])
        assert body['error']['code'] != -32001
        assert 'unknown' in body['error']['message'].lower()
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_unavailable_upstream_is_safe_sync_error():
    module = gateway()
    server = await asyncio.start_server(lambda r, w: None, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    async with TestClient(TestServer(module.create_app(electrum_port=port, explorer_port=0))) as client:
        _, body = await rpc(client)
        assert body['error']['code'] == -32001
        assert 'synchroniz' in body['error']['message'].lower()
        assert str(port) not in body['error']['message']


@pytest.mark.asyncio
async def test_session_capacity_rejects_then_evicts_idle_sessions():
    async with running_gateway(max_sessions=1, idle_timeout=0.05) as (client, calls, connections):
        await rpc(client)
        response, body = await rpc(client, token=OTHER_TOKEN)
        assert response.status == 503
        assert body['error']['code'] == -32001
        assert len(connections) == 1
        await asyncio.sleep(0.08)
        _, body = await rpc(client, token=OTHER_TOKEN)
        assert body['result']['connection'] == 1


@pytest.mark.asyncio
async def test_concurrent_requests_are_rejected_above_limit():
    entered, finish = asyncio.Event(), asyncio.Event()
    async def respond(connection, request, writer):
        entered.set()
        await finish.wait()
        return {'result': True}
    async with running_gateway(respond, max_concurrency=1) as (client, calls, _):
        pending = asyncio.create_task(rpc(client))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            response, body = await rpc(client, token=OTHER_TOKEN)
            assert response.status == 503
            assert body['error']['code'] == -32001
            assert len(calls) == 1
        finally:
            finish.set()
            await pending


@pytest.mark.asyncio
async def test_status_only_reads_fixed_local_public_path_and_filters_secrets():
    module = gateway()
    paths = []
    async def status(request):
        paths.append(request.path_qs)
        return web.json_response({'ready': False, 'phase': 'index_syncing',
            'core': {'blocks': 100, 'password': 'secret'},
            'electrumx': {'indexed_height': 50, 'percent': 50}, 'credentials': 'secret'})
    explorer = web.Application()
    explorer.router.add_get('/api/v1/status', status)
    async with TestServer(explorer) as upstream:
        async with TestClient(TestServer(module.create_app(explorer_port=upstream.port))) as client:
            response = await client.get('/api/v1/status?url=http://evil/')
            body = await response.json()
            assert body['phase'] == 'index_syncing'
            assert body['electrumx']['indexed_height'] == 50
            assert 'secret' not in json.dumps(body)
            assert paths == ['/api/v1/status']


@pytest.mark.asyncio
async def test_servers_relays_only_fixed_shape_gateway_urls():
    module = gateway()
    async def servers(request):
        return web.json_response({'discovery': True, 'updated_at': 5, 'secret': 'x', 'servers': [
            {'url': 'https://203.0.113.9/api/v1/electrum', 'height': 10, 'seen_at': 4,
             'assets_ready': True, 'smartnode': True, 'operator': 'secret'},
            {'url': 'https://node.example/api/v1/electrum', 'height': 9, 'seen_at': 3},
            {'url': 'https://evil.example/api/v1/electrum?x=1', 'height': 9, 'seen_at': 3},
            {'url': 'http://198.51.100.1/api/v1/electrum', 'height': 9, 'seen_at': 3},
            {'url': 'https://user@evil/api/v1/electrum', 'height': 9, 'seen_at': 3},
            {'url': 'https://198.51.100.2/api/v1/electrum', 'height': 'tall', 'seen_at': 3}]})
    explorer = web.Application()
    explorer.router.add_get('/api/v1/servers', servers)
    async with TestServer(explorer) as upstream:
        async with TestClient(TestServer(module.create_app(explorer_port=upstream.port))) as client:
            response = await client.get('/api/v1/servers')
            body = await response.json()
            assert response.headers['Access-Control-Allow-Origin'] == '*'
            assert [s['url'] for s in body['servers']] == [
                'https://203.0.113.9/api/v1/electrum', 'https://node.example/api/v1/electrum']
            assert body['servers'][1] == {'url': 'https://node.example/api/v1/electrum', 'height': 9,
                                          'seen_at': 3, 'assets_ready': False, 'smartnode': False}
            assert 'secret' not in json.dumps(body)
    async with TestClient(TestServer(module.create_app(explorer_port=0))) as client:
        assert await (await client.get('/api/v1/servers')).json() == {'servers': [], 'discovery': False}


def test_nonloopback_upstream_and_invalid_limits_are_rejected():
    module = gateway()
    for host in ['example.com', '192.168.1.1', '0.0.0.0']:
        with pytest.raises(ValueError):
            module.create_app(electrum_host=host)
    for options in [{'max_sessions': 0}, {'max_concurrency': 0}, {'request_timeout': 0}]:
        with pytest.raises(ValueError):
            module.create_app(**options)


@pytest.mark.asyncio
@pytest.mark.parametrize('raw', [b'{"jsonrpc":"2.0","id":1,"error":null}\n',
    b'{"jsonrpc":"2.0","id":1,"result":NaN}\n', b'not-json\n'])
async def test_invalid_upstream_responses_produce_safe_protocol_error(raw):
    async def respond(connection, request, writer):
        writer.write(raw)
        await writer.drain()
        return None
    async with running_gateway(respond) as (client, _, _):
        response, body = await rpc(client)
        assert response.status == 200
        assert 'error' in body
        assert 'Traceback' not in json.dumps(body)


@pytest.mark.asyncio
async def test_nonfinite_json_is_rejected_without_tcp():
    async with running_gateway() as (client, calls, _):
        response = await client.post('/api/v1/electrum',
            data='{"jsonrpc":"2.0","id":1,"method":"server.features","params":[NaN]}',
            headers={'Content-Type': 'application/json', 'X-Electrum-Session': TOKEN})
        assert (await response.json())['error']['code'] == -32700
        assert calls == []


@pytest.mark.asyncio
async def test_status_redirect_cannot_reach_an_arbitrary_endpoint():
    module = gateway()
    paths = []
    async def redirect(request):
        paths.append(request.path)
        raise web.HTTPFound('/private')
    async def private(request):
        paths.append(request.path)
        return web.json_response({'secret': 'credential'})
    explorer = web.Application()
    explorer.router.add_get('/api/v1/status', redirect)
    explorer.router.add_get('/private', private)
    async with TestServer(explorer) as upstream:
        async with TestClient(TestServer(module.create_app(explorer_port=upstream.port))) as client:
            response = await client.get('/api/v1/status')
            assert response.status == 503
            assert paths == ['/api/v1/status']


@pytest.mark.asyncio
async def test_uuid_session_spelling_reuses_the_same_socket():
    async with running_gateway() as (client, _, connections):
        _, first = await rpc(client)
        _, second = await rpc(client, token='01234567-89ab-cdef-0123-456789abcdef')
        assert first['result'] == second['result']
        assert len(connections) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('token', ['arbitrary-session-name', 'http://127.0.0.1:10225/'])
async def test_invalid_session_tokens_never_open_upstream(token):
    async with running_gateway() as (client, calls, connections):
        response, body = await rpc(client, token=token)
        assert response.status == 400
        assert body['error']['code'] == -32600
        assert connections == []
        assert calls == []


@pytest.mark.asyncio
async def test_subscription_polls_return_current_values_on_same_connection():
    height = 100
    async def respond(connection, request, writer):
        return {'result': {'height': height}}
    async with running_gateway(respond) as (client, _, connections):
        _, first = await rpc(client, 'blockchain.headers.subscribe')
        height = 101
        _, second = await rpc(client, 'blockchain.headers.subscribe')
        assert first['result']['height'] == 100
        assert second['result']['height'] == 101
        assert len(connections) == 1


@pytest.mark.parametrize('clear_parent_environment', [False, True])
def test_module_entry_point_rejects_nonloopback_environment(monkeypatch, clear_parent_environment):
    if clear_parent_environment:
        for key in list(os.environ):
            monkeypatch.delenv(key)
    result = subprocess.run([sys.executable, '-m', 'electrumx.http_gateway'],
        cwd=MODULE.parents[1], capture_output=True, text=True, timeout=15,
        env={**MODULE_IMPORT_ENV, 'WALLET_ELECTRUM_HOST': '192.168.1.1'})
    assert result.returncode == 2
    assert 'loopback' in result.stderr


@pytest.mark.asyncio
async def test_default_session_limit_leaves_room_for_backend_connections():
    async with running_gateway() as (client, calls, connections):
        for index in range(32):
            response, body = await rpc(client, token=format(index, '032x'))
            assert response.status == 200
            assert 'result' in body
        response, body = await rpc(client, token=format(32, '032x'))
        assert response.status == 503
        assert body['error']['code'] == -32001
        assert len(connections) == 32
        assert len(calls) == 32


@pytest.mark.parametrize('arguments,host', [([], '0.0.0.0'),
    (['--host', '0.0.0.0'], '127.0.0.1'),
    (['--host', '192.0.2.1'], '127.0.0.1')])
def test_module_entry_point_rejects_public_http_bind(arguments, host):
    with socket.socket() as unused:
        unused.bind(('127.0.0.1', 0))
        port = unused.getsockname()[1]
    result = subprocess.run([sys.executable, '-m', 'electrumx.http_gateway',
                             '--port', str(port), *arguments],
        cwd=MODULE.parents[1], capture_output=True, text=True, timeout=3,
        env={**MODULE_IMPORT_ENV, 'WALLET_HTTP_HOST': host,
             'WALLET_ELECTRUM_HOST': '127.0.0.1'})
    assert result.returncode == 2
    assert 'loopback' in result.stderr
