import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from electrumx.lib.tx import TxInput
from electrumx.lib.tx_raptoreum import DeserializerRaptoreum
from electrumx.server.raptoreum_db import AssetIndex, AssetIndexError
from tests.lib.test_raptoreum_assets import (
    ASSET, P2PKH, asset_script, new_asset_payload, update_asset_payload, raw_tx, varbytes,
)


class MemoryStore:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def iterator(self, prefix=b''):
        return iter(sorted((k, v) for k, v in self.data.items() if k.startswith(prefix)))

    def iterator_from(self, prefix, start):
        return ((k, v) for k, v in self.iterator(prefix) if k >= (start or prefix))

    @contextmanager
    def write_batch(self):
        changes = {}
        yield SimpleNamespace(put=lambda k, v: changes.__setitem__(k, v),
                              delete=lambda k: changes.__setitem__(k, None))
        for k, v in changes.items():
            if v is None:
                self.data.pop(k, None)
            else:
                self.data[k] = v


def commit(index):
    with index.storage.write_batch() as batch:
        index.write_batch(batch)
    index.committed()


def tx(raw):
    return DeserializerRaptoreum(raw).read_tx()


def test_mint_send_same_block_and_exact_rollback():
    store = MemoryStore()
    index = AssetIndex(store)
    create_hash = bytes.fromhex(ASSET)[::-1]
    mint_hash = b'\x55' * 32
    send_hash = b'\x66' * 32
    create = tx(raw_tx(8, new_asset_payload()))
    import struct
    mint = tx(raw_tx(10, b'\x01\0' + varbytes(ASSET.encode())
                    + b'\x01\0' + bytes(32) + varbytes(b'sig'),
                    [(0, asset_script(500000000))]))
    index.apply_block([(create, create_hash), (mint, mint_hash)], 1, 0)
    commit(index)
    before = dict(store.data)
    assert index.metadata(ASSET)['supply'] == 500000000
    recipient = bytes.fromhex('76a914' + '22'*20 + '88ac')
    send = tx(raw_tx(outputs=[(0, asset_script(200000000, recipient=recipient)),
                             (0, asset_script(300000000))]))
    send = send._replace(inputs=[TxInput(mint_hash, 0, b'', 0xffffffff)])
    child = tx(raw_tx(outputs=[(0, asset_script(200000000))]))
    child = child._replace(inputs=[TxInput(send_hash, 0, b'', 0xffffffff)])
    index.apply_block([(send, send_hash), (child, b'\x77'*32)], 2, 2)
    commit(index)
    assert index.lookup(mint_hash, 0) is None
    assert index.lookup(send_hash, 0) is None
    assert index.lookup(send_hash, 1)['amount'] == 300000000
    index.rollback(2)
    commit(index)
    assert store.data == before


def test_commit_failure_retains_pending_changes():
    index = AssetIndex(MemoryStore())
    index.apply_block([(tx(raw_tx(outputs=[(0, asset_script())])), b'a'*32)], 1, 0)
    pending = dict(index.pending)
    with pytest.raises(RuntimeError):
        with index.storage.write_batch() as batch:
            index.write_batch(batch)
            raise RuntimeError('injected disk failure')
    assert index.pending == pending
    assert index.storage.data == {}
    commit(index)
    assert index.lookup(b'a'*32, 0)['amount'] == 123000000


def test_metadata_version_and_missing_undo_fail_closed():
    index = AssetIndex(MemoryStore())
    with pytest.raises(AssetIndexError):
        index.rollback(999)
    with pytest.raises(ValueError):
        bad = tx(raw_tx(8, new_asset_payload().replace(b'\x01\0', b'\x02\0', 1)))
        index.apply_block([(bad, b'a'*32)], 1, 0)


def test_noncanonical_output_remains_visible_and_nonselectable():
    index = AssetIndex(MemoryStore())
    script = asset_script()
    script = script[:27] + b'\x01' + script[28:]
    index.apply_block([(tx(raw_tx(outputs=[(0, script)])), b'x'*32)], 1, 0)
    assert index.lookup(b'x'*32, 0)['spendable'] is False


def test_restart_restores_asset_index():
    store = MemoryStore()
    index = AssetIndex(store)
    index.apply_block([(tx(raw_tx(outputs=[(0, asset_script())])), b'x'*32)], 1, 0)
    commit(index)
    restarted = AssetIndex(store)
    assert restarted.lookup(b'x'*32, 0)['amount'] == 123000000
    restarted.rollback(1)
    commit(restarted)
    assert restarted.lookup(b'x'*32, 0) is None


def test_metadata_update_keeps_supply_and_reverses_owner_change():
    index = AssetIndex(MemoryStore())
    index.apply_block([(tx(raw_tx(8, new_asset_payload())), bytes.fromhex(ASSET)[::-1])], 1, 0)
    commit(index)
    before = dict(index.storage.data)
    index.apply_block([(tx(raw_tx(9, update_asset_payload())), b'u'*32)], 2, 1)
    assert index.metadata(ASSET)['owner'] == '66'*20
    assert index.metadata(ASSET)['supply'] == 0
    assert index.metadata(ASSET)['decimal_point'] == 2
    commit(index)
    index.rollback(2)
    commit(index)
    assert index.storage.data == before


def test_large_holder_uses_summary_and_paged_output_seek():
    import threading
    from electrumx.lib.tx import TxOutput
    from electrumx.server.raptoreum_db import RaptoreumDB
    from electrumx.lib.coins import Raptoreum
    store = MemoryStore()
    index = AssetIndex(store)
    many = tx(raw_tx())._replace(outputs=[TxOutput(0, asset_script(100000000))] * 10001)
    index.apply_block([(many, b'x'*32)], 1, 0)
    commit(index)
    db = object.__new__(RaptoreumDB)
    db.utxo_db, db.assets, db.asset_lock = store, index, threading.RLock()
    db.db_height, db.db_tip = 1, b't'*32
    hashX = Raptoreum.hashX_from_script(P2PKH)
    assert db.asset_view(hashX)['balances'][ASSET]['confirmed'] == 10001*100000000
    page = db.asset_view(hashX, {'mode': 'outputs', 'asset_id': ASSET, 'after': '',
                               'limit': 1, 'spends': set()})
    assert len(page['outputs']) == 1
    assert page['more']
    second = db.asset_view(hashX, {'mode': 'outputs', 'asset_id': ASSET,
                                 'after': page['last'], 'limit': 1, 'spends': set()})
    assert second['outputs'][0]['tx_pos'] != page['outputs'][0]['tx_pos']
