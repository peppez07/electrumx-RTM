'''Asset records share the native UTXO database and its atomic commit boundary.'''

import json
import struct
import threading
from heapq import merge

from electrumx.lib.hash import hash_to_hex_str
from electrumx.lib.raptoreum_assets import parse_asset_script, AssetParseError
from electrumx.lib.tx_raptoreum import decode_asset_payload
from electrumx.server.db import DB

PREFIX = b'\x00rtm-assets\x01'
SCHEMA_KEY = PREFIX + b'S'
SCHEMA_VERSION = b'1'
CHECKPOINT_KEY = PREFIX + b'C'


class AssetIndexError(ValueError):
    pass


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def output_key(tx_hash, pos):
    return tx_hash + struct.pack('<I', pos)


def transfer_record(transfer, tx_hash, pos, height, tx_num, tx_type=0):
    return {'asset_id': transfer.asset_id, 'amount': transfer.amount,
            'hashX': transfer.hashX.hex(), 'tx_hash': hash_to_hex_str(tx_hash),
            'tx_pos': pos, 'height': height, 'tx_num': tx_num,
            'is_unique': transfer.is_unique, 'unique_id': transfer.unique_id,
            'unique_end': transfer.unique_end, 'script': transfer.original_script.hex(),
            'value': 0, 'source_type': tx_type,
            'spendable': transfer.canonical and tx_type != 7}


class AssetIndex:
    def __init__(self, storage):
        self.storage = storage
        self.pending = {}
        self._undo = None

    def get(self, key):
        return self.pending[key] if key in self.pending else self.storage.get(key)

    def put(self, key, value):
        if self._undo is not None and key not in self._undo:
            prior = self.get(key)
            self._undo[key] = prior
        self.pending[key] = value

    def record(self, key):
        value = self.get(key)
        return json.loads(value) if value is not None else None

    def lookup(self, tx_hash, pos):
        return self.record(PREFIX + b'O' + output_key(tx_hash, pos))

    def metadata(self, asset_id):
        return self.record(PREFIX + b'M' + asset_id.encode())

    def _output(self, record, tx_hash, pos, remove=False):
        outpoint = output_key(tx_hash, pos)
        value = None if remove else encode(record)
        self.put(PREFIX + b'O' + outpoint, value)
        self.put(PREFIX + b'A' + bytes.fromhex(record['hashX'])
                 + record['asset_id'].encode() + outpoint, value)
        key = PREFIX + b'B' + bytes.fromhex(record['hashX']) + record['asset_id'].encode()
        balance = self.record(key) or {'confirmed': 0, 'utxo_count': 0}
        sign = -1 if remove else 1
        balance['confirmed'] += sign * record['amount']
        balance['utxo_count'] += sign
        self.put(key, encode(balance))

    def apply_block(self, txs, height, first_tx_num, keep_undo=True):
        self._undo = {}
        touched = set()
        try:
            for tx_num, (tx, tx_hash) in enumerate(txs, first_tx_num):
                events = {}

                def event(record, delta, kind):
                    hashX = bytes.fromhex(record['hashX'])
                    aid = record['asset_id']
                    touched.add(hashX)
                    item = events.setdefault((hashX, aid), {'amount': 0, 'kinds': set()})
                    item['amount'] += delta
                    item['kinds'].add(kind)

                for txin in tx.inputs:
                    if not txin.is_generation():
                        record = self.lookup(txin.prev_hash, txin.prev_idx)
                        if record:
                            event(record, -record['amount'], 'spend')
                            self._output(record, txin.prev_hash, txin.prev_idx, True)
                outputs = []
                for pos, txout in enumerate(tx.outputs):
                    transfer = parse_asset_script(txout.pk_script)
                    if transfer:
                        if txout.value != 0:
                            raise AssetParseError('asset output has nonzero native value')
                        record = transfer_record(transfer, tx_hash, pos, height, tx_num,
                                                 tx.tx_type)
                        self._output(record, tx_hash, pos)
                        outputs.append(record)
                        event(record, record['amount'], 'receive')

                fields = decode_asset_payload(tx)
                if fields and tx.tx_type in (8, 9, 10):
                    aid = hash_to_hex_str(tx_hash) if tx.tx_type == 8 else fields['asset_id']
                    old = self.metadata(aid)
                    if tx.tx_type == 8:
                        if old:
                            raise AssetIndexError('duplicate asset creation')
                        meta = dict(fields, asset_id=aid, supply=0, mint_count=0)
                        if not fields['is_root']:
                            root = self.metadata(fields['root_id'])
                            if root is None:
                                raise AssetIndexError('missing root asset metadata; rebuild required')
                            meta['name'] = root['name'] + '|' + fields['name']
                        meta['creation_height'] = height
                    else:
                        if old is None:
                            raise AssetIndexError('missing asset metadata; rebuild required')
                        meta = dict(old)
                        if tx.tx_type == 9:
                            meta.update(fields)
                        else:
                            meta['supply'] += sum(o['amount'] for o in outputs
                                                  if o['asset_id'] == aid)
                            meta['mint_count'] += 1
                    meta.update(last_tx_hash=hash_to_hex_str(tx_hash), height=height)
                    self.put(PREFIX + b'M' + aid.encode(), encode(meta))
                    # Ownership/target metadata changes are discoverable even at zero balance.
                    for metadata in (old, meta):
                        if metadata:
                            for role in ('owner', 'target'):
                                key_id = bytes.fromhex(metadata[role])
                                from hashlib import sha256
                                hashX = sha256(b'\x76\xa9\x14' + key_id + b'\x88\xac').digest()[:11]
                                event({'hashX': hashX.hex(), 'asset_id': aid}, 0,
                                      {8: 'create', 9: 'update', 10: 'mint'}[tx.tx_type])

                for (hashX, aid), item in events.items():
                    self.put(PREFIX + b'D' + hashX + aid.encode(), b'1')
                    suffix = struct.pack('>Q', tx_num)
                    history = {'tx_hash': hash_to_hex_str(tx_hash), 'height': height,
                               'asset_id': aid, 'amount': item['amount'], 'tx_num': tx_num,
                               'kinds': sorted(item['kinds'])}
                    self.put(PREFIX + b'H' + hashX + aid.encode() + suffix, encode(history))
                    self.put(PREFIX + b'T' + hashX + aid.encode(), encode(history['tx_hash']))
            undo = self._undo
            self._undo = None
            if keep_undo:
                self.put(PREFIX + b'Z' + struct.pack('>I', height),
                         encode([[k.hex(), v.hex() if v is not None else None]
                                 for k, v in undo.items()]))
            return touched
        except Exception:
            undo, self._undo = self._undo, None
            for k, v in undo.items():
                self.pending[k] = v
            raise

    def rollback(self, height):
        key = PREFIX + b'Z' + struct.pack('>I', height)
        data = self.get(key)
        if data is None:
            raise AssetIndexError('missing asset undo data; rebuild required')
        for k, v in json.loads(data):
            self.put(bytes.fromhex(k), bytes.fromhex(v) if v is not None else None)
        self.put(key, None)

    def write_batch(self, batch):
        for key, value in self.pending.items():
            if value is None:
                batch.delete(key)
            else:
                batch.put(key, value)

    def committed(self):
        self.pending.clear()

    def memory_size(self):
        return sum(len(k) + (len(v) if v else 0) + 128 for k, v in self.pending.items())

    def confirmed_records(self, kind, suffix, limit=10000):
        prefix = PREFIX + kind + suffix
        result = []
        for key, value in self.storage.iterator(prefix=prefix):
            if len(result) >= limit:
                raise AssetIndexError('asset query exceeds bounded result size')
            result.append((key[len(prefix):].hex(), json.loads(value)))
        return result


