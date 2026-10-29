'''Exercise each wallet request through dispatch using recorded local responses.

No Core connection is made, and the broadcast test never sends a transaction.
Live mainnet reconciliation is a separate step after Core synchronization.
'''

import copy
import logging
from collections import defaultdict
from types import SimpleNamespace

import pytest
from aiorpcx import Request, RPCError

from electrumx.lib.coins import Raptoreum
from tests.lib.test_raptoreum_assets import ASSET
from tests.server.test_raptoreum_protocol import SCRIPTHASH, session


CASES = [
    ('blockchain.asset.get_metadata', [ASSET]),
    ('blockchain.scripthash.assets.get_balance', [SCRIPTHASH]),
    ('blockchain.scripthash.assets.list', [SCRIPTHASH]),
    ('blockchain.scripthash.assets.listunspent', [SCRIPTHASH, ASSET]),
    ('blockchain.scripthash.assets.get_history', [SCRIPTHASH, ASSET]),
    ('blockchain.scripthash.assets.subscribe', [SCRIPTHASH]),
    ('blockchain.scripthash.assets.unsubscribe', [SCRIPTHASH]),
    ('blockchain.scripthash.get_balance', [SCRIPTHASH]),
    ('blockchain.scripthash.get_history', [SCRIPTHASH]),
    ('blockchain.scripthash.get_mempool', [SCRIPTHASH]),
    ('blockchain.scripthash.listunspent', [SCRIPTHASH]),
    ('blockchain.scripthash.subscribe', [SCRIPTHASH]),
    ('blockchain.scripthash.unsubscribe', [SCRIPTHASH]),
    ('blockchain.transaction.get', ['dd'*32]),
    ('blockchain.transaction.get_merkle', ['dd'*32, 80]),
    ('blockchain.transaction.id_from_pos', [80, 0]),
    ('blockchain.transaction.broadcast', ['01000000000000000000']),
    ('blockchain.block.header', [80]),
    ('server.features', []),
    ('server.ping', []),
]


def routed_session():
    s = session()
    s.coin = Raptoreum
    s.env = SimpleNamespace(drop_client_unknown=False, report_services=[], coin=Raptoreum)
    s.client = 'pytest'
    s.txs_sent = 0
    s.hashX_subs = {}
    s.mempool_statuses = {}
    s.logger = logging.getLogger(__name__)
    s.db.db_height = 100
    s.db.db_tip = bytes.fromhex('aa'*32)
    s.db.asset_metadata = lambda aid: {'height': 100, 'tip': 'aa'*32,
                                      'metadata': copy.deepcopy(s.db.view['metadata'].get(aid))}

    async def empty(*args):
        return []

    async def zero(*args):
        return 0

    async def no_spends(*args):
        return set()

    async def history(*args):
        return [(bytes.fromhex('dd'*32), 80)], 1

    async def merkle(*args):
        return ['ee'*32], 0, 1

    async def hashes(*args):
        return [bytes.fromhex('dd'*32)], 1

    async def header(*args):
        return bytes(80)

    async def broadcast(raw):
        assert raw == '01000000000000000000'
        s.recorded_broadcast = raw
        return 'dd'*32

    async def daemon(method, *args):
        assert method == 'getrawtransaction'
        assert args == ('dd'*32, False)
        return '01000000000000000000'

    s.mempool.balance_delta = zero
    s.mempool.unordered_UTXOs = empty
    s.mempool.potential_spends = no_spends
    s.mempool.transaction_summaries = empty
    s.session_mgr = SimpleNamespace(_method_counts=defaultdict(int), limited_history=history,
        merkle_branch_for_tx_hash=merkle, tx_hashes_at_blockheight=hashes, raw_header=header,
        broadcast_transaction=broadcast)
    s.daemon_request = daemon
    s.set_request_handlers((1, 4, 2))
    return s


@pytest.mark.asyncio
@pytest.mark.parametrize('method,args', CASES, ids=[method for method, _ in CASES])
async def test_each_request_dispatches_and_returns_expected_shape(method, args):
    s = routed_session()
    if method == 'blockchain.scripthash.assets.unsubscribe':
        await s.assets_subscribe(SCRIPTHASH)
    if method == 'blockchain.scripthash.unsubscribe':
        await s.scripthash_subscribe(SCRIPTHASH)
    result = await s.handle_request(Request(method, args))
    assert s.session_mgr._method_counts[method] == 1
    if method == 'blockchain.asset.get_metadata':
        assert result['metadata']['asset_id'] == ASSET
        assert result['metadata']['supply'] == str(2**53+1)
    elif method == 'blockchain.scripthash.assets.get_balance':
        assert result['assets'][ASSET]['confirmed'] == str(2**53+1)
    elif method in ('blockchain.scripthash.assets.list',
                    'blockchain.scripthash.assets.listunspent',
                    'blockchain.scripthash.assets.get_history'):
        assert isinstance(result['items'], list)
        assert 'cursor' in result
        assert result['indexed_height'] == 100
    elif method.endswith('.unsubscribe'):
        assert result is True
    elif method.endswith('.subscribe'):
        assert isinstance(result, str) and len(result) == 64
    elif method == 'blockchain.scripthash.get_balance':
        assert result == {'confirmed': 100000, 'unconfirmed': 0}
    elif method == 'blockchain.scripthash.get_history':
        assert result == [{'tx_hash': 'dd'*32, 'height': 80}]
    elif method == 'blockchain.scripthash.get_mempool':
        assert result == []
    elif method == 'blockchain.scripthash.listunspent':
        assert len(result) == 1 and result[0]['value'] == 100000
    elif method == 'blockchain.transaction.get':
        assert result == '01000000000000000000'
    elif method == 'blockchain.transaction.get_merkle':
        assert result == {'block_height': 80, 'merkle': ['ee'*32], 'pos': 0}
    elif method in ('blockchain.transaction.id_from_pos', 'blockchain.transaction.broadcast'):
        assert result == 'dd'*32
        if method.endswith('.broadcast'):
            assert s.recorded_broadcast and s.txs_sent == 1
    elif method == 'blockchain.block.header':
        assert result == '00'*80
    elif method == 'server.features':
        assert result['genesis_hash'] == Raptoreum.GENESIS_HASH
        assert result['rtm_assets']['version'] == 1
        assert result['rtm_assets']['ready'] is True
    elif method == 'server.ping':
        assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize('method,args', CASES[:6], ids=[method for method, _ in CASES[:6]])
async def test_asset_reads_and_subscription_wait_for_core_sync(method, args):
    s = routed_session()
    s.mempool.asset_ready = False
    with pytest.raises(RPCError) as error:
        await s.handle_request(Request(method, args))
    assert error.value.code == -32001


@pytest.mark.asyncio
async def test_features_reports_assets_not_ready_during_core_sync():
    s = routed_session()
    s.mempool.asset_ready = False
    result = await s.handle_request(Request('server.features', []))
    assert result['rtm_assets']['ready'] is False
