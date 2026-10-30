import pytest

from electrumx.explorer.clients import BackendError
from electrumx.explorer.peers import (RTM_GENESIS_HASH, PeerDirectory, public_host,
                                      smartnode_hosts)


def test_smartnode_hosts_keep_enabled_public_ipv4_only():
    listing = {
        'a-0': {'address': '203.0.113.9:10226', 'status': 'ENABLED'},
        'b-0': {'address': '203.0.113.9:10227', 'status': 'ENABLED'},
        'c-0': {'address': '198.51.100.7:10226', 'status': 'POSE_BANNED'},
        'd-0': {'address': '10.0.0.5:10226', 'status': 'ENABLED'},
        'e-0': {'address': '127.0.0.1:10226', 'status': 'ENABLED'},
        'f-0': {'address': '[2001:db8::1]:10226', 'status': 'ENABLED'},
        'g-0': {'address': 'junk', 'status': 'ENABLED'},
        'h-0': 'junk',
    }
    # Documentation ranges are not global; use real public examples for the positive case.
    listing['i-0'] = {'address': '8.8.8.8:10226', 'status': 'ENABLED'}
    assert smartnode_hosts(listing) == {'8.8.8.8'}
    assert smartnode_hosts(None) == set()


@pytest.mark.parametrize('value, expected', [
    ('144.202.18.233', '144.202.18.233'),
    ('https://Node.Example.org/api/v1/electrum', 'node.example.org'),
    ('192.168.1.1', None), ('localhost', None), ('printer.local', None), ('bad host', None),
])
def test_public_host(value, expected):
    assert public_host(value) == expected


class Core:
    def __init__(self, listing):
        self.listing, self.calls = listing, 0

    async def call(self, method, params=None):
        assert (method, params) == ('smartnodelist', ['json'])
        self.calls += 1
        if isinstance(self.listing, Exception):
            raise self.listing
        return self.listing


class Clock:
    def __init__(self):
        self.now = 1000

    def __call__(self):
        return self.now


def directory(core, answers, clock, **options):
    peers = PeerDirectory(core, clock=clock, sweep_interval=3600, **options)
    probed = []

    async def rpc(host, session_id, method):
        probed.append((host, method))
        answer = answers.get(host)
        if answer is None:
            raise ValueError('unreachable')
        return answer[method]
    peers.rpc = rpc
    return peers, probed


def ok(height, genesis=RTM_GENESIS_HASH, ready=True):
    return {'server.features': {'genesis_hash': genesis, 'rtm_assets': {'ready': ready}},
            'blockchain.headers.subscribe': {'height': height}}


@pytest.mark.asyncio
async def test_sweep_verifies_genesis_and_height_and_rechecks_only_known_between_sweeps():
    core = Core({'a': {'address': '8.8.8.8:10226', 'status': 'ENABLED'},
                 'b': {'address': '1.1.1.1:10226', 'status': 'ENABLED'},
                 'c': {'address': '9.9.9.9:10226', 'status': 'ENABLED'}})
    clock = Clock()
    peers, probed = directory(core, {
        '8.8.8.8': ok(500), '1.1.1.1': ok(400, genesis='00'*32), '144.202.18.233': ok(501, ready=False)},
        clock, seeds=['144.202.18.233'])
    await peers.refresh()
    snapshot = peers.snapshot()
    assert [s['url'] for s in snapshot['servers']] == [
        'https://144.202.18.233/api/v1/electrum', 'https://8.8.8.8/api/v1/electrum']
    assert snapshot['servers'][0] == {'url': 'https://144.202.18.233/api/v1/electrum', 'height': 501,
                                      'assets_ready': False, 'smartnode': False, 'seen_at': 1000}
    assert snapshot['servers'][1]['smartnode'] is True
    assert snapshot['smartnodes_checked'] == 3

    probed.clear()
    clock.now += 600
    await peers.refresh()
    assert core.calls == 1
    assert {host for host, _ in probed} == {'8.8.8.8', '144.202.18.233'}


@pytest.mark.asyncio
async def test_unreachable_peers_expire_and_core_errors_keep_seeds():
    clock = Clock()
    answers = {'8.8.8.8': ok(500)}
    peers, _ = directory(Core({'a': {'address': '8.8.8.8:1', 'status': 'ENABLED'}}), answers, clock)
    await peers.refresh()
    assert len(peers.snapshot()['servers']) == 1
    answers.clear()
    clock.now += 3600
    await peers.refresh()
    assert len(peers.snapshot()['servers']) == 1  # recently seen; kept briefly
    clock.now += 2*3600 + 1
    await peers.refresh()
    assert peers.snapshot()['servers'] == []

    seeded, _ = directory(Core(BackendError()), {'144.202.18.233': ok(9)}, Clock(), seeds=['144.202.18.233'])
    await seeded.refresh()
    assert [s['height'] for s in seeded.snapshot()['servers']] == [9]


@pytest.mark.asyncio
async def test_probe_rejects_bad_heights_and_non_dict_answers():
    clock = Clock()
    peers, _ = directory(Core({}), {
        '8.8.8.8': {'server.features': {'genesis_hash': RTM_GENESIS_HASH},
                    'blockchain.headers.subscribe': {'height': 'tall'}},
        '1.1.1.1': {'server.features': ['x'], 'blockchain.headers.subscribe': {}},
    }, clock, seeds=['8.8.8.8', '1.1.1.1'])
    await peers.refresh()
    assert peers.snapshot()['servers'] == []
    await peers.close()
