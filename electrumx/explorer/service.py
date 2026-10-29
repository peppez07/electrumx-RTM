'''Bounded REST endpoints and same-origin explorer UI.'''

import asyncio
import hashlib
import json
import re
import time
from collections import OrderedDict
from decimal import Decimal
from pathlib import Path

from aiohttp import web

from electrumx.lib.coins import Raptoreum
from electrumx.lib.raptoreum_assets import parse_asset_script
from .clients import BackendError

HEX = re.compile(r'^[0-9a-fA-F]{64}$')
STATIC = Path(__file__).parent / 'static'
MONEY_FIELDS = frozenset({
    'value', 'valueSat', 'value_atomic', 'amount', 'Amount', 'amount_atomic',
    'supply', 'Circulating_supply', 'balance', 'balanceRTM', 'balanceSatoshis',
    'balance_atomic', 'confirmed', 'unconfirmed', 'fee', 'fee_atomic',
    'avgfee', 'avgfeerate', 'maxfee', 'maxfeerate', 'medianfee', 'medianfeerate',
    'minfee', 'minfeerate', 'totalfee', 'total_out', 'subsidy',
    'mempoolminfee', 'minrelaytxfee', 'locked_rtm_sat',
})


def normalize(value, field=None):
    if field in MONEY_FIELDS and isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return format(value, 'f') if isinstance(value, Decimal) else str(value)
    if field == 'feerate_percentiles' and isinstance(value, (list, tuple)):
        return [normalize(v, 'fee') for v in value]
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > 9007199254740991:
        return str(value)
    if isinstance(value, Decimal):
        return format(value, 'f')
    if isinstance(value, dict):
        return {k: normalize(v, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize(v) for v in value]
    return value


def json_response(value, **kwargs):
    return web.json_response(normalize(value), **kwargs)


class APIError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def number(value, default=0, maximum=1000000000, minimum=0):
    if value is None:
        return default
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or len(value) > 10:
        raise APIError(400, 'invalid_input', 'Expected a non-negative integer.')
    result = int(value)
    if not minimum <= result <= maximum:
        raise APIError(400, 'invalid_input', 'Number is outside the permitted range.')
    return result


def hash_id(value):
    if not HEX.fullmatch(value):
        raise APIError(400, 'invalid_input', 'Expected a 64-character hash or asset ID.')
    return value.lower()


def address_hash(value):
    try:
        if len(value) > 60:
            raise ValueError()
        script = Raptoreum.pay_to_address_script(value)
        return hashlib.sha256(script).digest()[::-1].hex()
    except Exception:
        raise APIError(400, 'invalid_address', 'Expected a valid RTM mainnet address.') from None


def pagination(request, maximum=100):
    return (number(request.query.get('limit'), default=25, maximum=maximum, minimum=1),
            number(request.query.get('offset'), maximum=100000))


def page(items, limit, offset):
    return {'items': items[offset:offset+limit],
            'next_offset': offset+limit if len(items) > offset+limit else None}


class Explorer:
    def __init__(self, core, electrum, status_ttl):
        self.core, self.electrum = core, electrum
        self.status_ttl = status_ttl
        self.cached_status = None
        self.core_info = None
        self.checked = 0
        self.status_lock = asyncio.Lock()

    async def status(self, fresh=False):
        async with self.status_lock:
            if not fresh and self.cached_status and time.monotonic()-self.checked < self.status_ttl:
                return self.cached_status
            result = {'network': 'mainnet', 'ready': False, 'phase': 'core_unavailable',
                      'core': {}, 'electrumx': {'ready': False},
                      'checked_at': int(time.time())}
            try:
                info = await self.core.call('getblockchaininfo')
                self.core_info = info
                result['core'] = {k: info.get(k) for k in (
                    'chain', 'blocks', 'headers', 'bestblockhash',
                    'initialblockdownload', 'verificationprogress')}
                if info.get('chain') != 'main':
                    result['phase'] = 'wrong_network'
                elif (info.get('initialblockdownload') is not False or
                      info.get('blocks', -1) < info.get('headers', 0)):
                    result['phase'] = 'core_syncing'
                else:
                    result['phase'] = 'index_unavailable'
                    features = await self.electrum.call('server.features')
                    assets = features.get('rtm_assets', {})
                    result['electrumx'] = assets
                    result['ready'] = bool(assets.get('ready') is True and
                        assets.get('indexed_height') == info.get('blocks') and
                        assets.get('indexed_tip') == info.get('bestblockhash'))
                    result['phase'] = 'ready' if result['ready'] else 'index_syncing'
            except BackendError:
                pass
            self.cached_status, self.checked = result, time.monotonic()
            return result

    async def gate(self, fresh=False):
        status = await self.status(fresh=fresh)
        if not status['ready']:
            raise APIError(503, 'synchronizing', 'Chain data is unavailable until Core and the asset index synchronize.')
        return status

    async def block(self, identifier):
        if identifier.isdecimal():
            height = number(identifier)
            block_hash = await self.core.call('getblockhash', [height])
        else:
            block_hash = hash_id(identifier)
        return await self.core.call('getblock', [block_hash, 1])

    async def blocks(self, request):
        status = await self.gate()
        limit = number(request.query.get('limit'), default=10, maximum=25, minimum=1)
        start = number(request.query.get('start'), default=status['core']['blocks'])
        if start > status['core']['blocks']:
            raise APIError(404, 'not_found', 'Block is beyond the indexed chain.')
        result = []
        for height in range(start, max(-1, start-limit), -1):
            block = await self.block(str(height))
            result.append({k: v for k, v in block.items() if k not in ('tx', 'hex')})
            result[-1]['tx_count'] = len(block['tx'])
        return json_response({'items': result, 'next_start': start-limit if start >= limit else None})

    async def block_detail(self, request):
        await self.gate()
        limit, offset = pagination(request)
        block = await self.block(request.match_info['id'])
        ids = block.get('tx', [])
        block.update(tx_count=len(ids), tx=ids[offset:offset+limit],
                     next_offset=offset+limit if len(ids) > offset+limit else None)
        return json_response(block)

    async def transaction_data(self, txid, resolve_inputs=True):
        result = await self.core.call('getrawtransaction', [hash_id(txid), True])
        result.pop('hex', None)
        result['coinbase'] = any('coinbase' in vin for vin in result.get('vin', []))
        for output in result.get('vout', []):
            value = Decimal(str(output.get('value', '0')))*100000000
            if value != value.to_integral_value():
                raise BackendError()
            output['value_atomic'] = str(int(value))
            output['value'] = format(Decimal(str(output.get('value', '0'))), 'f')
            script = output.get('scriptPubKey', {}).get('hex', '')
            try:
                asset = parse_asset_script(bytes.fromhex(script))
                if asset:
                    output['asset'] = {'asset_id': asset.asset_id, 'amount_atomic': str(asset.amount),
                        'is_unique': asset.is_unique, 'unique_id': str(asset.unique_id) if asset.unique_id is not None else None,
                        'unique_end': str(asset.unique_end) if asset.unique_end is not None else None}
            except ValueError:
                output['asset_parse_error'] = True
        result['fee_atomic'] = None
        result['fee_status'] = 'coinbase' if result['coinbase'] else 'unavailable'
        inputs = result.get('vin', [])
        if resolve_inputs and not result['coinbase'] and 0 < len(inputs) <= 32:
            total = 0
            try:
                previous = {}
                for vin in inputs:
                    prev_id = hash_id(vin['txid'])
                    if prev_id not in previous:
                        previous[prev_id] = await self.core.call('getrawtransaction', [prev_id, True])
                    output = next(o for o in previous[prev_id]['vout'] if o['n'] == vin['vout'])
                    atomic = Decimal(str(output['value']))*100000000
                    if atomic != atomic.to_integral_value():
                        raise ValueError()
                    total += int(atomic)
                    vin['prevout'] = {'value_atomic': str(int(atomic)),
                                      'scriptPubKey': output.get('scriptPubKey', {})}
                fee = total-sum(int(o['value_atomic']) for o in result.get('vout', []))
                if fee >= 0:
                    result['fee_atomic'], result['fee_status'] = str(fee), 'resolved'
            except (BackendError, KeyError, ValueError, StopIteration):
                pass
        return result

    async def transaction(self, request):
        await self.gate()
        return json_response(await self.transaction_data(request.match_info['id']))

    async def transactions(self, request):
        status = await self.gate()
        limit, offset = pagination(request, 25)
        block = await self.block(request.query.get('block', str(status['core']['blocks'])))
        ids = block['tx']
        items = []
        for txid in ids[offset:offset+limit]:
            item = await self.transaction_data(txid, resolve_inputs=False)
            items.append({k: item.get(k) for k in ('txid', 'time', 'size', 'confirmations', 'coinbase', 'type')})
        return json_response({'items': items, 'block': block.get('height'),
                              'next_offset': offset+limit if len(ids) > offset+limit else None})

    async def address(self, request):
        await self.gate()
        address = request.match_info['address']
        scripthash = address_hash(address)
        balance = await self.electrum.call('blockchain.scripthash.get_balance', [scripthash])
        return json_response({'address': address, 'scripthash': scripthash,
            'balance': {k: str(balance[k]) for k in ('confirmed', 'unconfirmed')}})

    async def address_records(self, request):
        await self.gate()
        scripthash = address_hash(request.match_info['address'])
        limit, offset = pagination(request)
        mode = request.match_info['mode']
        if mode not in ('history', 'utxos'):
            raise web.HTTPNotFound()
        method = 'get_history' if mode == 'history' else 'listunspent'
        records = await self.electrum.call('blockchain.scripthash.'+method, [scripthash])
        records = [{k: str(v) if k in ('fee', 'value') else v for k, v in item.items()}
                   for item in records]
        if mode == 'utxos':
            records = [dict(item, value=str(item['value'])) for item in records]
        result = page(records, limit, offset)
        result['source'] = 'electrumx'
        result['note'] = 'Subject to the ElectrumX server history/response limit; oversized histories return an error.'
        return json_response(result)

    async def address_assets(self, request):
        await self.gate()
        scripthash = address_hash(request.match_info['address'])
        limit = number(request.query.get('limit'), default=25, maximum=100, minimum=1)
        cursor = request.query.get('cursor') or None
        if cursor and len(cursor) > 1024:
            raise APIError(400, 'invalid_input', 'Asset cursor is too long.')
        aid = request.match_info.get('asset')
        if aid:
            hash_id(aid)
            mode = request.match_info['mode']
            if mode not in ('history', 'utxos'):
                raise web.HTTPNotFound()
            method = 'get_history' if mode == 'history' else 'listunspent'
            args = [scripthash, aid.lower(), cursor, limit]
        else:
            method, args = 'list', [scripthash, cursor, limit]
        result = await self.electrum.call('blockchain.scripthash.assets.'+method, args)
        return json_response(result)

    async def assets(self, request):
        await self.gate()
        limit, offset = pagination(request)
        records = await self.core.call('listassets', [True, limit+1, offset])
        if not isinstance(records, dict):
            raise BackendError('unsupported')
        items = list(records.values())
        return json_response({'items': items[:limit], 'next_offset': offset+limit if len(items) > limit else None})

    async def asset(self, request):
        await self.gate()
        return json_response(await self.core.call('getassetdetailsbyid', [hash_id(request.match_info['id'])]))

    async def holders(self, request):
        await self.gate()
        limit, offset = pagination(request)
        metadata = await self.core.call('getassetdetailsbyid', [hash_id(request.match_info['id'])])
        name = metadata.get('Asset_name')
        if not name:
            raise BackendError()
        records = await self.core.call('listaddressesbyasset', [name, False, limit+1, offset])
        if not isinstance(records, dict):
            raise BackendError('unsupported')
        items = [{'address': k, 'balance_atomic': str(v)} for k, v in records.items()]
        return json_response({'items': items[:limit], 'next_offset': offset+limit if len(items) > limit else None})

    async def search(self, request):
        await self.gate()
        query = request.query.get('q', '').strip()
        if not query or len(query) > 128:
            raise APIError(400, 'invalid_input', 'Enter an address, transaction, block or asset.')
        matches = []
        if query.isascii() and query.isdecimal():
            block = await self.block(query)
            matches.append({'type': 'block', 'id': str(block['height'])})
        elif HEX.fullmatch(query):
            query = query.lower()
            for kind, method, params in [('transaction', 'getrawtransaction', [query, True]),
                    ('block', 'getblock', [query, 1]), ('asset', 'getassetdetailsbyid', [query])]:
                try:
                    await self.core.call(method, params)
                    matches.append({'type': kind, 'id': query})
                except BackendError as e:
                    if e.code not in ('not_found', 'unsupported'):
                        raise
        else:
            try:
                address_hash(query)
                matches.append({'type': 'address', 'id': query})
            except APIError:
                metadata = await self.core.call('getassetdetailsbyname', [query])
                aid = metadata.get('Asset_id') or metadata.get('Asset_Id')
                if aid:
                    matches.append({'type': 'asset', 'id': aid})
        return json_response({'matches': matches})


def create_app(core, electrum, rate_limit=120, status_ttl=10, enable_wallet_api=False):
    from .compat import PUBLIC_STATUS_PATHS, register_compat
    from .wallet import (CAPABILITIES_PATH, POST_PATHS, BROADCAST_STARTED,
                         SUBMITTED_TXID, register_wallet, unknown_submission)
    explorer = Explorer(core, electrum, status_ttl)
    visitors = OrderedDict()
    semaphore = asyncio.Semaphore(8)

    async def guarded(request, handler):
        if (request.method == 'OPTIONS' or request.path == '/api/v1/status' or
                request.path == CAPABILITIES_PATH or request.path in PUBLIC_STATUS_PATHS or
                (request.path in POST_PATHS and not enable_wallet_api)):
            return await handler(request)
        before = await explorer.gate(fresh=True)
        snapshot = (before['core']['blocks'], before['core']['bestblockhash'])
        response = await handler(request)
        # A successful relay is final for this HTTP request. A later tip change
        # must not turn an accepted submission into an error and prompt retries.
        if request.method == 'POST' and request.path == '/api/v1/wallet/broadcast':
            return response
        after = await explorer.gate(fresh=True)
        if snapshot != (after['core']['blocks'], after['core']['bestblockhash']):
            raise APIError(503, 'synchronizing', 'Indexed chain changed during this request; retry.')
        response.headers['X-Indexed-Height'] = str(snapshot[0])
        response.headers['X-Indexed-Tip'] = snapshot[1]
        return response

    @web.middleware
    async def boundary(request, handler):
        try:
            if request.path.startswith('/api/'):
                now, key = time.monotonic(), request.remote or 'local'
                count, since = visitors.pop(key, (0, now))
                if now-since >= 60:
                    count, since = 0, now
                visitors[key] = (count+1, since)
                while len(visitors) > 4096:
                    visitors.popitem(last=False)
                if count >= rate_limit:
                    raise APIError(429, 'rate_limited', 'Please wait before making more requests.')
                if semaphore.locked():
                    raise APIError(503, 'busy', 'Explorer is busy; please retry.')
                async with semaphore:
                    response = await asyncio.wait_for(guarded(request, handler), 45)
            else:
                response = await handler(request)
        except APIError as e:
            response = json_response({'error': {'code': e.code, 'message': e.message}}, status=e.status)
        except BackendError as e:
            status = {'not_found': 404, 'synchronizing': 503, 'unsupported': 503,
                      'invalid_transaction':400, 'missing_inputs':409,
                      'transaction_rejected':422, 'already_known':409}.get(e.code, 502)
            message = {'invalid_transaction':'Core could not decode this transaction.',
                'missing_inputs':'Core could not find the required unspent inputs.',
                'transaction_rejected':'Core rejected this transaction under consensus or relay policy.',
                'already_known':'Transaction is already known to the chain; check its status.'}.get(
                    e.code, 'Backend data unavailable. Retry after synchronization; restart pagination if the cursor expired.')
            response = json_response({'error': {'code': e.code, 'message':message}}, status=status)
        except web.HTTPException as e:
            response = json_response({'error': {'code': 'not_found' if e.status == 404 else 'request_error',
                                               'message': e.reason}}, status=e.status)
        except Exception:
            if request.get(BROADCAST_STARTED):
                response = unknown_submission(request.get(SUBMITTED_TXID))
            else:
                response = json_response({'error': {'code': 'unavailable', 'message': 'Explorer data is temporarily unavailable.'}}, status=502)
        if response.status in (429, 503):
            response.headers['Retry-After'] = '15'
        response.headers.update({'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"})
        if request.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
            response.headers['Access-Control-Allow-Methods'] = (
                'GET, POST, OPTIONS' if request.path in POST_PATHS else 'GET, OPTIONS')
        return response

    app = web.Application(middlewares=[boundary], client_max_size=210000)

    async def status(request):
        return json_response(await explorer.status())

    async def index(request):
        return web.FileResponse(STATIC/'index.html')

    app.router.add_get('/api/v1/status', status)
    app.router.add_get('/api/v1/search', explorer.search)
    app.router.add_get('/api/v1/blocks', explorer.blocks)
    app.router.add_get('/api/v1/blocks/{id}', explorer.block_detail)
    app.router.add_get('/api/v1/transactions', explorer.transactions)
    app.router.add_get('/api/v1/transactions/{id}', explorer.transaction)
    app.router.add_get('/api/v1/addresses/{address}', explorer.address)
    app.router.add_get('/api/v1/addresses/{address}/assets', explorer.address_assets)
    app.router.add_get('/api/v1/addresses/{address}/assets/{asset}/{mode}', explorer.address_assets)
    app.router.add_get('/api/v1/addresses/{address}/{mode}', explorer.address_records)
    app.router.add_get('/api/v1/assets', explorer.assets)
    register_wallet(app, explorer, enable_wallet_api)
    app.router.add_get('/api/v1/assets/{id}/holders', explorer.holders)
    app.router.add_get('/api/v1/assets/{id}', explorer.asset)
    register_compat(app, explorer)
    async def preflight(request):
        return web.Response(status=204)
    for resource in list(app.router.resources()):
        if resource.canonical.startswith('/api/'):
            resource.add_route('OPTIONS', preflight)
    app.router.add_get('/', index)
    if STATIC.exists():
        app.router.add_static('/static/', STATIC, show_index=False)
    async def close(app):
        for backend in (core, electrum):
            if hasattr(backend, 'close'):
                await backend.close()
    app.on_cleanup.append(close)
    return app
