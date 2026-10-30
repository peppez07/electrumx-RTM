'''Read a small shared host snapshot; never inspect host processes or query Core.'''

import asyncio
import json
import math
from pathlib import Path
import time


class ResourceSnapshot:
    def __init__(self, path):
        self.path = Path(path) if path else None
        self.cached, self.checked = None, -float('inf')
        self.lock = asyncio.Lock()

    def read(self):
        if self.path is None:
            return None
        try:
            with self.path.open('rb') as source:
                raw = source.read(16385)
            if len(raw) > 16384:
                return None
            data = json.loads(raw)
            timestamp = data['sampled_at']
            if type(timestamp) is not int or not 0 < timestamp <= time.time()+5:
                return None
            result = {'sampled_at':timestamp}
            for group, fields in (
                ('memory', ('total_bytes','used_bytes','available_bytes')),
                ('swap', ('total_bytes','used_bytes')),
                ('disk', ('total_bytes','used_bytes','available_bytes'))):
                values = {key:data[group][key] for key in fields}
                if any(type(v) is not int or not 0 <= v < 2**63 for v in values.values()):
                    return None
                if values['used_bytes'] > values['total_bytes'] or values.get('available_bytes',0) > values['total_bytes']:
                    return None
                if group != 'swap' and values['total_bytes'] == 0:
                    return None
                result[group] = values
            cpu = {key:data['cpu'][key] for key in ('used_percent','iowait_percent','interval_seconds')}
            for key,value in cpu.items():
                maximum = 180 if key == 'interval_seconds' else 100
                if value is not None and (type(value) not in (int,float) or not math.isfinite(value) or not 0 <= value <= maximum):
                    return None
            result['cpu'] = cpu
            return result
        except (OSError, ValueError, KeyError, TypeError):
            return None

    async def get(self):
        async with self.lock:
            if time.monotonic()-self.checked >= 10:
                self.cached = await asyncio.get_running_loop().run_in_executor(None, self.read)
                self.checked = time.monotonic()
        if self.cached is None:
            return {'available':False, 'scope':'vps', 'refresh_seconds':60}
        age = max(0, int(time.time())-self.cached['sampled_at'])
        return dict(self.cached, available=True, scope='vps', age_seconds=age,
                    stale=age > 180, refresh_seconds=60)
