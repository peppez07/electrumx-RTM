import asyncio

import pytest

from electrumx.explorer.cache import ReadCache


@pytest.mark.asyncio
async def test_cache_is_bounded_expires_and_returns_independent_values():
    clock=[0]
    cache=ReadCache(max_bytes=100,max_entries=2,clock=lambda:clock[0])
    calls=[]
    async def load():
        calls.append(1);return {'value':'x'*25}
    first=await cache.get('a',load,10)
    first['value']='mutated'
    assert (await cache.get('a',load,10))['value'] == 'x'*25
    assert len(calls) == 1
    await cache.get('b',load,10);await cache.get('c',load,10)
    assert cache.byte_size <= 100 and cache.entry_count <= 2
    await cache.get('a',load,10)
    assert len(calls) == 4
    clock[0]=11
    await cache.get('a',load,10)
    assert len(calls) == 5


@pytest.mark.asyncio
async def test_concurrent_reads_share_work_and_one_cancel_does_not_cancel_other_readers():
    cache=ReadCache();started=asyncio.Event();finish=asyncio.Event();calls=[]
    async def load():
        calls.append(1);started.set();await finish.wait();return {'ok':True}
    first=asyncio.create_task(cache.get('same',load,10));await started.wait()
    second=asyncio.create_task(cache.get('same',load,10));await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):await first
    finish.set()
    assert await second == {'ok':True} and len(calls) == 1


@pytest.mark.asyncio
async def test_oversized_results_are_not_retained_and_changed_tip_has_an_independent_key():
    cache=ReadCache(max_bytes=10);calls=[]
    async def large():calls.append(1);return {'large':'x'*100}
    await cache.get(('tip1','overview'),large,10)
    await cache.get(('tip1','overview'),large,10)
    assert cache.byte_size == 0 and len(calls) == 2
    async def small():return {'n':1}
    await cache.get(('tip1','small'),small,10)
    result=await cache.get(('tip2','small'),lambda:async_value({'n':2}),10)
    assert result == {'n':2}


async def async_value(value):return value
