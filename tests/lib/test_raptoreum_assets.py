import hashlib
import struct

import pytest

from electrumx.lib.raptoreum_assets import AssetParseError, parse_asset_script
from electrumx.lib.tx_raptoreum import DeserializerRaptoreum, decode_asset_payload


ASSET = '12' * 32
P2PKH = bytes.fromhex('76a914' + '11' * 20 + '88ac')


def varbytes(value):
    assert len(value) < 253
    return bytes([len(value)]) + value


def asset_script(amount=123000000, unique=None, recipient=P2PKH):
    data = b'rtm' + varbytes(ASSET.encode()) + bytes([unique is not None])
    if unique is not None:
        data += struct.pack('<Q', unique)
    data += struct.pack('<q', amount)
    return recipient + b'\xbc\x4c' + bytes([len(data)]) + data + b'\x75'


def raw_tx(tx_type=0, payload=b'', outputs=(), version=3):
    raw = struct.pack('<HH', version, tx_type) + b'\x01'
    raw += b'\0' * 32 + b'\xff' * 4 + b'\x01\x00' + b'\xff' * 4
    raw += bytes([len(outputs)])
    for value, script in outputs:
        raw += struct.pack('<q', value) + varbytes(script)
    raw += b'\0' * 4
    if version == 3 and tx_type:
        raw += varbytes(payload)
    return raw


def new_asset_payload(root=True, unique=False):
    # Network layout from Core 2.0.3.01 src/evo/providertx.h, independent of parser.
    raw = struct.pack('<H', 1) + varbytes(b'EXAMPLE')
    raw += bytes([1, unique]) + struct.pack('<HB', 10, 2) + varbytes(b'ipfs-ref')
    raw += struct.pack('<HB', 10, 0) + b'\x22' * 20
    raw += struct.pack('<Bq', 0, 100000000) + b'\x33' * 20 + b'\0' * 20
    raw += bytes([root])
    if not root:
        raw += varbytes(ASSET.encode()) + varbytes(b'signature')
    raw += struct.pack('<H', 0) + varbytes(b'') + b'\0' * 32
    return raw + struct.pack('<H', 0) + b'\x44' * 32


def update_asset_payload():
    raw = b'\x01\0' + varbytes(ASSET.encode()) + b'\x01' + varbytes(b'new-ref')
    raw += struct.pack('<HB', 12, 0) + b'\x55'*20
    raw += struct.pack('<BHq', 0, 15, 200000000) + b'\x66'*20 + bytes(20)
    raw += struct.pack('<H', 0) + varbytes(b'') + bytes(32)
    return raw + struct.pack('<H', 0) + bytes(32) + varbytes(b'signature')


def test_asset_recipient_and_atomic_amount():
    transfer = parse_asset_script(asset_script())
    assert transfer.asset_id == ASSET
    assert transfer.amount == 123000000
    assert transfer.recipient_script == P2PKH
    assert transfer.hashX == hashlib.sha256(P2PKH).digest()[:11]
    assert transfer.canonical
    assert parse_asset_script(P2PKH) is None


def test_unique_interval_does_not_lose_precision():
    start = 2**53 + 17
    transfer = parse_asset_script(asset_script(300000000, start))
    assert (transfer.unique_id, transfer.unique_end) == (start, start + 3)


def test_core_recognized_noncanonical_script_is_visible_but_not_selectable():
    script = asset_script()
    # Core's detector and deserializer ignore the push's claimed size.
    altered = script[:27] + b'\x01' + script[28:]
    transfer = parse_asset_script(altered)
    assert transfer.asset_id == ASSET
    assert not transfer.canonical


def test_core_detector_does_not_fall_through_after_partial_magic_match():
    script = asset_script()
    script = script[:27] + b'r' + script[28:]
    assert parse_asset_script(script) is None


@pytest.mark.parametrize('script', [asset_script()[:-7], asset_script(-1)])
def test_recognized_invalid_asset_fails_explicitly(script):
    with pytest.raises(AssetParseError):
        parse_asset_script(script)


def test_unusual_unique_intervals_are_visible_but_not_selectable():
    assert not parse_asset_script(asset_script(1, 0)).canonical
    assert not parse_asset_script(asset_script(200000000, 2**64-1)).canonical


def test_reference_hash_retains_non_utf8_bytes():
    payload = new_asset_payload().replace(b'\x08ipfs-ref', b'\x01\xff')
    fields = decode_asset_payload(DeserializerRaptoreum(raw_tx(8, payload)).read_tx())
    assert fields['reference_hash_hex'] == 'ff'


def test_rtM_type_collision_and_lossless_hash():
    payload = new_asset_payload()
    raw = raw_tx(8, payload, [(0, asset_script())])
    reader = DeserializerRaptoreum(raw)
    tx, tx_hash = reader.read_tx_and_hash()
    assert tx.tx_type == 8
    assert tx.extra_payload == payload
    assert tx.serialize() == raw
    assert tx_hash == hashlib.sha256(hashlib.sha256(raw).digest()).digest()
    assert reader.cursor == len(raw)
    fields = decode_asset_payload(tx)
    assert fields['name'] == 'EXAMPLE'
    assert fields['decimal_point'] == 2
    assert fields['owner'] == '33' * 20


def test_version_four_does_not_read_extra_payload():
    raw = raw_tx(8, version=4)
    reader = DeserializerRaptoreum(raw + b'NEXT')
    tx = reader.read_tx()
    assert tx.extra_payload == b''
    assert reader.cursor == len(raw)
    assert tx.serialize() == raw


def test_child_payload_and_unknown_payload():
    tx = DeserializerRaptoreum(raw_tx(8, new_asset_payload(False))).read_tx()
    assert decode_asset_payload(tx)['root_id'] == ASSET
    tx = DeserializerRaptoreum(raw_tx(187, b'opaque')).read_tx()
    assert tx.extra_payload == b'opaque'
    assert decode_asset_payload(tx) is None


def test_update_and_mint_fields_follow_network_order():
    tx = DeserializerRaptoreum(raw_tx(9, update_asset_payload())).read_tx()
    fields = decode_asset_payload(tx)
    assert fields['asset_id'] == ASSET
    assert fields['max_mint_count'] == 15
    assert fields['amount'] == 200000000
    assert fields['owner'] == '66'*20
    mint = b'\x01\0' + varbytes(ASSET.encode()) + b'\x0c\0' + bytes(32) + varbytes(b'sig')
    fields = decode_asset_payload(DeserializerRaptoreum(raw_tx(10, mint)).read_tx())
    assert fields['fee'] == 12
    assert fields['signature'] == b'sig'.hex()


def test_truncated_transaction_does_not_rely_on_assert():
    with pytest.raises(ValueError):
        DeserializerRaptoreum(raw_tx(8, b'payload')[:-2]).read_tx()


def test_vectors_match_core_mainnet_decoder_txids():
    import json
    from pathlib import Path
    vectors = json.loads((Path(__file__).parents[1] / 'fixtures' / 'raptoreum'
                          / 'core_decoder_vectors.json').read_text())
    for vector in vectors['transactions']:
        raw = bytes.fromhex(vector['raw'])
        tx, tx_hash = DeserializerRaptoreum(raw).read_tx_and_hash()
        assert tx_hash[::-1].hex() == vector['txid']
        assert tx.serialize() == raw
        assert tx.tx_type == vector['type']
