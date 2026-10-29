'''RTM transaction framing, independent of the conflicting Dash DIP5 types.'''

import struct
from collections import namedtuple

from electrumx.lib.tx import Deserializer
from electrumx.lib.util import pack_varbytes, pack_varint
from electrumx.lib.raptoreum_assets import Reader, AssetParseError, valid_asset_id


class RaptoreumTx(namedtuple('RaptoreumTx',
                           'version inputs outputs locktime tx_type extra_payload')):
    def serialize(self):
        header = (self.version & 0xffff) | (self.tx_type << 16)
        raw = (struct.pack('<I', header) + pack_varint(len(self.inputs))
               + b''.join(i.serialize() for i in self.inputs)
               + pack_varint(len(self.outputs))
               + b''.join(o.serialize() for o in self.outputs)
               + struct.pack('<I', self.locktime))
        if self.version == 3 and self.tx_type:
            raw += pack_varbytes(self.extra_payload)
        return raw


class DeserializerRaptoreum(Deserializer):
    def _read_nbytes(self, size):
        end = self.cursor + size
        if size < 0 or end > self.binary_length:
            raise ValueError('truncated Raptoreum transaction')
        raw = self.binary[self.cursor:end]
        self.cursor = end
        return raw

    def _number(self, fmt):
        return struct.unpack('<' + fmt, self._read_nbytes(struct.calcsize('<' + fmt)))[0]

    def _read_le_uint32(self):
        return self._number('I')

    def _read_le_int64(self):
        return self._number('q')

    def _read_varint(self):
        first = self._number('B')
        if first < 253:
            return first
        fmt, minimum = {253: ('H', 253), 254: ('I', 65536),
                        255: ('Q', 4294967296)}[first]
        size = self._number(fmt)
        if size < minimum:
            raise ValueError('nonminimal CompactSize')
        return size

    def read_tx(self):
        header = self._read_le_uint32()
        version, tx_type = header & 0xffff, header >> 16
        if version >= 0x8000:
            version -= 0x10000
        inputs = self._read_inputs()
        outputs = self._read_outputs()
        locktime = self._read_le_uint32()
        payload = self._read_varbytes() if version == 3 and tx_type else b''
        return RaptoreumTx(version, inputs, outputs, locktime, tx_type, payload)


def decode_asset_payload(tx):
    '''Decode network serialization, including signatures omitted from SER_GETHASH.'''
    if tx.tx_type not in (7, 8, 9, 10):
        return None
    r = Reader(tx.extra_payload)
    version = r.number('H')
    if tx.version != 3 or version != 1:
        raise AssetParseError('unsupported RTM special payload version')
    fields = {'version': version, 'tx_type': tx.tx_type}

    def reference_hash():
        raw = r.varbytes()
        fields['reference_hash_hex'] = raw.hex()
        return raw.decode('utf-8', errors='replace')
    if tx.tx_type == 7:
        fields.update(maturity=r.number('i'), lock_time=r.number('i'),
                      lock_output_index=r.number('H'), fee=r.number('H'),
                      updatable=bool(r.number('B')))
    elif tx.tx_type == 10:
        fields.update(asset_id=r.string(), fee=r.number('H'))
        fields['inputs_hash'] = r.take(32).hex()
        fields['signature'] = r.varbytes().hex()
        r.finished()
        if not valid_asset_id(fields['asset_id']):
            raise AssetParseError('invalid mint asset ID')
        return fields
    else:
        if tx.tx_type == 8:
            fields.update(name=r.string(), updatable=bool(r.number('B')),
                          is_unique=bool(r.number('B')), max_mint_count=r.number('H'),
                          decimal_point=r.number('B'), reference_hash=reference_hash())
        else:
            fields.update(asset_id=r.string(), updatable=bool(r.number('B')),
                          reference_hash=reference_hash())
        fields.update(fee=r.number('H'), distribution_type=r.number('B'),
                      target=r.take(20).hex(), issue_frequency=r.number('B'))
        if tx.tx_type == 9:
            fields['max_mint_count'] = r.number('H')
        fields.update(amount=r.number('q'), owner=r.take(20).hex(),
                      collateral=r.take(20).hex())
        if tx.tx_type == 8:
            fields['is_root'] = bool(r.number('B'))
            if not fields['is_root']:
                fields['root_id'] = r.string()
                fields['signature'] = r.varbytes().hex()
    fields.update(ex_chain_type=r.number('H'), external_payout_script=r.varbytes().hex(),
                  external_txid=r.take(32)[::-1].hex(), external_confirmations=r.number('H'),
                  inputs_hash=r.take(32).hex())
    if tx.tx_type == 9:
        fields['signature'] = r.varbytes().hex()
    r.finished()
    return fields
