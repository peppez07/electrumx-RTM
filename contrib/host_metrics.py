#!/usr/bin/env python3
'''Once-per-minute whole-host resource snapshot; no RPC or process scanning.'''

import argparse
import json
import os
from pathlib import Path
import shutil
import tempfile
import time


def atomic_json(path, value):
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=str(path.parent), delete=False) as output:
            temporary = Path(output.name)
            json.dump(value, output, separators=(',', ':'))
        temporary.chmod(0o640)
        os.replace(str(temporary), str(path))
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def sample(proc=Path('/proc'), disk_path='/', previous=None, now=None, monotonic=None):
    proc = Path(proc)
    now = time.time() if now is None else now
    monotonic = time.monotonic() if monotonic is None else monotonic
    lines = (proc/'stat').read_text().splitlines()
    values = list(map(int, lines[0].split()[1:9]))
    if len(values) != 8 or any(v < 0 for v in values):
        raise ValueError('Invalid CPU counters')
    boot = next(line.split()[1] for line in lines if line.startswith('btime '))
    total, idle, wait = sum(values), values[3], values[4]
    percent, io_wait, interval = None, None, None
    if isinstance(previous, dict) and previous.get('boot') == boot:
        try:
            seconds = monotonic-previous['monotonic']
            delta = total-previous['total']
            idle_delta, wait_delta = idle-previous['idle'], wait-previous['wait']
            if 0 < seconds <= 180 and delta > 0 and 0 <= idle_delta+wait_delta <= delta and min(idle_delta,wait_delta) >= 0:
                percent = round(100*(delta-idle_delta-wait_delta)/delta, 2)
                io_wait = round(100*wait_delta/delta, 2)
                interval = round(seconds, 2)
        except (KeyError, TypeError):
            pass
    memory = {key:int(value.split()[0])*1024 for key,value in (
        line.split(':',1) for line in (proc/'meminfo').read_text().splitlines())}
    ram_total, ram_available = memory['MemTotal'], memory['MemAvailable']
    swap_total, swap_free = memory['SwapTotal'], memory['SwapFree']
    if not (0 <= ram_available <= ram_total and ram_total > 0 and 0 <= swap_free <= swap_total):
        raise ValueError('Invalid memory counters')
    disk = shutil.disk_usage(disk_path)
    result = {'sampled_at':int(now),
        'cpu':{'used_percent':percent, 'iowait_percent':io_wait, 'interval_seconds':interval},
        'memory':{'total_bytes':ram_total, 'used_bytes':ram_total-ram_available, 'available_bytes':ram_available},
        'swap':{'total_bytes':swap_total, 'used_bytes':swap_total-swap_free},
        'disk':{'total_bytes':disk.total, 'used_bytes':disk.used, 'available_bytes':disk.free}}
    state = {'boot':boot, 'monotonic':monotonic, 'total':total, 'idle':idle, 'wait':wait}
    return result, state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', default='/var/lib/rtm-explorer/metrics')
    args = parser.parse_args(argv)
    directory = Path(args.directory)
    try:
        try:
            previous = json.loads((directory/'cpu-state.json').read_text())
        except (OSError, ValueError):
            previous = None
        result, state = sample(previous=previous)
        atomic_json(directory/'cpu-state.json', state)
        atomic_json(directory/'host.json', result)
    except (OSError, ValueError, KeyError, StopIteration):
        print('Host resource sampling unavailable; previous snapshot retained.')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
