'''Wallet API tests use local fixtures, never sign with or send funds to Core.'''
import copy
from decimal import Decimal
import hashlib

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from electrumx.explorer.clients import BackendError
from electrumx.explorer.service import create_app
from tests.explorer.test_api import ADDRESS, ASSET, TXID, TIP, Core, Electrum
from tests.lib.test_raptoreum_assets import P2PKH, new_asset_payload, raw_tx as network_tx

CHILD = '34'*32


def raw_tx(*args):
    # Framing fixture only: it has an input and output, but no valid signatures.
    # Acceptance in this file is provided by WalletCore, never a real daemon.
    return network_tx(*args, outputs=[(1000, P2PKH)])


class WalletCore(Core):
    async def call(self, method, params=None):
        if method in ('decoderawtransaction', 'sendrawtransaction', 'spork'):
            self.calls.append((method, params or []))
            if method == 'decoderawtransaction':
                return {'txid':TXID, 'type':0, 'hex':params[0]}
            if method == 'sendrawtransaction':
                return TXID
            return {'SPORK_22_SPECIAL_TX_FEE':25600 if params == ['show'] else True}
        if method == 'getrawtransaction' and params[1] is False:
            return raw_tx().hex()
        return await super().call(method, params)


class WalletElectrum(Electrum):
    async def call(self, method, params=None):
        if method == 'blockchain.estimatefee':
            self.calls.append((method, params))
            return Decimal('0.00001234')
        if method == 'blockchain.relayfee':
            return Decimal('0.00001000')
        if method == 'blockchain.transaction.get_merkle':
            return {'block_height':100, 'merkle':['11'*32], 'pos':0}
        if method == 'blockchain.asset.get_metadata':
            self.calls.append((method, params))
            child = params[0] == CHILD
            return {'metadata': {'asset_id':params[0], 'is_root':not child,
                'root_id':ASSET if child else None, 'name':'ROOT|CHILD' if child else 'ROOT',
                'supply':'9007199254740993', 'owner':'11'*20},
                'indexed_height':100, 'indexed_tip':TIP}
        return await super().call(method, params)


@pytest_asyncio.fixture
async def wallet():
    core, electrum = WalletCore(), WalletElectrum()
    async with TestClient(TestServer(create_app(core, electrum, status_ttl=0,
                                              enable_wallet_api=True))) as client:
        yield client, core, electrum


@pytest.mark.asyncio
async def test_wallet_capabilities_available_during_sync_without_live_data_queries(wallet):
    client, core, electrum = wallet
    core.info['initialblockdownload'] = True
    result = await (await client.get('/api/v1/wallet/capabilities')).json()
    assert result['network'] == 'mainnet'
    assert result['signing'] == 'client_only'
    assert result['asset_types'] == ['root', 'subasset', 'unique']
    assert result['broadcast_enabled'] is True and not result['ready']
    assert result['acceptance_preflight'] is False
    assert electrum.calls == []


