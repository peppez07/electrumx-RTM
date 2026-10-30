import copy

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from electrumx.explorer.service import create_app

ADDRESS = 'RVLFQj4RiMCDDrWGZXRd2ui1NKGQ9ieooz'
TIP = 'ab' * 32
TXID = 'cd' * 32
ASSET = 'ef' * 32


class Core:
    def __init__(self):
        self.calls = []
        self.info = dict(chain='main', blocks=100, headers=100,
                         bestblockhash=TIP, initialblockdownload=False,
                         verificationprogress=1)

    async def call(self, method, params=None):
        params = params or []
        self.calls.append((method, params))
        responses = {
            'getblockchaininfo': self.info,
            'getblockhash': TIP,
            'getblock': dict(hash=TIP, height=100, time=12345,
                             confirmations=1, tx=[TXID, '12' * 32]),
            'getrawtransaction': dict(txid=TXID, confirmations=1,
                                      vin=[{'coinbase': '00'}],
                                      vout=[dict(n=0, value='0.00000001',
                                                 scriptPubKey={'hex': '6a'})]),
            'getassetdetailsbyid': {'Asset_id': ASSET, 'Asset_name': 'RTM.TEST'},
            'getassetdetailsbyname': {'Asset_id': ASSET, 'Asset_name': 'RTM.TEST'},
            'listassets': {'RTM.TEST': {'Asset_id': ASSET, 'Asset_name': 'RTM.TEST'}},
            'listaddressesbyasset': {ADDRESS: '900719925474099312345'},
            'getmempoolinfo': {'size': 0, 'bytes': 0},
        }
        return copy.deepcopy(responses[method])


class Electrum:
    def __init__(self):
        self.calls = []
        self.features = {'rtm_assets': {'ready': True, 'indexed_height': 100,
                                        'indexed_tip': TIP}}

    async def call(self, method, params=None):
        self.calls.append((method, params))
        if method == 'server.features':
            return self.features
        if method.endswith('get_balance'):
            return {'confirmed': 9007199254740993, 'unconfirmed': -1}
        if method.endswith('get_history') and '.assets.' not in method:
            return [{'tx_hash': TXID, 'height': 100}, {'tx_hash': '11'*32, 'height': 99}]
        if method == 'blockchain.scripthash.listunspent':
            return [{'tx_hash': TXID, 'tx_pos': 0, 'value': 9007199254740993, 'height': 100}]
        return {'items': [{'asset_id': ASSET, 'confirmed': '12345678901234567890',
                            'unconfirmed': '0'}], 'cursor': None,
                'indexed_height': 100, 'indexed_tip': TIP}


@pytest_asyncio.fixture
async def explorer():
    core, electrum = Core(), Electrum()
    client = TestClient(TestServer(create_app(core, electrum, status_ttl=0)))
    await client.start_server()
    yield client, core, electrum
    await client.close()


@pytest.mark.asyncio
async def test_ibd_only_calls_status_and_rejects_data(explorer):
    client, core, electrum = explorer
    core.info.update(initialblockdownload=True, blocks=5)
    status = await (await client.get('/api/v1/status')).json()
    assert status['phase'] == 'core_syncing' and not status['ready']
    for path in ['/api/v1/blocks', '/api/v1/transactions/'+TXID,
                 '/api/v1/addresses/'+ADDRESS, '/api/v1/assets']:
        response = await client.get(path)
        assert response.status == 503
        assert (await response.json())['error']['code'] == 'synchronizing'
    assert all(method == 'getblockchaininfo' for method, _ in core.calls)
    assert electrum.calls == []


@pytest.mark.asyncio
async def test_non_mainnet_is_never_served(explorer):
    client, core, electrum = explorer
    core.info['chain'] = 'test'
    assert (await client.get('/api/v1/blocks')).status == 503
    assert not electrum.calls


@pytest.mark.asyncio
async def test_tip_mismatch_is_not_ready(explorer):
    client, _, electrum = explorer
    electrum.features['rtm_assets']['indexed_tip'] = '00'*32
    result = await (await client.get('/api/v1/status')).json()
    assert not result['ready'] and result['phase'] == 'index_syncing'
    assert (await client.get('/api/v1/addresses/'+ADDRESS)).status == 503


@pytest.mark.asyncio
async def test_coin_quantities_never_round_in_json(explorer):
    client, _, electrum = explorer
    result = await (await client.get('/api/v1/addresses/'+ADDRESS)).json()
    assert result['balance'] == {'confirmed': '9007199254740993', 'unconfirmed': '-1'}
    assert len(result['scripthash']) == 64
    result = await (await client.get('/api/v1/addresses/'+ADDRESS+'/utxos')).json()
    assert result['items'][0]['value'] == '9007199254740993'


