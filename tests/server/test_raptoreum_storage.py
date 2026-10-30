'''Real storage lifecycle checks; run on Linux with the LevelDB extra installed.'''

import asyncio
import os
import struct
from types import SimpleNamespace

import pytest

pytest.importorskip('plyvel')

from electrumx.lib.coins import Raptoreum
from electrumx.server.db import DB
from electrumx.server.raptoreum_db import RaptoreumDB, PREFIX, SCHEMA_KEY
from electrumx.server.raptoreum_block_processor import RaptoreumBlockProcessor
from tests.lib.test_raptoreum_assets import (
    ASSET, asset_script, new_asset_payload, raw_tx, varbytes,
)


def env(path):
    return SimpleNamespace(coin=Raptoreum, db_dir=str(path), db_engine='leveldb',
                           reorg_limit=200, cache_MB=8)


def raw_block(previous, transactions, nonce=0):
    header = struct.pack('<I', 4) + previous + bytes(32) + struct.pack('<III', 1, 0, nonce)
    return header + bytes([len(transactions)]) + b''.join(transactions)


def close(db):
    if db.utxo_db:
        db.utxo_db.close()
    db.history.close_db()


@pytest.mark.asyncio
async def test_initial_history_only_flush_can_reopen(tmp_path):
    original = os.getcwd()
    db = None
    try:
        db = RaptoreumDB(env(tmp_path))
        await db.open_for_sync()
        bp = RaptoreumBlockProcessor(db.env, db, SimpleNamespace(cached_height=lambda: 1), None)
        block = raw_block(bytes(32), [])
        bp.advance_blocks([Raptoreum.block(block, 0)])
        await bp.flush(False)
        assert db.utxo_db.get(SCHEMA_KEY)
        close(db)
        db = RaptoreumDB(env(tmp_path))
        await db.open_for_sync()
        assert db.db_height == -1
    finally:
        if db:
            close(db)
        os.chdir(original)


@pytest.mark.asyncio
async def test_atomic_asset_flush_reopen_and_block_rollback(tmp_path):
    original = os.getcwd()
    db = None
    try:
        db = RaptoreumDB(env(tmp_path))
        await db.open_for_sync()
        bp = RaptoreumBlockProcessor(db.env, db, SimpleNamespace(cached_height=lambda: 2), None)
        genesis = raw_block(bytes(32), [])
        bp.advance_blocks([Raptoreum.block(genesis, 0)])
        await bp.flush(True)
        before = dict(db.utxo_db.iterator(prefix=PREFIX))
        create_raw = raw_tx(8, new_asset_payload())
        created, create_hash = Raptoreum.DESERIALIZER(create_raw).read_tx_and_hash()
        aid = create_hash[::-1].hex()
        script = asset_script(500000000).replace(ASSET.encode(), aid.encode())
        mint_payload = b'\x01\0' + varbytes(aid.encode()) + b'\x01\0' + bytes(32) + varbytes(b'sig')
        minted = raw_tx(10, mint_payload, [(0, script)])
        block = raw_block(Raptoreum.header_hash(genesis[:80]), [create_raw, minted], 1)
        bp.advance_blocks([Raptoreum.block(block, 1)])
        await bp.flush(True)
        assert db.asset_metadata(aid)['metadata']['supply'] == 500000000
        await db.open_for_serving()
        assert db.asset_metadata(aid)['metadata']['supply'] == 500000000
        async with bp.state_lock:
            bp.backup_blocks([block])
            db.flush_backup(bp.flush_data(), bp.touched)
        assert dict(db.utxo_db.iterator(prefix=PREFIX)) == before
    finally:
        if db:
            close(db)
        os.chdir(original)


@pytest.mark.asyncio
async def test_existing_coin_only_database_requires_separate_rebuild(tmp_path):
    original = os.getcwd()
    db = None
    try:
        db = DB(env(tmp_path))
        await db.open_for_sync()
        db.write_utxo_state(db.utxo_db)
        close(db)
        db = RaptoreumDB(env(tmp_path))
        with pytest.raises(DB.DBError, match='fresh DB_DIRECTORY'):
            await db.open_for_sync()
    finally:
        if db:
            close(db)
        os.chdir(original)