@pytest.mark.asyncio
async def test_wallet_post_routes_are_disabled_without_explicit_opt_in():
    async with TestClient(TestServer(create_app(WalletCore(), WalletElectrum()))) as client:
        response = await client.post('/api/v1/wallet/broadcast', json={'hex':raw_tx().hex()})
        assert response.status == 403
        result = await (await client.get('/api/v1/wallet/capabilities')).json()
        assert result['broadcast_enabled'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('raw', [raw_tx().hex(), raw_tx(8,new_asset_payload()).hex(),
                                raw_tx(8,new_asset_payload(root=False)).hex()])
async def test_coin_root_and_subasset_bytes_relay_without_server_signing(wallet, raw):
    client, core, _ = wallet
    response = await client.post('/api/v1/wallet/broadcast', json={'hex':raw})
    assert response.status == 200
    result = await response.json()
    assert result['accepted'] is True and result['txid'] == TXID
    assert [entry for entry in core.calls if entry[0] != 'getblockchaininfo'] == [('sendrawtransaction',[raw])]


@pytest.mark.asyncio
async def test_decode_reports_syntax_only_not_signature_or_consensus_validation(wallet):
    client, core, _ = wallet
    result = await (await client.post('/api/v1/wallet/decode', json={'hex':raw_tx().hex()})).json()
    assert result['validation_level'] == 'syntax_only'
    assert result['signature_verified'] is False and result['acceptance_checked'] is False
    assert all(method != 'sendrawtransaction' for method,_ in core.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [{'hex':'zz'}, {'hex':'00'}, {'hex':123},
    {'hex':raw_tx().hex(),'seed':'private'}, {'hex':raw_tx().hex(),'privkeys':[]},
    {'hex':raw_tx().hex(),'maxfeerate':0}, {'hex':raw_tx().hex(),'bypasslimits':True},
    {'hex':'00'*100001}, ['raw'], {'hex':raw_tx().hex()+'00'}])
async def test_invalid_or_sensitive_payload_never_reaches_core(wallet, body):
    client, core, _ = wallet
    response = await client.post('/api/v1/wallet/broadcast', json=body)
    assert response.status in (400, 413)
    assert all(method == 'getblockchaininfo' for method,_ in core.calls)


@pytest.mark.asyncio
async def test_accepted_broadcast_is_not_reported_failed_after_tip_advances(wallet):
    client, core, _ = wallet
    original = core.call
    async def advance(method, params=None):
        result = await original(method, params)
        if method == 'sendrawtransaction':
            core.info.update(blocks=101, headers=101, bestblockhash='12'*32)
        return result
    core.call = advance
    response = await client.post('/api/v1/wallet/broadcast', json={'hex':raw_tx().hex()})
    assert response.status == 200 and (await response.json())['accepted'] is True
    assert len([method for method,_ in core.calls if method=='sendrawtransaction']) == 1


@pytest.mark.asyncio
async def test_coin_asset_relay_is_blocked_during_core_sync(wallet):
    client, core, _ = wallet
    core.info['initialblockdownload'] = True
    assert (await client.post('/api/v1/wallet/broadcast',json={'hex':raw_tx().hex()})).status == 503
    assert all(method == 'getblockchaininfo' for method,_ in core.calls)


@pytest.mark.asyncio
async def test_wallet_fees_and_dynamic_asset_policy_are_exact(wallet):
    client, _, _ = wallet
    result = await (await client.get('/api/v1/wallet/fees?blocks=6')).json()
    assert result['estimated_fee_per_kb_rtm'] == '0.00001234'
    assert result['relay_fee_per_kb_rtm'] == '0.00001000'
    result = await (await client.get('/api/v1/wallet/asset-policy')).json()
    assert result['special_tx_fee_rtm'] == '100'
    assert result['special_tx_fee_atomic'] == '10000000000'
    assert result['subassets_max_depth'] == 1


@pytest.mark.asyncio
async def test_subasset_context_returns_distinct_child_and_parent(wallet):
    client, _, electrum = wallet
    result = await (await client.get('/api/v1/wallet/assets/'+CHILD)).json()
    assert result['metadata']['asset_id'] == CHILD
    assert result['metadata']['root_id'] == ASSET
    assert result['root_metadata']['asset_id'] == ASSET
    assert result['metadata']['supply'] == '9007199254740993'
    assert result['subasset_creation_requires_root_owner_signature'] is True
    assert [params[0] for method, params in electrum.calls if method=='blockchain.asset.get_metadata'] == [CHILD, ASSET]


@pytest.mark.asyncio
async def test_raw_transaction_and_merkle_proof_available_for_local_verification(wallet):
    client, _, _ = wallet
    raw = await (await client.get('/api/v1/transactions/'+TXID+'/raw')).json()
    assert raw['hex'] == raw_tx().hex()
    proof = await (await client.get('/api/v1/transactions/'+TXID+'/proof?height=100')).json()
    assert proof['merkle'] == ['11'*32] and proof['block_height'] == 100


@pytest.mark.asyncio
async def test_browser_preflight_needs_no_backend_and_allows_no_credentials(wallet):
    client, core, _ = wallet
    response = await client.options('/api/v1/wallet/broadcast', headers={
        'Origin':'chrome-extension://example', 'Access-Control-Request-Method':'POST',
        'Access-Control-Request-Headers':'content-type'})
    assert response.status == 204
    assert response.headers['Access-Control-Allow-Origin'] == '*'
    assert 'POST' in response.headers['Access-Control-Allow-Methods']
    assert 'Access-Control-Allow-Credentials' not in response.headers
    assert core.calls == []


@pytest.mark.asyncio
async def test_broadcast_connection_failure_reports_unknown_outcome_without_retry(wallet):
    client, core, _ = wallet
    original = core.call
    submissions = []
    async def uncertain(method, params=None):
        if method == 'sendrawtransaction':
            submissions.append(params)
            raise BackendError()
        return await original(method, params)
    core.call = uncertain
    raw = raw_tx().hex()
    response = await client.post('/api/v1/wallet/broadcast',json={'hex':raw})
    result = await response.json()
    assert response.status == 502 and result['error']['code'] == 'submission_unknown'
    assert result['accepted'] is None
    assert result['txid'] == hashlib.sha256(hashlib.sha256(bytes.fromhex(raw)).digest()).digest()[::-1].hex()
    assert len(submissions) == 1


@pytest.mark.asyncio
async def test_core_rejection_is_sanitized_and_does_not_encourage_retry(wallet):
    client, core, _ = wallet
    original = core.call
    async def reject(method, params=None):
        if method == 'sendrawtransaction':
            raise BackendError('transaction_rejected')
        return await original(method,params)
    core.call = reject
    response = await client.post('/api/v1/wallet/broadcast',json={'hex':raw_tx().hex()})
    result = await response.json()
    assert response.status == 422
    assert result['error']['code'] == 'transaction_rejected'
    assert 'retry' not in result['error']['message'].lower()


@pytest.mark.asyncio
async def test_unavailable_fee_estimate_is_not_a_zero_fee(wallet):
    client, _, electrum = wallet
    original = electrum.call
    async def unavailable(method, params=None):
        return -1 if method == 'blockchain.estimatefee' else await original(method,params)
    electrum.call = unavailable
    result = await (await client.get('/api/v1/wallet/fees')).json()
    assert result['estimated_fee_per_kb_rtm'] is None and result['estimate_available'] is False


@pytest.mark.asyncio
async def test_subasset_without_parent_metadata_fails_closed(wallet):
    client, _, electrum = wallet
    original = electrum.call
    async def absent_root(method, params=None):
        if method == 'blockchain.asset.get_metadata' and params == [ASSET]:
            return {'metadata':None}
        return await original(method,params)
    electrum.call = absent_root
    response = await client.get('/api/v1/wallet/assets/'+CHILD)
    assert response.status == 503


@pytest.mark.asyncio
async def test_wallet_name_lookup_supports_qualified_subasset_name(wallet):
    client, core, _ = wallet
    response = await client.get('/api/v1/assets/by-name/ROOT%7CCHILD')
    assert response.status == 200
    assert ('getassetdetailsbyname',['ROOT|CHILD']) in core.calls
