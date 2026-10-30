'''Explicit aliases for the public explorer.raptoreum.com API vocabulary.

Amounts remain strings even when the reference returns unsafe JSON numbers.
No remote explorer is contacted; all chain results come from local backends.
'''

import asyncio
import json
import time
from decimal import Decimal, localcontext
from urllib.parse import parse_qs

from aiohttp import web

from .clients import BackendError
from .service import APIError, address_hash, hash_id, json_response, normalize, number, pagination, page

PUBLIC_STATUS_PATHS = frozenset({
    '/api/getblockcount', '/api/getblockchaininfo', '/api/project',
    '/api/supply', '/api/marketcap', '/api/getrichlist',
})


def atomic_to_rtm(value):
    with localcontext() as context:
        context.prec = 100
        return format(Decimal(str(value))/Decimal(100000000), '.8f')


def json_mode(request, legacy=None):
    value = request.query.get('json')
    if value is None and legacy is not None:
        value = parse_qs(legacy, keep_blank_values=True).get('json', ['false'])[0]
    if value is None:
        return False
    if value.lower() not in ('true', 'false', ''):
        raise APIError(400, 'invalid_input', 'json must be true or false.')
    return value.lower() != 'false'


class CompatibilityAPI:
    def __init__(self, explorer):
        self.explorer = explorer
        self.large_lock = asyncio.Lock()

    async def call(self, method, params=None):
        if self.large_lock.locked():
            raise APIError(503, 'busy', 'A large explorer query is already running; retry.')
        async with self.large_lock:
            result = await self.explorer.core.call(method, params or [])
            if len(json.dumps(normalize(result)).encode()) > 4*1024*1024:
                raise APIError(503, 'response_too_large', 'Result exceeds the compatibility response limit.')
            return result

    async def block_id(self, request):
        height, block_hash = request.query.get('height'), request.query.get('hash')
        if (height is None) == (block_hash is None):
            raise APIError(400, 'invalid_input', 'Provide exactly one of height or hash.')
        if height is not None:
            return await self.explorer.core.call('getblockhash', [number(height)])
        return hash_id(block_hash)

    async def status(self, request):
        state = await self.explorer.status(fresh=True)
        if state['core'].get('chain') != 'main' or state['phase'] in ('core_unavailable', 'wrong_network'):
            raise BackendError()
        if request.path.endswith('getblockchaininfo'):
            return json_response({'success':'true', 'blockcount':self.explorer.core_info})
        count = state['core']['blocks']
        if json_mode(request):
            return json_response({'success':'true', 'blockcount':count})
        return web.Response(text=str(count), content_type='text/plain')

    async def block(self, request):
        block_hash = await self.block_id(request)
        return json_response({'success':'true', 'getblock':await self.call('getblock', [block_hash, 2])})

    async def blockstats(self, request):
        block_hash = await self.block_id(request)
        result = await self.call('getblockstats', [block_hash])
        monetary = {'avgfee', 'avgfeerate', 'maxfee', 'maxfeerate', 'medianfee',
                    'medianfeerate', 'minfee', 'minfeerate', 'totalfee', 'total_out', 'subsidy'}
        result = {k:str(v) if k in monetary else v for k,v in result.items()}
        return json_response({'success':'true', 'getblockstats':result})

    async def address(self, request):
        original = request.match_info.get('address', request.query.get('address', ''))
        address, _, legacy = original.partition('&')
        scripthash = address_hash(address)
        balance = await self.explorer.electrum.call('blockchain.scripthash.get_balance', [scripthash])
        atomic = str(balance['confirmed'])
        full = atomic_to_rtm(atomic)
        if json_mode(request, legacy):
            return json_response({'success':'true', 'address':address,
                                  'balanceRTM':full, 'balanceSatoshis':atomic})
        return web.Response(text=full, content_type='text/plain')

    async def asset(self, request):
        aid = hash_id(request.match_info.get('id', request.query.get('id', '')))
        metadata = await self.explorer.core.call('getassetdetailsbyid', [aid])
        name = metadata.get('Asset_name', '')
        distribution = metadata.get('Distribution', {})
        data = {'name':name, 'isRoot':'|' not in name,
                'isUnique':metadata.get('Isunique'), 'updatable':metadata.get('Updatable'),
                'decimalPoint':metadata.get('Decimalpoint'), 'maxMintCount':metadata.get('maxMintCount'),
                'referenceHash':metadata.get('ReferenceHash'), 'ownerAddress':metadata.get('owner'),
                'targetAddress':distribution.get('TargetAddress'),
                'issueFrequency':distribution.get('IssueFrequency')}
        asset = dict(metadata, _id=aid, name=name, data=data,
                     amount=str(metadata.get('Circulating_supply', '0')))
        return json_response({'success':True, 'asset':asset})

    async def transaction(self, request):
        txid = hash_id(request.match_info.get('id', request.query.get('txid', '')))
        result = await self.call('getrawtransaction', [txid, True])
        return json_response({'success':True, 'transaction':result})

    async def simple(self, request):
        name = request.match_info['operation']
        method, params, key = {
            'getmininginfo':('getmininginfo', [], 'mininginfo'),
            'getmempoolinfo':('getmempoolinfo', [], 'getmempoolinfo'),
            'getrawmempool':('getrawmempool', [], 'getmempoolinfo'),
            'getsmartnodecount':('smartnode', ['count'], 'getsamrtnodecount'),
        }[name]
        result = await self.call(method, params)
        if isinstance(result, (dict, list)) and len(result) > 5000:
            raise APIError(503, 'response_too_large', 'Legacy query exceeds 5000 records; result was not truncated.')
        return json_response({'success':'true', key:result})

    async def smartnodes(self, request):
        result = await self.call('smartnodelist', ['json'])
        if not isinstance(result, dict):
            raise BackendError()
        if len(result) > 5000:
            raise APIError(503, 'response_too_large', 'Smartnode response exceeds 5000 records.')
        return json_response([dict(value, _id=key, id=key) for key,value in result.items()])

    async def public_snapshot(self, name, factory):
        state = await self.explorer.gate()
        async def load():
            return dict(await factory(), snapshot_at=int(time.time()),
                        indexed_tip=state['core']['bestblockhash'])
        return await self.explorer.public_cache.get(
            (name,state['core']['bestblockhash']), load, 30)

    async def network(self, request):
        async def load():
            result = {}
            for name,method,params in (('mining','getmininginfo',[]),
                    ('mempool','getmempoolinfo',[]), ('smartnodes','smartnode',['count'])):
                try:
                    result[name] = await self.call(method, params)
                except BackendError:
                    result[name] = None
            return result
        return json_response(await self.public_snapshot('network', load))

    async def node_page(self, request):
        limit, offset = pagination(request)
        async def load():
            result = await self.call('smartnodelist', ['json'])
            if not isinstance(result, dict):
                raise BackendError()
            if len(result) > 5000:
                raise APIError(503, 'response_too_large', 'Smartnode response exceeds 5000 records.')
            return {'items':[dict(value, id=key) for key,value in sorted(result.items())]}
        data = await self.public_snapshot('smartnodes', load)
        return json_response(dict(page(data['items'], limit, offset),
            total=len(data['items']), snapshot_at=data['snapshot_at'], indexed_tip=data['indexed_tip']))


