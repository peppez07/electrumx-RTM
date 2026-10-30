import copy

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from electrumx.explorer.service import create_app
from tests.explorer.test_api import ADDRESS, ASSET, TXID, TIP, Core, Electrum


class ReferenceCore(Core):
    async def call(self, method, params=None):
        if method in ('getblockstats', 'getmininginfo', 'getrawmempool', 'smartnode', 'smartnodelist'):
            self.calls.append((method, params or []))
            return copy.deepcopy({
                'getblockstats': {'height':100, 'avgfee':1, 'totalfee':9007199254740993},
                'getmininginfo': {'chain':'main', 'blocks':100, 'pooledtx':1},
                'getrawmempool':[TXID],
                'smartnode':{'total':10, 'enabled':9},
                'smartnodelist':{'outpoint':{'proTxHash':TIP, 'address':'127.0.0.1:10226',
                                           'status':'ENABLED'}},
            }[method])
        result = await super().call(method, params)
        if method == 'getblock' and params[1] == 2:
            result['tx'] = [{'txid':TXID, 'hex':'03000000'}]
        if method == 'getrawtransaction':
            result['hex'] = '03000000'
        if method == 'getassetdetailsbyid':
            result.update(Isunique=False, Decimalpoint=8, owner=ADDRESS,
                          Circulating_supply='12345678901234567890')
        return result


@pytest_asyncio.fixture
async def reference():
    core, electrum = ReferenceCore(), Electrum()
    async with TestClient(TestServer(create_app(core, electrum, status_ttl=0))) as client:
        yield client, core, electrum


@pytest.mark.asyncio
async def test_legacy_count_plain_and_json_during_initial_sync(reference):
    client, core, electrum = reference
    core.info['initialblockdownload'] = True
    response = await client.get('/api/getblockcount')
    assert response.status == 200 and await response.text() == '100'
    result = await (await client.get('/api/getblockcount?json=true')).json()
    assert result == {'success':'true', 'blockcount':100}
    result = await (await client.get('/api/getblockchaininfo')).json()
    assert result['blockcount']['initialblockdownload'] is True
    assert electrum.calls == []


@pytest.mark.asyncio
async def test_reference_wrappers_preserve_raw_transactions_and_full_block(reference):
    client, core, _ = reference
    result = await (await client.get('/api/getblock?height=100')).json()
    assert result['success'] == 'true'
    assert result['getblock']['tx'][0]['txid'] == TXID
    assert ('getblock', [TIP, 2]) in core.calls
    for path in ['/api/gettransaction/'+TXID, '/api/getrawtransaction?txid='+TXID]:
        result = await (await client.get(path)).json()
        assert result['success'] is True
        assert result['transaction']['hex'] == '03000000'


@pytest.mark.asyncio
async def test_reference_balance_and_old_ampersand_query_are_exact(reference):
    client, _, _ = reference
    assert await (await client.get('/api/getaddressbalance/'+ADDRESS)).text() == '90071992.54740993'
    for path in ['/api/getaddressbalance/'+ADDRESS+'?json=true',
                 '/api/getaddressbalance/'+ADDRESS+'&json=true',
                 '/api/getaddressbalance?address='+ADDRESS+'&json=true']:
        result = await (await client.get(path)).json()
        assert result == {'success':'true', 'address':ADDRESS,
                          'balanceRTM':'90071992.54740993', 'balanceSatoshis':'9007199254740993'}


@pytest.mark.asyncio
@pytest.mark.parametrize('path,key', [
    ('getblockstats?height=100','getblockstats'), ('getmininginfo','mininginfo'),
    ('getmempoolinfo','getmempoolinfo'), ('getrawmempool','getmempoolinfo'),
    ('getsmartnodecount','getsamrtnodecount')])
async def test_observed_reference_envelopes(reference, path, key):
    client, _, _ = reference
    response = await client.get('/api/'+path)
    assert response.status == 200
    result = await response.json()
    assert result['success'] == 'true' and key in result
    if path.startswith('getblockstats'):
        assert result[key]['avgfee'] == '1'
        assert result[key]['totalfee'] == '9007199254740993'


