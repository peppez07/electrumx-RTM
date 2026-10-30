'''Additive RTM asset extension; existing coin responses retain native units.'''

import base64
import asyncio
import binascii
import hashlib
import json
import hmac
import secrets

from aiorpcx import RPCError, run_in_thread

from electrumx.lib.raptoreum_assets import valid_asset_id
from electrumx.lib.raptoreum_future import future_spendability
from electrumx.server.raptoreum_db import AssetIndexError, encode
from electrumx.server.session import DashElectrumX, BAD_REQUEST, scripthash_to_hashX

RETRY = -32001


def public_record(record):
    result = dict(record)
    for key in ('amount', 'supply', 'unique_id', 'unique_end'):
        if key in result and result[key] is not None:
            result[key] = str(result[key])
    result.pop('hashX', None)
    result.pop('tx_num', None)
    return result


class RaptoreumElectrumX(DashElectrumX):
    MAX_ASSET_SUBSCRIPTIONS = 100

    def __init__(self, *args, **kwargs):
        self.asset_subs = {}
        super().__init__(*args, **kwargs)

    def set_request_handlers(self, ptuple):
        super().set_request_handlers(ptuple)
        self.request_handlers.update({
            'blockchain.asset.get_metadata': self.asset_get_metadata,
            'blockchain.scripthash.assets.get_balance': self.assets_get_balance,
            'blockchain.scripthash.assets.list': self.assets_list,
            'blockchain.scripthash.assets.listunspent': self.assets_listunspent,
            'blockchain.scripthash.assets.get_history': self.assets_get_history,
            'blockchain.scripthash.assets.subscribe': self.assets_subscribe,
            'blockchain.scripthash.assets.unsubscribe': self.assets_unsubscribe,
        })

    async def server_features_async(self):
        result = await super().server_features_async()
        result['rtm_assets'] = {
            'version': 1, 'ready': self.mempool.asset_ready,
            'indexed_height': self.db.db_height,
            'indexed_tip': self.db.db_tip[::-1].hex(),
            'amount_encoding': 'atomic_decimal_string',
            'asset_destinations': ['p2pkh'], 'future_outputs_selectable': True,
            'future_maturity': 'next_block_height_or_core_adjusted_time',
            'methods': sorted(k for k in self.request_handlers
                              if '.assets.' in k or k == 'blockchain.asset.get_metadata'),
        }
        return result

    def sub_count(self):
        return super().sub_count() + len(self.asset_subs)

    @staticmethod
    def _asset_id(asset_id):
        if not valid_asset_id(asset_id):
            raise RPCError(BAD_REQUEST, 'expected lowercase 64-character asset ID')
        return asset_id

    @staticmethod
    def _limit(limit):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise RPCError(BAD_REQUEST, 'limit must be an integer from 1 to 1000')
        return limit

    async def _view(self, hashX, request=None):
        async with self.mempool.lock:
            if not self.mempool.asset_ready:
                raise RPCError(RETRY, 'asset mempool is synchronizing; retry')
            try:
                request = dict(request or {'mode': 'summary'})
                request['extra_ids'] = {
                    o['asset_id'] for o in self.mempool.asset_outputs.values()
                    if o['hashX'] == hashX.hex()}
                request['extra_ids'].update(aid for h, aid in self.mempool.asset_history
                                            if h == hashX.hex())
                if request['mode'] == 'outputs':
                    request['spends'] = set(self.mempool.asset_spends)
                view = await run_in_thread(self.db.asset_view, hashX, request)
            except AssetIndexError as e:
                raise RPCError(BAD_REQUEST, str(e)) from e
            if (view['height'], view['tip']) != self.mempool.asset_tip:
                raise RPCError(RETRY, 'indexed chain changed; retry')
            view.update(generation=self.mempool.asset_generation,
                        pending_outputs=list(self.mempool.asset_outputs.values()),
                        pending_inputs=list(self.mempool.asset_inputs),
                        spends=set(self.mempool.asset_spends),
                        events=list(self.mempool.asset_events),
                        pending_history=dict(self.mempool.asset_history))
            return view

    @staticmethod
    def _balances(view, hashX):
        if 'balances' in view:
            result = {aid: {'confirmed': b['confirmed'], 'unconfirmed': 0}
                      for aid, b in view['balances'].items()}
        else:
            result = {}
            for record in view['outputs']:
                item = result.setdefault(record['asset_id'], {'confirmed': 0, 'unconfirmed': 0})
                item['confirmed'] += record['amount']
        for records, sign in ((view['pending_outputs'], 1), (view['pending_inputs'], -1)):
            for record in records:
                if record['hashX'] == hashX.hex():
                    item = result.setdefault(record['asset_id'], {'confirmed': 0, 'unconfirmed': 0})
                    item['unconfirmed'] += sign * record['amount']
        return result

    @staticmethod
    def _indexed(view):
        return {'indexed_height': view['height'], 'indexed_tip': view['tip'],
                'mempool_generation': view['generation']}

    @staticmethod
    def _state(view, query):
        return hashlib.sha256(encode([view['height'], view['tip'], view['generation'], query])).hexdigest()

    def _position(self, cursor, view, query):
        if cursor is None:
            return None
        if not isinstance(cursor, str) or len(cursor) > 1024:
            raise RPCError(BAD_REQUEST, 'invalid asset cursor')
        try:
            data = json.loads(base64.b64decode(cursor.encode('ascii'), altchars=b'-_', validate=True))
            if not isinstance(data, dict):
                raise ValueError
            signature = data.pop('signature')
            if not hmac.compare_digest(signature, self._sign_cursor(data)):
                raise ValueError
            position = data['position']
            if data['state'] != self._state(view, query):
                raise RPCError(RETRY, 'stale asset cursor; restart pagination')
            return position
        except (ValueError, KeyError, TypeError, UnicodeError, binascii.Error) as e:
            raise RPCError(BAD_REQUEST, 'invalid asset cursor') from e

    def _sign_cursor(self, data):
        if not hasattr(self, '_cursor_secret'):
            self._cursor_secret = secrets.token_bytes(32)
        return hmac.new(self._cursor_secret, encode(data), hashlib.sha256).hexdigest()

    def _page(self, items, position, view, query):
        cursor = None
        if position is not None:
            data = {'state': self._state(view, query), 'position': position}
            data['signature'] = self._sign_cursor(data)
            cursor = base64.urlsafe_b64encode(encode(data)).decode()
        self.bump_cost(1 + len(items) / 50)
        return dict(self._indexed(view), items=items, cursor=cursor)

    async def asset_get_metadata(self, asset_id):
        self._asset_id(asset_id)
        async with self.mempool.lock:
            if not self.mempool.asset_ready:
                raise RPCError(RETRY, 'asset mempool is synchronizing; retry')
            result = await run_in_thread(self.db.asset_metadata, asset_id)
            if (result['height'], result['tip']) != self.mempool.asset_tip:
                raise RPCError(RETRY, 'indexed chain changed; retry')
        self.bump_cost(1)
        return {'metadata': public_record(result['metadata']) if result['metadata'] else None,
                'indexed_height': result['height'], 'indexed_tip': result['tip']}

    async def assets_get_balance(self, scripthash):
        hashX = scripthash_to_hashX(scripthash)
        view = await self._view(hashX)
        balances = self._balances(view, hashX)
        if len(balances) > 1000:
            raise RPCError(BAD_REQUEST, 'too many assets; use assets.list pagination')
        self.bump_cost(1 + len(view['outputs']) / 50)
        return dict(self._indexed(view), assets={
            aid: {k: str(v) for k, v in balance.items()}
            for aid, balance in sorted(balances.items())})

    async def assets_list(self, scripthash, cursor=None, limit=100):
        self._limit(limit)
        hashX = scripthash_to_hashX(scripthash)
        initial = await self._view(hashX, {'mode': 'state'})
        query = ['list', hashX.hex()]
        after = self._position(cursor, initial, query) or ''
        if not isinstance(after, str) or (after and not valid_asset_id(after)):
            raise RPCError(BAD_REQUEST, 'invalid asset cursor position')
        view = await self._view(hashX, {'mode': 'discovery', 'after': after, 'limit': limit})
        if self._state(initial, query) != self._state(view, query):
            raise RPCError(RETRY, 'indexed chain changed; retry')
        balances = self._balances(view, hashX)
        items = [{'asset_id': aid, 'confirmed': str(balances.get(aid, {}).get('confirmed', 0)),
                  'unconfirmed': str(balances.get(aid, {}).get('unconfirmed', 0))}
                 for aid in view['asset_ids']]
        return self._page(items, view['last'] if view['more'] else None, view, query)

    async def assets_listunspent(self, scripthash, asset_id, cursor=None, limit=100):
        return await self._records_page('outputs', scripthash, asset_id, cursor, limit)

    async def assets_get_history(self, scripthash, asset_id, cursor=None, limit=100):
        return await self._records_page('history', scripthash, asset_id, cursor, limit)

    async def _records_page(self, mode, scripthash, asset_id, cursor, limit):
        self._asset_id(asset_id)
        self._limit(limit)
        hashX = scripthash_to_hashX(scripthash)
        initial = await self._view(hashX, {'mode': 'state'})
        query = [mode, hashX.hex(), asset_id]
        position = self._position(cursor, initial, query) or {'phase': 'confirmed', 'after': ''}
        if not isinstance(position, dict) or position.get('phase') not in ('confirmed', 'pending'):
            raise RPCError(BAD_REQUEST, 'invalid asset cursor position')
        phase, after = position['phase'], position.get('after')
        if phase == 'confirmed' and (not isinstance(after, str) or
                (after and (len(after) != (72 if mode == 'outputs' else 16)
                            or any(c not in '0123456789abcdef' for c in after)))):
            raise RPCError(BAD_REQUEST, 'invalid asset cursor position')
        if phase == 'pending' and (type(after) is not int or after < 0):
            raise RPCError(BAD_REQUEST, 'invalid asset cursor position')
        request = {'mode': mode, 'asset_id': asset_id, 'after': after, 'limit': limit}
        view = await self._view(hashX, request if phase == 'confirmed' else {'mode': 'state'})
        if self._state(view, query) != self._state(initial, query):
            raise RPCError(RETRY, 'indexed chain changed; retry')
        items = list(view[mode]) if phase == 'confirmed' else []
        next_position = None
        if phase == 'confirmed' and view['more']:
            next_position = {'phase': 'confirmed', 'after': view['last']}
        else:
            if mode == 'history':
                pending = view['pending_history'].get((hashX.hex(), asset_id), [])
            else:
                pending = [o for o in view['pending_outputs'] if o['hashX'] == hashX.hex()
                           and o['asset_id'] == asset_id
                           and (o['tx_hash'], o['tx_pos']) not in view['spends']]
            pending = sorted(pending, key=lambda o: (o['tx_hash'], o.get('tx_pos', 0)))
            offset = after if phase == 'pending' else 0
            room = limit-len(items)
            items += pending[offset:offset+room]
            if len(pending) > offset+room:
                next_position = {'phase': 'pending', 'after': offset+room}
        context = None
        if mode == 'outputs':
            if any(o.get('future') is not None and o.get('height', 0) > 0 for o in items):
                try:
                    context = await asyncio.wait_for(self.daemon_request('future_spend_context'), 10)
                except Exception as error:
                    raise RPCError(RETRY, 'future maturity context unavailable; retry') from error
                if (not isinstance(context, dict) or
                        (context.get('height'), context.get('tip')) != (view['height'], view['tip']) or
                        type(context.get('adjusted_time')) is not int or context['adjusted_time'] < 0 or
                        (self.db.db_height, self.db.db_tip[::-1].hex()) != (view['height'], view['tip']) or
                        self.mempool.asset_tip != (view['height'], view['tip']) or
                        self.mempool.asset_generation != view['generation'] or not self.mempool.asset_ready):
                    raise RPCError(RETRY, 'chain or mempool changed during future maturity check; retry')
            items = [future_spendability(o, view['height'], context['adjusted_time'] if context else None)
                     for o in items]
        result = self._page([public_record(o) for o in items], next_position, view, query)
        if context is not None:
            result['spendability_context'] = dict(context, spend_height=context['height']+1,
                time_source='core_offset_and_synchronized_host_clock')
        return result

    async def asset_status(self, hashX):
        view = await self._view(hashX)
        balances = self._balances(view, hashX)
        active = {aid for aid, b in balances.items() if b['confirmed'] or b['unconfirmed']}
        active.update(aid for h, aid in view['pending_history'] if h == hashX.hex())
        relevant_metadata = {aid: view['metadata'].get(aid) for aid in sorted(active)}
        pending = {aid: view['pending_history'].get((hashX.hex(), aid), []) for aid in sorted(active)}
        events = [e for e in view['events'] if e['asset_id'] in active]
        outputs = [o for o in view['pending_outputs']
                   if o['hashX'] == hashX.hex() and (o['tx_hash'], o['tx_pos']) not in view['spends']]
        outputs.sort(key=lambda o: (o['tx_hash'], o['tx_pos']))
        events.sort(key=lambda e: e['tx_hash'])
        for rows in pending.values():
            rows.sort(key=lambda o: o['tx_hash'])
        if not view['asset_ids'] and not active:
            return None
        self.bump_cost(1 + (len(outputs) + len(view['asset_ids'])) / 50)
        return hashlib.sha256(b'rtm-assets-v1:' + encode([
            balances, outputs, view.get('revisions', {}), relevant_metadata, pending, events])).hexdigest()

    async def assets_subscribe(self, scripthash):
        hashX = scripthash_to_hashX(scripthash)
        if hashX not in self.asset_subs and len(self.asset_subs) >= self.MAX_ASSET_SUBSCRIPTIONS:
            raise RPCError(BAD_REQUEST, 'too many asset subscriptions')
        status = await self.asset_status(hashX)
        self.asset_subs[hashX] = (scripthash, status)
        return status

    async def assets_unsubscribe(self, scripthash):
        self.bump_cost(0.1)
        return self.asset_subs.pop(scripthash_to_hashX(scripthash), None) is not None

    async def notify(self, touched, height_changed):
        await super().notify(touched, height_changed)
        generation = self.mempool.asset_generation
        if not height_changed and getattr(self, '_asset_notified_generation', None) == generation:
            return
        complete = True
        # Recheck all bounded subscriptions: metadata updates affect holders whose
        # native script hash need not be touched by the update transaction.
        for hashX, (alias, old_status) in list(self.asset_subs.items()):
            try:
                status = await self.asset_status(hashX)
            except RPCError:
                complete = False
                continue
            if status != old_status:
                self.asset_subs[hashX] = alias, status
                await self.send_notification('blockchain.scripthash.assets.subscribe',
                                             (alias, status))
        if complete:
            self._asset_notified_generation = generation

    async def hashX_listunspent(self, hashX):
        result = await super().hashX_listunspent(hashX)
        utxos = await self.db.all_utxos(hashX)
        excluded = await run_in_thread(self.db.asset_outpoints, utxos)
        excluded = {(h[::-1].hex(), pos) for h, pos in excluded}
        excluded.update((o['tx_hash'], o['tx_pos']) for o in self.mempool.asset_outputs.values())
        # Asset outputs always carry zero RTM; filtering zero-value outputs also
        # protects native coin selection during confirmation/refresh races.
        return [o for o in result if o['value'] > 0
                and (o['tx_hash'], o['tx_pos']) not in excluded]
