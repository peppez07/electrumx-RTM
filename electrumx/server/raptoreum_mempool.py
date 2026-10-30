'''Asset mempool overlay. Native fee and dependency processing stay in MemPool.'''

from collections import defaultdict

import attr

from electrumx.lib.hash import hash_to_hex_str
from electrumx.lib.raptoreum_assets import parse_asset_script, AssetParseError
from electrumx.lib.tx_raptoreum import decode_asset_payload
from electrumx.server.mempool import MemPool, MemPoolTx, DBSyncError
from electrumx.server.raptoreum_db import transfer_record


@attr.s(slots=True)
class RaptoreumMemPoolTx(MemPoolTx):
    asset_tx = attr.ib(default=None)


class RaptoreumMemPool(MemPool):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.asset_outputs = {}
        self.asset_inputs = []
        self.asset_spends = set()
        self.asset_events = []
        self.asset_history = {}
        self.asset_ready = False
        self.asset_tip = None
        self.asset_generation = 0
        self._asset_fingerprint = None

    def _make_mempool_tx(self, tx, prevouts, out_pairs, size):
        return RaptoreumMemPoolTx(prevouts, None, out_pairs, 0, size, tx)

    async def _process_mempool(self, all_hashes, touched, mempool_height):
        self.asset_ready = False
        tip = self.api.db_tip()
        result = await super()._process_mempool(all_hashes, touched, mempool_height)
        outputs = {}
        metadata = []
        history = defaultdict(dict)
        inputs = []
        spends = set()

        def event(record, delta, tx_hash, height, kind):
            key = record['hashX'], record['asset_id']
            item = history[key].setdefault(tx_hash, {
                'tx_hash': tx_hash, 'asset_id': record['asset_id'], 'height': height,
                'amount': 0, 'kinds': []})
            item['amount'] += delta
            if kind not in item['kinds']:
                item['kinds'].append(kind)
            touched.add(bytes.fromhex(record['hashX']))

        for tx_hash, item in self.txs.items():
            tx = item.asset_tx
            fields = decode_asset_payload(tx)
            height = -int(any(p[0] in self.txs for p in item.prevouts))
            for pos, txout in enumerate(tx.outputs):
                transfer = parse_asset_script(txout.pk_script)
                if transfer:
                    if txout.value != 0:
                        raise AssetParseError('asset output has nonzero native value')
                    record = transfer_record(transfer, tx_hash, pos, height, -1, tx.tx_type,
                        {'maturity':fields['maturity'], 'lock_time':fields['lock_time']}
                        if tx.tx_type == 7 and pos == fields['lock_output_index'] else None)
                    outputs[tx_hash, pos] = record
                    event(record, record['amount'], record['tx_hash'], height, 'receive')
            if fields and tx.tx_type in (8, 9, 10):
                aid = hash_to_hex_str(tx_hash) if tx.tx_type == 8 else fields['asset_id']
                metadata.append({'asset_id': aid, 'tx_hash': hash_to_hex_str(tx_hash),
                                 'height': height, 'fields': fields})
                for role in ('owner', 'target'):
                    if role in fields:
                        script = b'\x76\xa9\x14' + bytes.fromhex(fields[role]) + b'\x88\xac'
                        event({'asset_id': aid, 'hashX': self.coin.hashX_from_script(script).hex()},
                              0, hash_to_hex_str(tx_hash), height, 'metadata')

        prevouts = {p for item in self.txs.values() for p in item.prevouts}
        external = sorted(p for p in prevouts if p[0] not in self.txs)
        confirmed = dict(zip(external, await self.api.lookup_asset_utxos(external)))
        for tx_hash, item in self.txs.items():
            height = -int(any(p[0] in self.txs for p in item.prevouts))
            for prevout in item.prevouts:
                record = outputs.get(prevout) or confirmed.get(prevout)
                if record:
                    inputs.append(record)
                    spends.add((record['tx_hash'], record['tx_pos']))
                    event(record, -record['amount'], hash_to_hex_str(tx_hash), height, 'spend')
        chain_ready = await self.api.asset_chain_ready()
        if self.api.db_height() != mempool_height or self.api.db_tip() != tip:
            raise DBSyncError
        fingerprint = tip, frozenset(self.txs)
        if fingerprint != self._asset_fingerprint:
            self.asset_generation += 1
        self._asset_fingerprint = fingerprint
        self.asset_outputs, self.asset_inputs, self.asset_spends = outputs, inputs, spends
        self.asset_events = metadata
        self.asset_history = {k: list(v.values()) for k, v in history.items()}
        self.asset_tip = mempool_height, hash_to_hex_str(tip)
        self.asset_ready = chain_ready
        return result
