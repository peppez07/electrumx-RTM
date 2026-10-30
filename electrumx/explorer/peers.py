'''Discovers public wallet gateways run by registered smartnodes.

Core's deterministic smartnode list supplies candidate IPv4 addresses; each
entry is backed by collateral, which limits fake entries. A candidate is only
contacted at https://IP/api/v1/electrum with `server.features` and
`blockchain.headers.subscribe`, with normal certificate validation - the same
requirements a browser wallet has. Nothing is announced or stored remotely.
'''

import asyncio
import ipaddress
import json
import re
import time
import uuid

import aiohttp

from .clients import BackendError


RTM_GENESIS_HASH = 'b79e5df07278b9567ada8fc655ffbfa9d3f586dc38da3dd93053686f41caeea0'
GATEWAY_PATH = '/api/v1/electrum'
HOST_RE = re.compile(r'(?=.{1,253}\Z)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?'
                     r'(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+\Z')
MAX_PROBE_BYTES = 65536


def gateway_url(host):
    return 'https://'+host+GATEWAY_PATH


def public_host(value):
    '''Returns a normalised public IPv4 address or DNS name, else None.'''
    host = str(value).strip().lower()
    if host.startswith('https://'):
        host = host[len('https://'):].split('/', 1)[0]
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host if HOST_RE.fullmatch(host) and not host.endswith('.local') else None
    return str(ip) if ip.version == 4 and ip.is_global else None


def smartnode_hosts(result):
    '''Public IPv4 hosts of ENABLED smartnodes from `smartnodelist json`.'''
    hosts = set()
    if not isinstance(result, dict):
        return hosts
    for value in result.values():
        if not isinstance(value, dict) or value.get('status') != 'ENABLED':
            continue
        address = value.get('address')
        if not isinstance(address, str) or ':' not in address:
            continue
        host = address.rsplit(':', 1)[0].strip('[]')
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            continue
        if ip.version == 4 and ip.is_global:
            hosts.add(str(ip))
    return hosts


class PeerDirectory:
    '''Keeps a bounded list of verified gateways; never raises into request handlers.'''

    def __init__(self, core, seeds=(), sweep_interval=3600, recheck_interval=600,
                 concurrency=16, timeout=5, max_peers=256, clock=time.time):
        self.core = core
        self.seeds = {host for host in map(public_host, seeds) if host}
        self.sweep_interval, self.recheck_interval = sweep_interval, recheck_interval
        self.concurrency, self.timeout, self.max_peers = concurrency, timeout, max_peers
        self.clock = clock
        self.peers = {}
        self.smartnodes = set()
        self.last_sweep = None
        self.updated_at = None
        self.session = None

    async def run(self):
        while True:
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(self.recheck_interval)

    async def refresh(self):
        now = self.clock()
        if self.last_sweep is None or now - self.last_sweep >= self.sweep_interval:
            try:
                self.smartnodes = smartnode_hosts(await self.core.call('smartnodelist', ['json']))
                self.last_sweep = now
            except BackendError:
                pass
            candidates = self.seeds | self.smartnodes | set(self.peers)
        else:
            # Between sweeps only previously verified gateways and seeds are rechecked.
            candidates = self.seeds | set(self.peers)
        if self.session is None:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                cookie_jar=aiohttp.DummyCookieJar(), trust_env=False,
                connector=aiohttp.TCPConnector(limit=self.concurrency))
        semaphore = asyncio.Semaphore(self.concurrency)

        async def check(host):
            async with semaphore:
                return host, await self.probe(host)

        for host, info in await asyncio.gather(*(check(h) for h in sorted(candidates))):
            if info:
                self.peers[host] = dict(info, url=gateway_url(host),
                                        smartnode=host in self.smartnodes, seen_at=int(self.clock()))
            elif host in self.peers and self.clock() - self.peers[host]['seen_at'] > 2*self.sweep_interval:
                del self.peers[host]
        self.updated_at = int(self.clock())

    async def probe(self, host):
        session_id = str(uuid.uuid4())
        try:
            features = await self.rpc(host, session_id, 'server.features')
            if not isinstance(features, dict) or features.get('genesis_hash') != RTM_GENESIS_HASH:
                return None
            header = await self.rpc(host, session_id, 'blockchain.headers.subscribe')
            height = header.get('height') if isinstance(header, dict) else None
            if type(height) is not int or height <= 0:
                return None
            assets = features.get('rtm_assets')
            return {'height': height,
                    'assets_ready': isinstance(assets, dict) and assets.get('ready') is True}
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError, UnicodeError, RecursionError):
            return None

    async def rpc(self, host, session_id, method):
        async with self.session.post(gateway_url(host), allow_redirects=False,
                headers={'X-Electrum-Session': session_id},
                json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': []}) as response:
            if response.status != 200:
                raise ValueError('Gateway unavailable')
            body = bytearray()
            async for chunk in response.content.iter_chunked(8192):
                body.extend(chunk)
                if len(body) > MAX_PROBE_BYTES:
                    raise ValueError('Gateway response too large')
            message = json.loads(body)
            if not isinstance(message, dict) or 'result' not in message:
                raise ValueError('Gateway not ready')
            return message['result']

    def snapshot(self):
        cutoff = self.clock() - 2*self.sweep_interval
        peers = sorted((p for p in self.peers.values() if p['seen_at'] >= cutoff),
                       key=lambda p: (-p['height'], -p['seen_at'], p['url']))[:self.max_peers]
        return {'genesis_hash': RTM_GENESIS_HASH, 'updated_at': self.updated_at,
                'smartnodes_checked': len(self.smartnodes),
                'servers': [{key: p[key] for key in ('url', 'height', 'assets_ready', 'smartnode', 'seen_at')}
                            for p in peers]}

    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None