class RaptoreumDB(DB):
    def __init__(self, env):
        super().__init__(env)
        self.asset_lock = threading.RLock()
        self.assets = None

    def read_utxo_state(self):
        super().read_utxo_state()
        marker = self.utxo_db.get(SCHEMA_KEY)
        if marker != SCHEMA_VERSION:
            if marker is not None or self.utxo_db.get(b'state') is not None:
                raise self.DBError('RTM asset schema missing/incompatible; use a fresh DB_DIRECTORY '
                                   'and rebuild from genesis (keep the old DB for rollback)')
        self.assets = AssetIndex(self.utxo_db)
        checkpoint = self.utxo_db.get(CHECKPOINT_KEY)
        if self.db_height >= 0 and (checkpoint is None or json.loads(checkpoint) != {
                'height': self.db_height, 'tip': hash_to_hex_str(self.db_tip)}):
            raise self.DBError('RTM asset checkpoint does not match native state; rebuild required')

    def write_utxo_state(self, batch):
        super().write_utxo_state(batch)
        # History-only flushes persist state too; mark the schema in that same
        # batch so a fresh interrupted sync is distinguishable from a legacy DB.
        batch.put(SCHEMA_KEY, SCHEMA_VERSION)

    def flush_utxo_db(self, batch, flush_data):
        super().flush_utxo_db(batch, flush_data)
        floor = self.min_undo_height(flush_data.height)
        prefix = PREFIX + b'Z'
        for key, _ in self.utxo_db.iterator(prefix=prefix):
            if struct.unpack('>I', key[-4:])[0] >= floor:
                break
            self.assets.pending[key] = None
        for key in list(self.assets.pending):
            if key.startswith(prefix) and struct.unpack('>I', key[-4:])[0] < floor:
                self.assets.pending[key] = None
        self.assets.write_batch(batch)
        batch.put(CHECKPOINT_KEY, encode({'height': self.db_height,
                                        'tip': hash_to_hex_str(self.db_tip)}))

    def flush_dbs(self, flush_data, flush_utxos, estimate_txs_remaining):
        with self.asset_lock:
            super().flush_dbs(flush_data, flush_utxos, estimate_txs_remaining)
            if flush_utxos:
                self.assets.committed()

    def flush_backup(self, flush_data, touched):
        with self.asset_lock:
            super().flush_backup(flush_data, touched)
            self.assets.committed()

    def asset_view(self, hashX, request=None):
        '''One committed chain snapshot, excluding unflushed processor changes.'''
        with self.asset_lock:
            request = request or {'mode': 'summary'}
            mode = request['mode']
            prefix = PREFIX + b'D' + hashX
            more, last, rows = False, '', []
            if mode in ('outputs', 'history'):
                ids = [request['asset_id']]
                kind = b'A' if mode == 'outputs' else b'H'
                record_prefix = PREFIX + kind + hashX + ids[0].encode()
                after = request.get('after', '')
                start = record_prefix + bytes.fromhex(after) if after else record_prefix
                iterator = self.utxo_db.iterator_from(record_prefix, start)
                for key, value in iterator:
                    suffix = key[len(record_prefix):].hex()
                    if after and suffix <= after:
                        continue
                    item = json.loads(value)
                    if mode == 'outputs' and (item['tx_hash'], item['tx_pos']) in request['spends']:
                        continue
                    if len(rows) == request['limit']:
                        more = True
                        break
                    rows.append(item)
                    last = suffix
            elif mode == 'state':
                ids = []
            else:
                after = request.get('after', '')
                start = prefix + after.encode()
                confirmed = (k[len(prefix):].decode() for k, _ in
                             self.utxo_db.iterator_from(prefix, start))
                extras = sorted(request.get('extra_ids', ()))
                ids, prior = [], None
                limit = request.get('limit', 1000)
                for aid in merge(confirmed, extras):
                    if aid <= after or aid == prior:
                        continue
                    prior = aid
                    if len(ids) == limit:
                        if mode == 'summary':
                            raise AssetIndexError('too many assets; use assets.list pagination')
                        more = True
                        break
                    ids.append(aid)
                last = ids[-1] if ids else ''
            metadata = {}
            revisions = {}
            balances = {}
            for aid in ids:
                value = self.utxo_db.get(PREFIX + b'M' + aid.encode())
                metadata[aid] = json.loads(value) if value is not None else None
                revision = self.utxo_db.get(PREFIX + b'T' + hashX + aid.encode())
                revisions[aid] = json.loads(revision) if revision else None
                value = self.utxo_db.get(PREFIX + b'B' + hashX + aid.encode())
                balances[aid] = json.loads(value) if value else {'confirmed': 0, 'utxo_count': 0}
            result = {'height': self.db_height, 'tip': hash_to_hex_str(self.db_tip),
                      'outputs': rows if mode == 'outputs' else [],
                      'history': rows if mode == 'history' else [],
                      'asset_ids': ids, 'metadata': metadata,
                      'revisions': revisions, 'balances': balances, 'more': more, 'last': last}
            return result

    def asset_history(self, hashX, asset_id):
        with self.asset_lock:
            return [v for _, v in self.assets.confirmed_records(b'H', hashX + asset_id.encode())]

    def asset_metadata(self, asset_id):
        with self.asset_lock:
            value = self.utxo_db.get(PREFIX + b'M' + asset_id.encode())
            return {'height': self.db_height, 'tip': hash_to_hex_str(self.db_tip),
                    'metadata': json.loads(value) if value is not None else None}

    def lookup_asset_utxos(self, prevouts):
        with self.asset_lock:
            result = []
            for tx_hash, pos in prevouts:
                value = self.utxo_db.get(PREFIX + b'O' + output_key(tx_hash, pos))
                result.append(json.loads(value) if value is not None else None)
            return result

    def asset_outpoints(self, utxos):
        with self.asset_lock:
            return {(u.tx_hash, u.tx_pos) for u in utxos
                    if self.utxo_db.get(PREFIX + b'O' + output_key(u.tx_hash, u.tx_pos))}
