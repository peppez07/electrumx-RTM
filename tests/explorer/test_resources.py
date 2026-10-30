import importlib.util
import json
from pathlib import Path

import pytest

ROOT=Path(__file__).resolve().parents[2]


def sampler():
    spec=importlib.util.spec_from_file_location('host_metrics',ROOT/'contrib/host_metrics.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def proc_files(tmp_path,cpu='cpu 100 0 100 800 0 0 0 0\nbtime 1000\n'):
    proc=tmp_path/'proc';proc.mkdir(exist_ok=True)
    (proc/'stat').write_text(cpu)
    (proc/'meminfo').write_text('MemTotal: 1000 kB\nMemAvailable: 400 kB\nSwapTotal: 500 kB\nSwapFree: 200 kB\n')
    return proc


def test_sampler_cpu_delta_memory_and_filesystem_without_subprocess(tmp_path):
    module=sampler();proc=proc_files(tmp_path)
    first,state=module.sample(proc=proc,disk_path=tmp_path,now=100,monotonic=10)
    assert first['cpu']['used_percent'] is None
    assert first['memory']['used_bytes']==600*1024
    assert first['swap']['used_bytes']==300*1024
    assert first['disk']['total_bytes']>first['disk']['used_bytes']>=0
    (proc/'stat').write_text('cpu 200 0 200 1600 0 0 0 0\nbtime 1000\n')
    second,_=module.sample(proc=proc,disk_path=tmp_path,previous=state,now=160,monotonic=70)
    assert second['cpu']['used_percent']==20
    assert second['cpu']['interval_seconds']==60
    assert second['sampled_at']==160
    assert set(second)=={'sampled_at','cpu','memory','swap','disk'}


def test_reboot_or_long_gap_does_not_report_fabricated_cpu(tmp_path):
    module=sampler();proc=proc_files(tmp_path);_,state=module.sample(proc=proc,disk_path=tmp_path,now=100,monotonic=10)
    (proc/'stat').write_text('cpu 200 0 200 1600 0 0 0 0\nbtime 2000\n')
    result,_=module.sample(proc=proc,disk_path=tmp_path,previous=state,now=160,monotonic=70)
    assert result['cpu']['used_percent'] is None
    (proc/'stat').write_text('cpu 200 0 200 1600 0 0 0 0\nbtime 1000\n')
    result,_=module.sample(proc=proc,disk_path=tmp_path,previous=state,now=1000,monotonic=910)
    assert result['cpu']['used_percent'] is None


def test_missing_available_memory_fails_instead_of_claiming_zero(tmp_path):
    module=sampler();proc=proc_files(tmp_path);(proc/'meminfo').write_text('MemTotal: 1000 kB\n')
    with pytest.raises((KeyError,ValueError)):module.sample(proc=proc,disk_path=tmp_path)


def test_atomic_snapshot_does_not_publish_partial_json(tmp_path):
    module=sampler();target=tmp_path/'snapshot.json';module.atomic_json(target,{'ok':True})
    assert json.loads(target.read_text())=={'ok':True}
    assert list(tmp_path.iterdir())==[target]

@pytest.mark.asyncio
async def test_resource_route_works_during_ibd_without_rpc_and_drops_private_fields(tmp_path):
    import time
    from aiohttp.test_utils import TestClient,TestServer
    from electrumx.explorer.service import create_app
    from tests.explorer.test_api import Core,Electrum
    module=sampler();proc=proc_files(tmp_path)
    data,_=module.sample(proc=proc,disk_path=tmp_path)
    data['hostname']='do-not-expose';data['cpu']['pid']=123
    path=tmp_path/'host.json';path.write_text(json.dumps(data))
    core,electrum=Core(),Electrum();core.info['initialblockdownload']=True
    async with TestClient(TestServer(create_app(core,electrum,metrics_file=path))) as client:
        response=await client.get('/api/v1/resources');result=await response.json()
        assert response.status==200 and result['available'] and result['scope']=='vps'
        assert result['stale'] is False and result['refresh_seconds']==60
        assert 'hostname' not in result and 'pid' not in result['cpu']
        assert core.calls==[] and electrum.calls==[]


@pytest.mark.asyncio
async def test_resource_missing_corrupt_oversize_or_future_file_is_explicitly_unavailable(tmp_path):
    from electrumx.explorer.resources import ResourceSnapshot
    path=tmp_path/'host.json'
    for content in (None,'not-json',' '*20000,'{"sampled_at":9999999999999}'):
        if content is not None:path.write_text(content)
        result=await ResourceSnapshot(path).get()
        assert result=={'available':False,'scope':'vps','refresh_seconds':60}
    assert (await ResourceSnapshot(None).get())['available'] is False


@pytest.mark.asyncio
async def test_resource_reader_caches_small_files_but_marks_old_samples_stale(tmp_path):
    import time
    from electrumx.explorer.resources import ResourceSnapshot
    module=sampler();proc=proc_files(tmp_path);data,_=module.sample(proc=proc,disk_path=tmp_path,now=time.time()-200)
    path=tmp_path/'host.json';path.write_text(json.dumps(data));reader=ResourceSnapshot(path)
    result=await reader.get();assert result['available'] and result['stale']
    path.write_text('broken')
    assert (await reader.get())['available']
