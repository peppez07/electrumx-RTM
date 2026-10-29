'''Synthetic root/subasset lifecycle, isolation and rollback; no live daemon.'''

import hashlib
import json

import pytest

from electrumx.lib.coins import Raptoreum
from electrumx.lib.hash import hash_to_hex_str
from electrumx.lib.tx import TxInput
from electrumx.server.raptoreum_mempool import RaptoreumMemPool
from tests.lib.test_raptoreum_assets import (
    ASSET, P2PKH, asset_script, new_asset_payload, update_asset_payload, raw_tx, varbytes)
from tests.server.test_raptoreum_index import AssetIndex, MemoryStore, commit, tx
from tests.server.test_raptoreum_mempool import API
from tests.server.test_raptoreum_requests import routed_session
from tests.server.test_raptoreum_protocol import SCRIPTHASH
from aiorpcx import Request

CHILD = '34'*32


def sub_script(amount, **kwargs):
    return asset_script(amount, **kwargs).replace(ASSET.encode(), CHILD.encode())


def mint(asset, amount):
    payload = b'\x01\0'+varbytes(asset.encode())+b'\x01\0'+bytes(32)+varbytes(b'offline-signature')
    script = asset_script(amount) if asset == ASSET else sub_script(amount)
    return tx(raw_tx(10,payload,[(0,script)]))


def test_subasset_mint_transfer_change_update_and_rollback_preserve_root():
    store = MemoryStore()
    index = AssetIndex(store)
    root_hash, child_hash = bytes.fromhex(ASSET)[::-1], bytes.fromhex(CHILD)[::-1]
    root = tx(raw_tx(8,new_asset_payload()))
    child = tx(raw_tx(8,new_asset_payload(root=False).replace(varbytes(b'EXAMPLE'),varbytes(b'CHILD'),1)))
    index.apply_block([(root,root_hash),(child,child_hash)],1,0)
    commit(index)
    assert index.metadata(CHILD)['name'] == 'EXAMPLE|CHILD'
    assert index.metadata(CHILD)['root_id'] == ASSET
    assert index.metadata(ASSET)['name'] == 'EXAMPLE'
    initial = dict(store.data)

    root_mint, child_mint = b'r'*32, b'c'*32
    index.apply_block([(mint(ASSET,100000000),root_mint),
                       (mint(CHILD,500000000),child_mint)],2,2)
    commit(index)
    assert index.metadata(ASSET)['supply'] == 100000000
    assert index.metadata(CHILD)['supply'] == 500000000
    before_transfer = dict(store.data)
    root_before = index.metadata(ASSET)
    recipient = bytes.fromhex('76a914'+'77'*20+'88ac')
    send = tx(raw_tx(outputs=[(0,sub_script(200000000,recipient=recipient)),
                              (0,sub_script(300000000))]))
    send = send._replace(inputs=[TxInput(child_mint,0,b'offline-signature',0xffffffff)])
    update = tx(raw_tx(9,update_asset_payload().replace(ASSET.encode(),CHILD.encode())))
    index.apply_block([(send,b's'*32),(update,b'u'*32)],3,4)
    commit(index)
    assert index.lookup(child_mint,0) is None
    assert index.lookup(b's'*32,0)['asset_id'] == CHILD
    assert index.lookup(b's'*32,1)['amount'] == 300000000
    assert index.lookup(root_mint,0)['asset_id'] == ASSET
    assert index.metadata(CHILD)['name'] == 'EXAMPLE|CHILD'
    assert index.metadata(CHILD)['owner'] == '66'*20
    assert index.metadata(CHILD)['supply'] == 500000000
    assert index.metadata(ASSET) == root_before
    index.rollback(3)
    commit(index)
    assert store.data == before_transfer
    index.rollback(2)
    commit(index)
    assert store.data == initial


@pytest.mark.asyncio
async def test_subasset_pending_transfer_remains_separate_and_evicts_cleanly():
    previous_hash = b'p'*32
    hashX = Raptoreum.hashX_from_script(P2PKH)
    previous = {'asset_id':CHILD,'amount':500000000,'hashX':hashX.hex(),
                'tx_hash':hash_to_hex_str(previous_hash),'tx_pos':0}
    transfer = tx(raw_tx(outputs=[(0,sub_script(500000000))]))._replace(
        inputs=[TxInput(previous_hash,0,b'offline-signature',0xffffffff),
                TxInput(previous_hash,1,b'offline-signature',0xffffffff)])
    binary = transfer.serialize()
    transfer_hash = hashlib.sha256(hashlib.sha256(binary).digest()).digest()
    api = API({hash_to_hex_str(transfer_hash):binary}, {(previous_hash,0):previous},
              {(previous_hash,0):(hashX,0),(previous_hash,1):(hashX,10000)})
    pool = RaptoreumMemPool(Raptoreum,api)
    await pool._process_mempool({transfer_hash},set(),100)
    assert pool.asset_ready
    assert all(record['asset_id'] == CHILD for record in pool.asset_outputs.values())
    assert all(aid == CHILD for _,aid in pool.asset_history)
    assert pool.txs[transfer_hash].fee == 10000
    await pool._process_mempool(set(),set(),100)
    assert pool.asset_outputs == {} and pool.asset_history == {}


@pytest.mark.asyncio
async def test_subasset_wire_balance_and_metadata_keep_child_identity():
    session = routed_session()
    view = session.db.view
    child_output = dict(view['outputs'][0],asset_id=CHILD,amount=200000000)
    view['outputs'].append(child_output)
    view['asset_ids'].append(CHILD)
    view['metadata'][CHILD] = {'asset_id':CHILD,'is_root':False,'root_id':ASSET,
                               'name':'EXAMPLE|CHILD','supply':200000000}
    metadata = await session.handle_request(Request('blockchain.asset.get_metadata',[CHILD]))
    assert metadata['metadata']['root_id'] == ASSET
    assert metadata['metadata']['name'] == 'EXAMPLE|CHILD'
    balances = await session.handle_request(Request('blockchain.scripthash.assets.get_balance',[SCRIPTHASH]))
    assert balances['assets'][CHILD]['confirmed'] == '200000000'
    assert balances['assets'][ASSET]['confirmed'] == '9007199254740993'
