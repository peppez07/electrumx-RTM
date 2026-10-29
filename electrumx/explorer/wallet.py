'''Non-custodial wallet data and opt-in relay of client-signed transactions.

This module never holds keys, builds transactions, or invokes Core wallet RPCs.
'''

import json
import hashlib
import re
from decimal import Decimal

from aiohttp import web

from electrumx.lib.tx_raptoreum import DeserializerRaptoreum
from .clients import BackendError
from .service import APIError, hash_id, json_response, number

CAPABILITIES_PATH = '/api/v1/wallet/capabilities'
POST_PATHS = frozenset({'/api/v1/wallet/decode', '/api/v1/wallet/broadcast'})
MAX_RAW_BYTES = 100000
SUBMITTED_TXID = web.RequestKey('submitted_txid', str) if hasattr(web, 'RequestKey') else 'submitted_txid'
BROADCAST_STARTED = web.RequestKey('broadcast_started', bool) if hasattr(web, 'RequestKey') else 'broadcast_started'


def unknown_submission(txid):
    return json_response({'accepted':None, 'txid':txid, 'error':{
        'code':'submission_unknown',
        'message':'Submission outcome is unknown. Check this transaction ID before submitting it again.'}}, status=502)


class WalletAPI:
    def __init__(self, explorer, enabled):
        self.explorer, self.enabled = explorer, enabled

    async def capabilities(self, request):
        status = await self.explorer.status()
        return json_response({'version':1, 'network':'mainnet', 'ready':status['ready'],
            'signing':'client_only', 'transaction_building':'client_only',
            'asset_types':['root', 'subasset', 'unique'], 'asset_destinations':['p2pkh'],
            'amount_encoding':'atomic_decimal_string', 'atomic_scale':'100000000',
            'broadcast_enabled':self.enabled, 'decode_enabled':self.enabled,
            'acceptance_preflight':False, 'max_raw_transaction_bytes':MAX_RAW_BYTES,
            'asset_lifecycle_relay':['transfer', 'create', 'mint', 'update', 'subasset_create'] if self.enabled else [],
            'note':'Relay capabilities do not construct or sign transactions. Core validates submissions.'})

    async def raw_body(self, request):
        if not self.enabled:
            raise APIError(403, 'wallet_api_disabled', 'Signed transaction relay is not enabled on this server.')
        if request.query:
            raise APIError(400, 'invalid_input', 'Wallet POST routes do not accept query overrides.')
        if request.content_type != 'application/json':
            raise APIError(415, 'invalid_content_type', 'Send application/json with exactly one hex field.')
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeError):
            raise APIError(400, 'invalid_input', 'Invalid JSON body.') from None
        if not isinstance(body, dict) or set(body) != {'hex'}:
            raise APIError(400, 'invalid_input', 'Only the public signed transaction hex is accepted; never send keys or seed phrases.')
        raw = body['hex']
        if not isinstance(raw, str) or not raw or len(raw) % 2 or len(raw) > MAX_RAW_BYTES*2 or not re.fullmatch('[0-9a-fA-F]+', raw):
            raise APIError(400, 'invalid_transaction', 'Expected bounded raw transaction hexadecimal.')
        try:
            binary = bytes.fromhex(raw)
            parser = DeserializerRaptoreum(binary)
            tx = parser.read_tx()
            if parser.cursor != len(binary) or not tx.inputs or not tx.outputs:
                raise ValueError()
        except (ValueError, IndexError):
            raise APIError(400, 'invalid_transaction', 'Transaction framing is invalid or has trailing data.') from None
        return raw

    async def decode(self, request):
        raw = await self.raw_body(request)
        decoded = await self.explorer.core.call('decoderawtransaction', [raw])
        return json_response({'transaction':decoded, 'validation_level':'syntax_only',
            'signature_verified':False, 'acceptance_checked':False,
            'note':'Core 2.0.3.01 has no testmempoolaccept RPC. Decoding does not validate signatures or spendability.'})

    async def broadcast(self, request):
        raw = await self.raw_body(request)
        # Keep Core's default fee ceiling and relay/consensus protections. No
        # allowhighfees, maxfeerate=0, bypasslimits, keys, or wallet-unlock calls.
        local_txid = hashlib.sha256(hashlib.sha256(bytes.fromhex(raw)).digest()).digest()[::-1].hex()
        request[SUBMITTED_TXID] = local_txid
        request[BROADCAST_STARTED] = True
        try:
            txid = await self.explorer.core.call('sendrawtransaction', [raw])
        except BackendError as error:
            if error.code in ('invalid_transaction', 'transaction_rejected', 'missing_inputs', 'already_known'):
                raise
            return unknown_submission(local_txid)
        if not isinstance(txid, str) or not re.fullmatch('[0-9a-fA-F]{64}', txid):
            return unknown_submission(local_txid)
        return json_response({'txid':txid, 'accepted':True,
                              'note':'Accepted by the local Core; confirmation is not guaranteed.'})

    async def fees(self, request):
        blocks = number(request.query.get('blocks'), default=6, minimum=1, maximum=1000)
        estimate = Decimal(str(await self.explorer.electrum.call('blockchain.estimatefee', [blocks])))
        relay = Decimal(str(await self.explorer.electrum.call('blockchain.relayfee')))
        if not estimate.is_finite() or not relay.is_finite() or relay < 0:
            raise BackendError()
        return json_response({'target_blocks':blocks, 'estimate_available':estimate >= 0,
            'estimated_fee_per_kb_rtm':format(estimate,'f') if estimate >= 0 else None,
            'relay_fee_per_kb_rtm':format(relay,'f')})

    async def policy(self, request):
        values = await self.explorer.core.call('spork', ['show'])
        active = await self.explorer.core.call('spork', ['active'])
        key = 'SPORK_22_SPECIAL_TX_FEE'
        value = values.get(key)
        if type(value) is not int or not 0 <= value < 2**63 or type(active.get(key)) is not bool:
            raise BackendError('unsupported')
        fee = (value >> 8) & 0xff if active[key] else 0
        return json_response({'special_tx_fee_rtm':str(fee),
            'special_tx_fee_atomic':str(fee*100000000), 'special_tx_fee_active':active[key],
            'subassets_max_depth':1, 'subasset_name_separator':'|',
            'subasset_creation_requires_root_owner_signature':True,
            'note':'Special transaction fee is additional to normal transaction fees. Wallets sign all authorizations locally.'})

    async def asset(self, request):
        aid = hash_id(request.match_info['id'])
        result = await self.explorer.electrum.call('blockchain.asset.get_metadata', [aid])
        metadata = result.get('metadata')
        if not metadata:
            raise APIError(404, 'not_found', 'Asset is not present in the confirmed index.')
        core_metadata = await self.explorer.core.call('getassetdetailsbyid', [aid])
        root, root_core = None, None
        if metadata.get('is_root') is False:
            root_id = metadata.get('root_id', '')
            try:
                root_id = hash_id(root_id)
            except APIError:
                raise BackendError('synchronizing') from None
            root = (await self.explorer.electrum.call('blockchain.asset.get_metadata', [root_id])).get('metadata')
            if not root or root.get('is_root') is not True:
                raise BackendError('synchronizing')
            root_core = await self.explorer.core.call('getassetdetailsbyid', [root_id])
        return json_response({'metadata':metadata, 'core_metadata':core_metadata,
            'root_metadata':root, 'root_core_metadata':root_core,
            'subasset_creation_requires_root_owner_signature':True,
            'signing':'client_only', 'indexed_height':result.get('indexed_height'),
            'indexed_tip':result.get('indexed_tip')})

    async def raw_transaction(self, request):
        txid = hash_id(request.match_info['id'])
        return json_response({'txid':txid,
            'hex':await self.explorer.core.call('getrawtransaction', [txid, False])})

    async def proof(self, request):
        txid = hash_id(request.match_info['id'])
        height = request.query.get('height')
        if height is None:
            raise APIError(400, 'invalid_input', 'A confirmed block height is required.')
        return json_response(await self.explorer.electrum.call(
            'blockchain.transaction.get_merkle', [txid, number(height)]))

    async def by_name(self, request):
        name = request.match_info['name']
        if not name or len(name.encode()) > 128 or any(ord(c) < 32 for c in name):
            raise APIError(400, 'invalid_input', 'Invalid root or subasset name.')
        return json_response(await self.explorer.core.call('getassetdetailsbyname', [name]))


def register_wallet(app, explorer, enabled):
    api = WalletAPI(explorer, enabled)
    app.router.add_get(CAPABILITIES_PATH, api.capabilities)
    app.router.add_get('/api/v1/wallet/fees', api.fees)
    app.router.add_get('/api/v1/wallet/asset-policy', api.policy)
    app.router.add_get('/api/v1/wallet/assets/{id}', api.asset)
    app.router.add_post('/api/v1/wallet/decode', api.decode)
    app.router.add_post('/api/v1/wallet/broadcast', api.broadcast)
    app.router.add_get('/api/v1/transactions/{id}/raw', api.raw_transaction)
    app.router.add_get('/api/v1/transactions/{id}/proof', api.proof)
    # Register this before the generic /assets/{id} route.
    app.router.add_get('/api/v1/assets/by-name/{name}', api.by_name)