@pytest.mark.asyncio
async def test_reference_asset_wrapper_contains_core_metadata(reference):
    client, _, _ = reference
    result = await (await client.get('/api/asset/'+ASSET)).json()
    assert result['success'] is True
    assert result['asset']['_id'] == ASSET
    assert result['asset']['name'] == 'RTM.TEST'
    assert result['asset']['data']['ownerAddress'] == ADDRESS
    assert result['asset']['amount'] == '12345678901234567890'


@pytest.mark.asyncio
async def test_smartnodes_report_chain_fields_without_invented_geography(reference):
    client, _, _ = reference
    result = await (await client.get('/api/getsmartnodes')).json()
    assert result[0]['proTxHash'] == TIP
    assert 'country' not in result[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['supply', 'marketcap', 'getrichlist'])
async def test_database_only_statistics_are_explicitly_unavailable(reference, path):
    client, _, _ = reference
    response = await client.get('/api/'+path)
    assert response.status == 501
    assert (await response.json())['error']['code'] == 'not_indexed'


@pytest.mark.asyncio
async def test_known_compat_data_stays_blocked_while_core_syncs(reference):
    client, core, electrum = reference
    core.info['initialblockdownload'] = True
    for path in ['getrawmempool', 'asset/'+ASSET, 'getaddressbalance/'+ADDRESS,
                 'getblock?height=100', 'getmininginfo']:
        assert (await client.get('/api/'+path)).status == 503
    assert all(method == 'getblockchaininfo' for method, _ in core.calls)
    assert electrum.calls == []


@pytest.mark.asyncio
async def test_compat_large_lists_are_never_silently_truncated(reference):
    client, core, _ = reference
    original = core.call
    async def too_many(method, params=None):
        return [TXID]*5001 if method == 'getrawmempool' else await original(method, params)
    core.call = too_many
    response = await client.get('/api/getrawmempool')
    assert response.status == 503
    assert 'error' in await response.json()


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['getblock', 'getblock?height=-1',
    'getblock?height=1&hash='+TIP, 'getblockstats?hash=bad',
    'getaddressbalance/nope', 'getrawtransaction?txid=bad'])
async def test_compat_rejects_ambiguous_or_invalid_queries(reference, path):
    client, _, _ = reference
    assert (await client.get('/api/'+path)).status == 400


@pytest.mark.asyncio
async def test_small_and_large_monetary_fields_always_use_strings(reference):
    client, core, _ = reference
    original = core.call
    async def money(method, params=None):
        result = await original(method, params)
        if method == 'getrawtransaction':
            result['vout'][0].update(valueSat=1)
            result['fee'] = 3
        elif method == 'getblock':
            result['tx'] = [{'txid':TXID,'vout':[{'valueSat':1},{'valueSat':9007199254740993}]}]
        elif method == 'getblockstats':
            result['feerate_percentiles'] = [1,2,3,4,5]
        elif method == 'getassetdetailsbyid':
            result.update(Circulating_supply=1,Distribution={'Amount':2})
        return result
    core.call = money
    transaction = await (await client.get('/api/gettransaction/'+TXID)).json()
    assert transaction['transaction']['vout'][0]['valueSat'] == '1'
    assert transaction['transaction']['fee'] == '3'
    block = await (await client.get('/api/getblock?height=100')).json()
    assert [v['valueSat'] for v in block['getblock']['tx'][0]['vout']] == ['1','9007199254740993']
    stats = await (await client.get('/api/getblockstats?height=100')).json()
    assert stats['getblockstats']['feerate_percentiles'] == ['1','2','3','4','5']
    metadata = await (await client.get('/api/v1/assets/'+ASSET)).json()
    assert metadata['Circulating_supply'] == '1'
    assert metadata['Distribution']['Amount'] == '2'

@pytest.mark.asyncio
async def test_lightweight_network_and_paginated_nodes_share_public_cache(reference):
    client,core,_=reference
    for path in ('network','smartnodes?limit=1'):
        response=await client.get('/api/v1/'+path)
        assert response.status == 200
        data=await response.json()
        assert data['snapshot_at']
    assert data['items'][0]['status'] == 'ENABLED' and data['next_offset'] is None
    calls=len([m for m,_ in core.calls if m=='smartnodelist'])
    await client.get('/api/v1/smartnodes?limit=1')
    assert len([m for m,_ in core.calls if m=='smartnodelist']) == calls
