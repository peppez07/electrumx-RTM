import struct

import pytest

from electrumx.lib.raptoreum_future import future_spendability
from electrumx.lib.tx_raptoreum import DeserializerRaptoreum
from electrumx.server.raptoreum_db import transfer_record, AssetIndex
from electrumx.lib.raptoreum_assets import parse_asset_script
from tests.lib.test_raptoreum_assets import asset_script, raw_tx, varbytes
from tests.server.test_raptoreum_index import MemoryStore, commit


def future_payload(maturity=5,lock_time=-1,output=0):
    # CFutureTx network serialization from Core2.0.3.01 providertx.h.
    return struct.pack('<HiiHHBH',1,maturity,lock_time,output,0,0,0)+varbytes(b'')+bytes(32)+struct.pack('<H',0)+bytes(32)


def future_tx(maturity=5,lock_time=-1,output=0):
    return DeserializerRaptoreum(raw_tx(7,future_payload(maturity,lock_time,output),[(0,asset_script()),(0,asset_script())])).read_tx()


@pytest.mark.parametrize('height,now,maturity,seconds,expected',[
    (103,1000,5,-1,False),(104,1000,5,-1,True),
    (100,1099,-1,100,False),(100,1100,-1,100,True),
    (104,1000,5,100,True),(100,1100,5,100,True),
    (100,1000,0,-1,True),(100,1000,-1,0,True),
    (100000,999999,-1,-1,False),
])
def test_core_maturity_predicate_uses_next_height_or_adjusted_time(height,now,maturity,seconds,expected):
    record={'height':100,'spendable':True,'future':{'maturity':maturity,'lock_time':seconds,'confirmed_time':1000}}
    assert future_spendability(record,height,now)['spendable'] is expected


def test_unconfirmed_future_output_never_matures_and_unknown_metadata_fails_closed():
    record={'height':0,'source_type':7,'spendable':True,'future':{'maturity':0,'lock_time':0,'confirmed_time':None}}
    assert future_spendability(record,100,1000)['spendable'] is False
    legacy={'height':100,'source_type':7,'spendable':False}
    assert future_spendability(legacy,200,999999)['spendable'] is False


def test_only_designated_output_is_locked_and_payload_is_persisted_with_undo():
    store=MemoryStore();index=AssetIndex(store);tx=future_tx(output=1)
    index.apply_block([(tx,b'x'*32)],100,0)
    unlocked=index.lookup(b'x'*32,0);locked=index.lookup(b'x'*32,1)
    assert unlocked['spendable'] is True and unlocked['future'] is None
    assert locked['future']=={'maturity':5,'lock_time':-1}
    assert locked['spendable'] is True  # canonical script; query applies dynamic lock
    commit(index);index=AssetIndex(store)
    assert index.lookup(b'x'*32,1)['future']['maturity']==5
    index.rollback(100);commit(index)
    assert index.lookup(b'x'*32,1) is None


def test_noncanonical_asset_output_is_not_made_selectable_by_maturity():
    record={'height':100,'spendable':False,'future':{'maturity':0,'lock_time':0,'confirmed_time':1000}}
    assert future_spendability(record,500,999999)['spendable'] is False

@pytest.mark.asyncio
async def test_daemon_uses_fresh_offset_and_rejects_changing_or_nonmain_chain(monkeypatch):
    import copy
    from electrumx.server.daemon import RaptoreumDaemon,DaemonError
    daemon=object.__new__(RaptoreumDaemon);calls=[]
    info={'chain':'main','initialblockdownload':False,'blocks':104,'headers':104,'bestblockhash':'aa'*32}
    async def send(method,params=None):
        calls.append(method)
        return {'timeoffset':25} if method=='getnetworkinfo' else copy.deepcopy(info)
    daemon._send_single=send
    monkeypatch.setattr('electrumx.server.daemon.time.time',lambda:1000.5)
    context=await daemon.future_spend_context()
    assert context=={'height':104,'tip':'aa'*32,'adjusted_time':1025}
    assert calls==['getblockchaininfo','getnetworkinfo','getblockchaininfo']
    info['chain']='test'
    with pytest.raises(DaemonError):await daemon.future_spend_context()
    info['chain']='main';n=[0]
    async def moving(method,params=None):
        if method=='getnetworkinfo':return {'timeoffset':0}
        n[0]+=1;return dict(info,bestblockhash=('aa' if n[0]==1 else 'bb')*32)
    daemon._send_single=moving
    with pytest.raises(DaemonError):await daemon.future_spend_context()