def register_compat(app, explorer):
    api = CompatibilityAPI(explorer)
    app.router.add_get('/api/v1/network', api.network)
    app.router.add_get('/api/v1/smartnodes', api.node_page)
    for path in ('getblockcount', 'getblockchaininfo'):
        app.router.add_get('/api/'+path, api.status)
    app.router.add_get('/api/getblock', api.block)
    app.router.add_get('/api/getblockstats', api.blockstats)
    app.router.add_get('/api/getaddressbalance/{address}', api.address)
    app.router.add_get('/api/getaddressbalance', api.address)
    app.router.add_get('/api/asset/{id}', api.asset)
    app.router.add_get('/api/asset', api.asset)
    app.router.add_get('/api/gettransaction/{id}', api.transaction)
    app.router.add_get('/api/getrawtransaction', api.transaction)
    app.router.add_get('/api/getrawtransaction/{id}', api.transaction)
    app.router.add_get('/api/{operation:getmininginfo|getmempoolinfo|getrawmempool|getsmartnodecount}', api.simple)
    app.router.add_get('/api/getsmartnodes', api.smartnodes)

    async def project(request):
        return json_response([{'_id':'raptoreum_info', 'project':{
            'name':'Raptoreum', 'type':'Blockchain', 'currency':'RTM', 'decimalpoints':8,
            'github':'https://github.com/Raptor3um/raptoreum', 'network':'mainnet'}}])

    async def not_indexed(request):
        raise APIError(501, 'not_indexed', 'This statistic requires a separate statistics/market-data index; it is not available from this server.')

    async def docs(request):
        return web.HTTPFound('/#/api')

    app.router.add_get('/api/project', project)
    for name in ('supply', 'marketcap', 'getrichlist'):
        app.router.add_get('/api/'+name, not_indexed)
    app.router.add_get('/api', docs)