@pytest.mark.asyncio
async def test_future_asset_headers_reopen_and_reorg_relock_outputs(tmp_path):
    import hashlib
    from electrumx.lib.raptoreum_future import future_spendability
    from tests.server.test_raptoreum_future import future_tx
    from tests.lib.test_raptoreum_assets import P2PKH
    original=os.getcwd();db=None
    request={'mode':'outputs','asset_id':ASSET,'after':'','limit':10,'spends':set()}
    hashx=hashlib.sha256(P2PKH).digest()[:11]
    def block(previous,txs,stamp,nonce):
        raw=raw_block(previous,txs,nonce)
        return raw[:68]+struct.pack('<I',stamp)+raw[72:]
    try:
        db=RaptoreumDB(env(tmp_path));await db.open_for_sync()
        bp=RaptoreumBlockProcessor(db.env,db,SimpleNamespace(cached_height=lambda:5),None)
        genesis=block(bytes(32),[],1,0);bp.advance_blocks([Raptoreum.block(genesis,0)])
        first=block(bp.tip,[future_tx(maturity=3,lock_time=100,output=1).serialize()],1000,1)
        bp.advance_blocks([Raptoreum.block(first,1)]);await bp.flush(True)
        def locked_row():return db.asset_view(hashx,request)['outputs'][1]
        row=locked_row();assert row['future']['confirmed_time']==1000
        assert future_spendability(row,db.db_height,1000)['spendable'] is False
        close(db);db=RaptoreumDB(env(tmp_path));await db.open_for_sync()
        bp=RaptoreumBlockProcessor(db.env,db,SimpleNamespace(cached_height=lambda:5),None)
        bp.height,bp.tip,bp.tx_count=db.db_height,db.db_tip,db.db_tx_count
        assert locked_row()['future']['confirmed_time']==1000
        second=block(bp.tip,[],1001,2);bp.advance_blocks([Raptoreum.block(second,2)])
        third=block(bp.tip,[],1002,3);bp.advance_blocks([Raptoreum.block(third,3)]);await bp.flush(True)
        assert future_spendability(locked_row(),db.db_height,1000)['spendable'] is True
        async with bp.state_lock:
            bp.backup_blocks([third]);db.flush_backup(bp.flush_data(),bp.touched)
        assert future_spendability(locked_row(),db.db_height,1000)['spendable'] is False
        assert future_spendability(locked_row(),db.db_height,1100)['spendable'] is True
        async with bp.state_lock:
            bp.backup_blocks([second,first]);db.flush_backup(bp.flush_data(),bp.touched)
        assert not db.asset_view(hashx,request)['outputs']
        alternate=block(bp.tip,[future_tx(maturity=3,lock_time=100,output=1).serialize()],2000,4)
        bp.advance_blocks([Raptoreum.block(alternate,1)]);await bp.flush(True)
        assert locked_row()['future']['confirmed_time']==2000
        assert future_spendability(locked_row(),db.db_height,1100)['spendable'] is False
    finally:
        if db:close(db)
        os.chdir(original)


@pytest.mark.asyncio
async def test_legacy_asset_schema_is_rejected_without_deleting_data(tmp_path):
    original=os.getcwd();db=None
    try:
        db=RaptoreumDB(env(tmp_path));await db.open_for_sync()
        db.utxo_db.put(SCHEMA_KEY,b'1');db.utxo_db.put(b'old-asset-data',b'preserve')
        close(db);db=RaptoreumDB(env(tmp_path))
        with pytest.raises(DB.DBError,match='fresh DB_DIRECTORY'):await db.open_for_sync()
        assert db.utxo_db.get(b'old-asset-data')==b'preserve'
    finally:
        if db:close(db)
        os.chdir(original)