@pytest.mark.asyncio
async def test_protocol_rechecks_maturity_on_new_tip_time_and_clock_rollback():
    from tests.server.test_raptoreum_protocol import session,SCRIPTHASH,ASSET
    s=session();s.db.db_height=100;s.db.db_tip=bytes.fromhex('aa'*32)
    original=s.db.view['outputs'][0]
    original.update(height=100,source_type=7,spendable=True,future={'maturity':5,'lock_time':100,'confirmed_time':1000})
    clock=[1099];calls=[]
    async def context(method,*args):
        calls.append(method);assert method=='future_spend_context'
        return {'height':s.db.view['height'],'tip':s.db.view['tip'],'adjusted_time':clock[0]}
    s.daemon_request=context
    first=await s.assets_listunspent(SCRIPTHASH,ASSET)
    assert first['items'][0]['spendable'] is False
    clock[0]=1100
    assert (await s.assets_listunspent(SCRIPTHASH,ASSET))['items'][0]['spendable'] is True
    clock[0]=1099
    assert (await s.assets_listunspent(SCRIPTHASH,ASSET))['items'][0]['spendable'] is False
    s.db.view.update(height=104);s.db.db_height=104;s.mempool.asset_tip=(104,'aa'*32)
    assert (await s.assets_listunspent(SCRIPTHASH,ASSET))['items'][0]['spendable'] is True
    s.db.view.update(height=103);s.db.db_height=103;s.mempool.asset_tip=(103,'aa'*32)
    assert (await s.assets_listunspent(SCRIPTHASH,ASSET))['items'][0]['spendable'] is False
    assert len(calls)==5


@pytest.mark.asyncio
async def test_protocol_never_returns_eligible_future_outputs_from_wrong_core_tip():
    from aiorpcx import RPCError
    from tests.server.test_raptoreum_protocol import session,SCRIPTHASH,ASSET
    s=session();s.db.db_height=100;s.db.db_tip=bytes.fromhex('aa'*32)
    s.db.view['outputs'][0].update(source_type=7,future={'maturity':0,'lock_time':0,'confirmed_time':1000})
    async def context(*args):return {'height':101,'tip':'bb'*32,'adjusted_time':9999}
    s.daemon_request=context
    with pytest.raises(RPCError):await s.assets_listunspent(SCRIPTHASH,ASSET)


def test_future_predicate_matches_pinned_core_cpp_oracle(tmp_path):
    import itertools,shutil,subprocess,os
    from pathlib import Path
    compiler=shutil.which('g++')
    if not compiler:pytest.skip('C++ compiler required; exercised by the Linux image suite')
    source=Path(__file__).parents[1]/'fixtures/raptoreum/future_maturity_oracle.cpp'
    binary=tmp_path/'oracle'
    # Upstream environment tests clear os.environ, including PATH. Restore a
    # tool search path only for the compiler subprocess so it can find ld.
    tool_env=dict(os.environ,PATH=os.path.dirname(compiler)+os.pathsep+os.defpath)
    subprocess.run([compiler,'-std=c++11','-O2',str(source),'-o',str(binary)],check=True,env=tool_env)
    cases=list(itertools.product([-1,0,1,5,2**31-1],[-1,0,1,60,2**31-1],[100,101,104,105,106],[999,1000,1001,1059,1060]))
    data=''.join(f'{maturity} {seconds} {height+1} 100 {now} 1000\n' for maturity,seconds,height,now in cases)
    expected=subprocess.check_output([str(binary)],input=data,text=True).splitlines()
    actual=[]
    for maturity,seconds,height,now in cases:
        record={'height':100,'spendable':True,'future':{'maturity':maturity,'lock_time':seconds,'confirmed_time':1000}}
        actual.append(str(int(future_spendability(record,height,now)['spendable'])))
    assert actual==expected


