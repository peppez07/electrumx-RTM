import asyncio
import copy
import hashlib
from types import SimpleNamespace

import pytest
from aiorpcx import RPCError

from electrumx.server.db import UTXO
from electrumx.server.raptoreum_session import RaptoreumElectrumX
from tests.lib.test_raptoreum_assets import ASSET, P2PKH

HASHX = hashlib.sha256(P2PKH).digest()[:11]
SCRIPTHASH = hashlib.sha256(P2PKH).digest()[::-1].hex()


class FakeDB:
    def __init__(self):
        self.view = {'height': 100, 'tip': 'aa'*32, 'outputs': [
            {'asset_id': ASSET, 'amount': 2**53+1, 'hashX': HASHX.hex(),
             'tx_hash': 'bb'*32, 'tx_pos': 0, 'height': 80, 'value': 0,
             'is_unique': False, 'unique_id': None, 'unique_end': None,
             'source_type': 0, 'script': '00', 'spendable': True}],
            'asset_ids': [ASSET, '99'*32], 'metadata': {
                ASSET: {'asset_id': ASSET, 'name': 'EXAMPLE', 'supply': 2**53+1,
                        'decimal_point': 8, 'last_tx_hash': '01'*32},
                '99'*32: {'asset_id': '99'*32, 'last_tx_hash': '02'*32}},
            'revisions': {ASSET: '03'*32}, 'history': [], 'history_more': False}

    def asset_view(self, hashX, request=None):
        assert hashX == HASHX
        result = copy.deepcopy(self.view)
        result.update(more=False, last='')
        if request and request['mode'] == 'discovery':
            ids = sorted(set(result['asset_ids']) | request['extra_ids'])
            ids = [aid for aid in ids if aid > request['after']]
            result['asset_ids'] = ids[:request['limit']]
            result['more'] = len(ids) > request['limit']
            result['last'] = result['asset_ids'][-1] if result['asset_ids'] else ''
        if request and request['mode'] == 'outputs':
            result['outputs'] = [o for o in result['outputs']
                                 if (o['tx_hash'], o['tx_pos']) not in request['spends']]
        return result

    def asset_outpoints(self, utxos):
        return {(bytes.fromhex('bb'*32), 0)}

    async def all_utxos(self, hashX):
        return [UTXO(1, 0, bytes.fromhex('bb'*32), 80, 0),
                UTXO(2, 0, bytes.fromhex('cc'*32), 80, 100000)]


def session():
    result = object.__new__(RaptoreumElectrumX)
    result.db = FakeDB()
    result.mempool = SimpleNamespace(lock=asyncio.Lock(), asset_ready=True,
        asset_tip=(100, 'aa'*32), asset_generation=1, asset_outputs={},
        asset_inputs=[], asset_spends=set(), asset_events=[], asset_history={})
    result.bump_cost = lambda value: None
    result.asset_subs = {}
    return result


@pytest.mark.asyncio
async def test_exact_balances_and_fully_spent_asset_discovery():
    s = session()
    response = await s.assets_get_balance(SCRIPTHASH)
    assert response['assets'][ASSET]['confirmed'] == str(2**53+1)
    listing = await s.assets_list(SCRIPTHASH, limit=1)
    assert len(listing['items']) == 1
    assert listing['cursor']
    page = await s.assets_list(SCRIPTHASH, listing['cursor'], 1)
    assert len(page['items']) == 1
    assert {listing['items'][0]['asset_id'], page['items'][0]['asset_id']} == {ASSET, '99'*32}
    s.mempool.asset_generation += 1
    with pytest.raises(RPCError):
        await s.assets_list(SCRIPTHASH, listing['cursor'], 1)


@pytest.mark.asyncio
async def test_native_listunspent_excludes_asset_outpoints():
    s = session()
    async def unordered(hashX):
        return []
    async def spends(hashX):
        return set()
    s.mempool.unordered_UTXOs = unordered
    s.mempool.potential_spends = spends
    result = await s.hashX_listunspent(HASHX)
    assert len(result) == 1
    assert result[0]['value'] == 100000


@pytest.mark.asyncio
async def test_pending_spend_and_future_outputs():
    s = session()
    original = s.db.view['outputs'][0]
    pending = dict(original, tx_hash='dd'*32, amount=100000000,
                   source_type=7, spendable=False, height=0)
    s.mempool.asset_spends = {('bb'*32, 0)}
    s.mempool.asset_inputs = [original]
    s.mempool.asset_outputs = {(b'd'*32, 0): pending}
    result = await s.assets_listunspent(SCRIPTHASH, ASSET)
    assert len(result['items']) == 1
    assert result['items'][0]['spendable'] is False
    balance = await s.assets_get_balance(SCRIPTHASH)
    assert balance['assets'][ASSET]['unconfirmed'] == str(100000000-(2**53+1))


@pytest.mark.asyncio
async def test_consistent_tip_required_and_limits_validated():
    s = session()
    with pytest.raises(RPCError):
        await s.assets_list(SCRIPTHASH, limit=True)
    with pytest.raises(RPCError):
        await s.assets_listunspent(SCRIPTHASH, 'bad-id')
    s.mempool.asset_tip = (101, 'aa'*32)
    with pytest.raises(RPCError):
        await s.assets_get_balance(SCRIPTHASH)


@pytest.mark.asyncio
async def test_metadata_only_changes_invalidate_holder_status():
    s = session()
    first = await s.asset_status(HASHX)
    s.db.view['metadata'][ASSET]['last_tx_hash'] = '04'*32
    assert await s.asset_status(HASHX) != first
    first = await s.asset_status(HASHX)
    s.db.view['metadata']['99'*32]['last_tx_hash'] = '05'*32
    assert await s.asset_status(HASHX) == first


@pytest.mark.asyncio
async def test_native_refresh_race_never_leaks_zero_value_asset_output():
    s = session()
    s.db.asset_outpoints = lambda utxos: set()  # prevout vanished after first query
    async def empty(hashX):
        return []
    async def no_spends(hashX):
        return set()
    s.mempool.unordered_UTXOs = empty
    s.mempool.potential_spends = no_spends
    assert [o['value'] for o in await s.hashX_listunspent(HASHX)] == [100000]


@pytest.mark.asyncio
async def test_cursor_is_authenticated_and_malformed_json_is_bad_request():
    import base64
    import json
    s = session()
    page = await s.assets_list(SCRIPTHASH, limit=1)
    token = json.loads(base64.urlsafe_b64decode(page['cursor']))
    token['position'] = 'ff'*32
    altered = base64.urlsafe_b64encode(json.dumps(token).encode()).decode()
    with pytest.raises(RPCError):
        await s.assets_list(SCRIPTHASH, altered, 1)
    with pytest.raises(RPCError):
        await s.assets_list(SCRIPTHASH, base64.urlsafe_b64encode(b'null').decode(), 1)
