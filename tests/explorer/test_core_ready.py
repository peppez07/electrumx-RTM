import importlib.util
from pathlib import Path

import pytest


def gate():
    path=Path(__file__).resolve().parents[2]/'contrib/core_ready.py'
    spec=importlib.util.spec_from_file_location('core_ready',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('info,expected',[
    ({'chain':'main','initialblockdownload':False,'blocks':100,'headers':100},True),
    ({'chain':'main','initialblockdownload':True,'blocks':100,'headers':100},False),
    ({'chain':'main','initialblockdownload':False,'blocks':99,'headers':100},False),
    ({'chain':'test','initialblockdownload':False,'blocks':100,'headers':100},False),
    ({'chain':'main','blocks':100,'headers':100},False), ({},False)])
def test_only_synced_mainnet_can_start_backend(info,expected):
    assert gate().ready(info) is expected


def test_gate_rpc_is_status_only_and_hides_backend_errors():
    module=gate(); calls=[]
    def transport(url,headers,body,timeout):
        calls.append((url,body))
        return {'result':{'chain':'main','initialblockdownload':True,'blocks':50,'headers':100}}
    config={'url':'http://127.0.0.1:10225/','username':'user','password':'SECRET'}
    status=module.check(config,transport)
    assert status['ready'] is False and status['phase'] == 'core_syncing'
    assert calls[0][1] == {'jsonrpc':'1.0','id':'core-ready','method':'getblockchaininfo','params':[]}
    def broken(*args): raise RuntimeError('SECRET leaked by backend')
    status=module.check(config,broken)
    assert status == {'ready':False,'phase':'core_unavailable'}
    assert 'SECRET' not in str(status)
