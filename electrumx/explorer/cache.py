'''Small process-local cache for public explorer read models only.'''

import asyncio
import json
import time
from collections import OrderedDict


class ReadCache:
    def __init__(self, max_bytes=2*1024*1024, max_entries=32, clock=time.monotonic):
        self.max_bytes, self.max_entries, self.clock = max_bytes, max_entries, clock
        self.entries, self.pending = OrderedDict(), {}
        self.byte_size = 0

    @property
    def entry_count(self):
        return len(self.entries)

    def discard(self, key):
        _, data = self.entries.pop(key)
        self.byte_size -= len(data)

    async def get(self, key, factory, ttl):
        now = self.clock()
        for expired in [k for k, (until, _) in self.entries.items() if until <= now]:
            self.discard(expired)
        if key in self.entries:
            self.entries.move_to_end(key)
            return json.loads(self.entries[key][1])
        if key not in self.pending:
            async def load():
                value = await asyncio.wait_for(factory(), 25)
                data = json.dumps(value, default=str, separators=(',', ':')).encode()
                if len(data) <= self.max_bytes:
                    while self.entries and (self.byte_size+len(data) > self.max_bytes or
                                            len(self.entries) >= self.max_entries):
                        self.discard(next(iter(self.entries)))
                    if self.max_entries > 0:
                        self.entries[key] = (self.clock()+ttl, data)
                        self.byte_size += len(data)
                return data
            task = self.pending[key] = asyncio.create_task(load())
            def done(finished):
                self.pending.pop(key, None)
                if not finished.cancelled():
                    finished.exception()
            task.add_done_callback(done)
        return json.loads(await asyncio.shield(self.pending[key]))

    async def close(self):
        tasks = list(self.pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