@pytest.mark.asyncio
async def test_bounded_history_has_next_page(explorer):
    client, _, _ = explorer
    result = await (await client.get('/api/v1/addresses/'+ADDRESS+'/history?limit=1')).json()
    assert len(result['items']) == 1 and result['next_offset'] == 1
    result = await (await client.get('/api/v1/addresses/'+ADDRESS+'/history?limit=1&offset=1')).json()
    assert result['items'][0]['height'] == 99 and result['next_offset'] is None


@pytest.mark.asyncio
async def test_asset_precision_and_cursor_pass_through(explorer):
    client, _, electrum = explorer
    result = await (await client.get('/api/v1/addresses/'+ADDRESS+'/assets?limit=2&cursor=hello')).json()
    assert result['items'][0]['confirmed'] == '12345678901234567890'
    asset_calls = [params for method, params in electrum.calls
                   if method == 'blockchain.scripthash.assets.list']
    assert asset_calls[-1][1:] == ['hello', 2]


@pytest.mark.asyncio
async def test_block_transaction_page_and_latest_blocks(explorer):
    client, _, _ = explorer
    result = await (await client.get('/api/v1/blocks/100?limit=1')).json()
    assert result['tx'] == [TXID] and result['next_offset'] == 1 and result['tx_count'] == 2
    result = await (await client.get('/api/v1/blocks?limit=2')).json()
    assert len(result['items']) == 2 and result['next_start'] == 98


@pytest.mark.asyncio
async def test_transaction_values_converted_to_atomic_strings(explorer):
    client, _, _ = explorer
    result = await (await client.get('/api/v1/transactions/'+TXID)).json()
    assert result['vout'][0]['value_atomic'] == '1'
    assert result['coinbase'] is True


@pytest.mark.asyncio
async def test_transaction_fee_is_exact_and_previous_outputs_are_shown(explorer):
    client, core, _ = explorer
    original = core.call
    async def call(method, params=None):
        if method == 'getrawtransaction':
            if params[0] == TXID:
                return {'txid': TXID, 'vin': [{'txid': '11'*32, 'vout': 0}],
                        'vout': [{'n': 0, 'value': '90071992.54740992', 'scriptPubKey': {'hex': '6a'}}]}
            return {'vout': [{'n': 0, 'value': '90071992.54740993', 'scriptPubKey': {'addresses': [ADDRESS]}}]}
        return await original(method, params)
    core.call = call
    result = await (await client.get('/api/v1/transactions/'+TXID)).json()
    assert result['fee_atomic'] == '1'
    assert result['vin'][0]['prevout']['value_atomic'] == '9007199254740993'


@pytest.mark.asyncio
async def test_asset_directory_and_holders_are_bounded(explorer):
    client, core, _ = explorer
    result = await (await client.get('/api/v1/assets?limit=2&offset=3')).json()
    assert result['items'][0]['Asset_id'] == ASSET
    assert ('listassets', [True, 3, 3]) in core.calls
    result = await (await client.get('/api/v1/assets/'+ASSET+'/holders?limit=2')).json()
    assert result['items'][0] == {'address': ADDRESS, 'balance_atomic': '900719925474099312345'}


@pytest.mark.asyncio
@pytest.mark.parametrize('suffix', ['blocks?limit=9999', 'blocks?start=-1',
    'blocks?limit=true', 'blocks/../secrets', 'addresses/not-an-address',
    'assets/not-a-hash', 'assets?offset=-1', 'search?q='])
async def test_invalid_input_is_rejected(explorer, suffix):
    client, _, _ = explorer
    assert (await client.get('/api/v1/'+suffix)).status in (400, 404)


@pytest.mark.asyncio
async def test_search_asset_name_and_block(explorer):
    client, _, _ = explorer
    result = await (await client.get('/api/v1/search?q=RTM.TEST')).json()
    assert result['matches'][0] == {'type': 'asset', 'id': ASSET}
    result = await (await client.get('/api/v1/search?q=100')).json()
    assert result['matches'][0] == {'type': 'block', 'id': '100'}


@pytest.mark.asyncio
async def test_no_generic_rpc_or_mutating_route(explorer):
    client, _, _ = explorer
    assert (await client.post('/api/v1/rpc', json={'method': 'stop'})).status == 404
    assert (await client.post('/api/v1/transactions', json={'hex': '00'})).status in (404, 405)


@pytest.mark.asyncio
async def test_rate_limit_has_retry_after():
    client = TestClient(TestServer(create_app(Core(), Electrum(), rate_limit=2)))
    await client.start_server()
    try:
        for _ in range(2):
            assert (await client.get('/api/v1/status')).status == 200
        response = await client.get('/api/v1/status')
        assert response.status == 429 and response.headers['Retry-After']
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_cached_ready_cannot_allow_coin_reads_after_sync_regression():
    core, electrum = Core(), Electrum()
    async with TestClient(TestServer(create_app(core, electrum, status_ttl=60))) as client:
        assert (await (await client.get('/api/v1/status')).json())['ready']
        core.info['initialblockdownload'] = True
        electrum.features['rtm_assets']['ready'] = False
        response = await client.get('/api/v1/addresses/'+ADDRESS)
        assert response.status == 503
        assert all(method == 'server.features' for method, _ in electrum.calls)