@pytest.mark.asyncio
async def test_unconfirmed_designated_asset_and_unlocked_subasset_in_mempool():
    from electrumx.lib.coins import Raptoreum
    from electrumx.lib.hash import hash_to_hex_str
    from electrumx.lib.tx import TxInput
    from electrumx.server.raptoreum_mempool import RaptoreumMemPool
    from tests.server.test_raptoreum_mempool import API
    from tests.lib.test_raptoreum_assets import ASSET,P2PKH
    previous=b'p'*32
    tx=future_tx(output=0)._replace(inputs=[TxInput(previous,0,b'',0xffffffff)])
    child='34'*32
    tx=tx._replace(outputs=[tx.outputs[0],type(tx.outputs[1])(0,asset_script().replace(ASSET.encode(),child.encode()))])
    raw=tx.serialize();_,txhash=DeserializerRaptoreum(raw).read_tx_and_hash()
    pool=RaptoreumMemPool(Raptoreum,API({hash_to_hex_str(txhash):raw},{},{(previous,0):(Raptoreum.hashX_from_script(P2PKH),10000)}))
    await pool._process_mempool({txhash},set(),100)
    locked,unlocked=pool.asset_outputs[txhash,0],pool.asset_outputs[txhash,1]
    assert future_spendability(locked,100,999999)['spendable'] is False
    assert future_spendability(unlocked,100,999999)['spendable'] is True
    assert unlocked['asset_id']==child


@pytest.mark.asyncio
async def test_context_failure_or_mempool_race_never_publishes_spendable():
    from aiorpcx import RPCError
    from tests.server.test_raptoreum_protocol import session,SCRIPTHASH,ASSET
    s=session();s.db.db_height=100;s.db.db_tip=bytes.fromhex('aa'*32)
    s.db.view['outputs'][0].update(source_type=7,future={'maturity':0,'lock_time':0,'confirmed_time':1000})
    async def fail(*args):raise RuntimeError('RPC unavailable')
    s.daemon_request=fail
    with pytest.raises(RPCError):await s.assets_listunspent(SCRIPTHASH,ASSET)
    async def moving(*args):
        s.mempool.asset_generation+=1
        return {'height':100,'tip':'aa'*32,'adjusted_time':9999}
    s.daemon_request=moving
    with pytest.raises(RPCError):await s.assets_listunspent(SCRIPTHASH,ASSET)

@pytest.mark.asyncio
@pytest.mark.parametrize('change',['db_tip','db_height','not_ready','mempool_tip'])
async def test_future_page_rejects_each_post_rpc_state_regression(change):
    from aiorpcx import RPCError
    from tests.server.test_raptoreum_protocol import session,SCRIPTHASH,ASSET
    s=session();s.db.db_height=100;s.db.db_tip=bytes.fromhex('aa'*32)
    s.db.view['outputs'][0].update(source_type=7,future={'maturity':0,'lock_time':0,'confirmed_time':1000})
    async def context(*args):
        if change=='db_tip':s.db.db_tip=bytes.fromhex('bb'*32)
        if change=='db_height':s.db.db_height=99
        if change=='not_ready':s.mempool.asset_ready=False
        if change=='mempool_tip':s.mempool.asset_tip=(99,'bb'*32)
        return {'height':100,'tip':'aa'*32,'adjusted_time':9999}
    s.daemon_request=context
    with pytest.raises(RPCError):await s.assets_listunspent(SCRIPTHASH,ASSET)


@pytest.mark.asyncio
@pytest.mark.parametrize('bad',[None,True,'0',0.5])
async def test_daemon_rejects_missing_or_malformed_clock_offset(bad):
    from electrumx.server.daemon import RaptoreumDaemon,DaemonError
    daemon=object.__new__(RaptoreumDaemon)
    async def send(method,*args):
        if method=='getnetworkinfo':return {'timeoffset':bad}
        return {'chain':'main','initialblockdownload':False,'blocks':100,'headers':100,'bestblockhash':'aa'*32}
    daemon._send_single=send
    with pytest.raises(DaemonError):await daemon.future_spend_context()


@pytest.mark.asyncio
async def test_daemon_never_checks_future_clock_during_initial_download():
    from electrumx.server.daemon import RaptoreumDaemon,DaemonError
    daemon=object.__new__(RaptoreumDaemon);calls=[]
    async def send(method,*args):
        calls.append(method)
        return {'chain':'main','initialblockdownload':True,'blocks':100,'headers':200,'bestblockhash':'aa'*32}
    daemon._send_single=send
    with pytest.raises(DaemonError):await daemon.future_spend_context()
    assert calls==['getblockchaininfo']
