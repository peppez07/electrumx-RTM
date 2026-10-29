from types import SimpleNamespace

import pytest

from electrumx.lib.coins import Raptoreum
from electrumx.lib.hash import hash_to_hex_str
from electrumx.lib.tx import TxInput
from electrumx.lib.tx_raptoreum import DeserializerRaptoreum
from electrumx.server.mempool import MemPoolAPI
from electrumx.server.raptoreum_mempool import RaptoreumMemPool
from tests.lib.test_raptoreum_assets import ASSET, P2PKH, asset_script, raw_tx


class API:
    def __init__(self, txs, confirmed, native):
        self.raw = txs
        self.confirmed = confirmed
        self.native = native
        self.chain_ready = True

    async def asset_chain_ready(self):
        return self.chain_ready

    def db_height(self):
        return 100

    def db_tip(self):
        return b't'*32

    async def raw_transactions(self, hashes):
        return [self.raw.get(h) for h in hashes]

    async def lookup_utxos(self, prevouts):
        return [self.native.get(p) for p in prevouts]

    async def lookup_asset_utxos(self, prevouts):
        return [self.confirmed.get(p) for p in prevouts]


MemPoolAPI.register(API)


@pytest.mark.asyncio
async def test_asset_pending_graph_and_eviction_preserve_native_fee():
    prev_hash = b'p'*32
    previous = {'asset_id': ASSET, 'amount': 500000000,
                'hashX': Raptoreum.hashX_from_script(P2PKH).hex(),
                'tx_hash': hash_to_hex_str(prev_hash), 'tx_pos': 0}
    first = DeserializerRaptoreum(raw_tx(outputs=[(0, asset_script(500000000))])).read_tx()
    first = first._replace(inputs=[TxInput(prev_hash, 0, b'', 0xffffffff),
                                  TxInput(prev_hash, 1, b'', 0xffffffff)])
    first_raw = first.serialize()
    first, first_hash = DeserializerRaptoreum(first_raw).read_tx_and_hash()
    second = first._replace(inputs=[TxInput(first_hash, 0, b'', 0xffffffff)])
    second_raw = second.serialize()
    _, second_hash = DeserializerRaptoreum(second_raw).read_tx_and_hash()
    api = API({hash_to_hex_str(first_hash): first_raw,
               hash_to_hex_str(second_hash): second_raw},
              {(prev_hash, 0): previous},
              {(prev_hash, 0): (bytes.fromhex(previous['hashX']), 0),
               (prev_hash, 1): (bytes.fromhex(previous['hashX']), 10000)})
    pool = RaptoreumMemPool(Raptoreum, api)
    touched = set()
    await pool._process_mempool({second_hash, first_hash}, touched, 100)
    assert pool.txs[first_hash].fee == 10000
    assert len(pool.asset_outputs) == 2
    assert (hash_to_hex_str(first_hash), 0) in pool.asset_spends
    assert pool.asset_ready
    generation = pool.asset_generation
    await pool._process_mempool({second_hash, first_hash}, set(), 100)
    assert pool.asset_generation == generation
    await pool._process_mempool(set(), set(), 100)
    assert pool.asset_outputs == {}
    assert pool.asset_spends == set()
    assert pool.asset_generation > generation


@pytest.mark.asyncio
async def test_core_initial_block_download_never_advertises_asset_readiness():
    api = API({}, {}, {})
    api.chain_ready = False
    pool = RaptoreumMemPool(Raptoreum, api)
    await pool._process_mempool(set(), set(), 100)
    assert not pool.asset_ready


def test_rtm_components_and_native_recipient_are_selected():
    assert Raptoreum.MEMPOOLCLS is RaptoreumMemPool
    assert Raptoreum.hashX_from_script(asset_script()) == Raptoreum.hashX_from_script(P2PKH)
    assert Raptoreum.DESERIALIZER is DeserializerRaptoreum