@pytest.mark.asyncio
async def test_native_balance_is_discarded_if_tip_changes_during_read(explorer):
    client, core, electrum = explorer
    original = electrum.call
    async def change_tip(method, params=None):
        result = await original(method, params)
        if method.endswith('get_balance'):
            core.info.update(bestblockhash='12'*32, blocks=101, headers=101)
            electrum.features['rtm_assets'].update(indexed_tip='12'*32, indexed_height=101)
        return result
    electrum.call = change_tip
    response = await client.get('/api/v1/addresses/'+ADDRESS)
    assert response.status == 503
    assert 'balance' not in await response.json()


@pytest.mark.asyncio
async def test_pending_history_fees_are_atomic_strings(explorer):
    client, _, electrum = explorer
    original = electrum.call
    async def pending(method, params=None):
        if method == 'blockchain.scripthash.get_history':
            return [{'tx_hash':TXID, 'height':0, 'fee':9007199254740993}]
        return await original(method, params)
    electrum.call = pending
    result = await (await client.get('/api/v1/addresses/'+ADDRESS+'/history')).json()
    assert result['items'][0]['fee'] == '9007199254740993'


@pytest.mark.asyncio
async def test_core_disconnect_keeps_explicit_last_known_progress_but_blocks_financial_data(explorer):
    from electrumx.explorer.clients import BackendError
    client,core,_=explorer
    core.info.update(initialblockdownload=True,blocks=55,headers=100,verificationprogress=0.29)
    initial=await (await client.get('/api/v1/status')).json()
    async def unavailable(*args):raise BackendError()
    core.call=unavailable
    result=await (await client.get('/api/v1/status')).json()
    assert result['phase'] == 'core_unavailable' and result['ready'] is False
    assert result['core']['blocks'] == 55 and result['core']['verificationprogress'] == 0.29
    assert result['core_stale'] is True
    assert result['core_checked_at'] == initial['core_checked_at']
    assert (await client.get('/api/v1/addresses/'+ADDRESS)).status == 503


@pytest.mark.asyncio
async def test_dashboard_reuses_bounded_public_work_and_invalidates_at_new_tip(explorer):
    client,core,electrum=explorer
    response=await client.get('/api/v1/overview?limit=2')
    assert response.status == 200
    result=await response.json()
    assert len(result['blocks']) == 2 and len(result['transactions']) == 2
    assert result['indexed_tip'] == TIP
    count=len([method for method,_ in core.calls if method=='getrawtransaction'])
    await client.get('/api/v1/overview?limit=2')
    assert len([method for method,_ in core.calls if method=='getrawtransaction']) == count
    core.info.update(blocks=101,headers=101,bestblockhash='12'*32)
    electrum.features['rtm_assets'].update(indexed_height=101,indexed_tip='12'*32)
    await client.get('/api/v1/overview?limit=2')
    assert len([method for method,_ in core.calls if method=='getrawtransaction']) > count


@pytest.mark.asyncio
async def test_static_health_endpoint_requires_no_core_calls(explorer):
    client,core,electrum=explorer
    result=await (await client.get('/api/v1/health')).json()
    assert result == {'service':'electrumx-rtm-explorer','version':1}
    assert core.calls == [] and electrum.calls == []

@pytest.mark.asyncio
async def test_http_utxos_preserve_future_eligibility_and_core_context(explorer):
    from tests.explorer.test_api import ADDRESS,ASSET
    client,_,electrum=explorer
    previous=electrum.call
    async def call(method,params=None):
        if method=='blockchain.scripthash.assets.listunspent':
            return {'items':[{'asset_id':ASSET,'amount':'100000000','spendable':False,
                'spendable_reason':'future_locked','future':{'maturity':5,'lock_time':-1,'confirmed_time':1000}}],
                'cursor':None,'spendability_context':{'height':100,'tip':'ab'*32,'adjusted_time':1000,'spend_height':101}}
        return await previous(method,params)
    electrum.call=call
    data=await (await client.get('/api/v1/addresses/'+ADDRESS+'/assets/'+ASSET+'/utxos')).json()
    assert data['items'][0]['spendable'] is False
    assert data['items'][0]['future']['maturity']==5
    assert data['spendability_context']['spend_height']==101

@pytest.mark.asyncio
async def test_future_capability_tracks_connected_index_support(explorer):
    client,_,electrum=explorer
    first=await (await client.get('/api/v1/wallet/capabilities')).json()
    assert first['future_outputs_selectable'] is False
    electrum.features['rtm_assets']['future_outputs_selectable']=True
    updated=await (await client.get('/api/v1/wallet/capabilities')).json()
    assert updated['future_outputs_selectable'] is True
