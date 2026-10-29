'''Raptoreum Core 2.0.3.01 asset wire formats and recipient extraction.'''

import struct
from dataclasses import dataclass
from hashlib import sha256

COIN = 100000000
MAX_MONEY = 21000000000 * COIN


class AssetParseError(ValueError):
    pass


class Reader:
    def __init__(self, data):
        self.data = data
        self.cursor = 0

    def take(self, size):
        end = self.cursor + size
        if size < 0 or end > len(self.data):
            raise AssetParseError('truncated RTM asset data')
        value = self.data[self.cursor:end]
        self.cursor = end
        return value

    def number(self, fmt):
        return struct.unpack('<' + fmt, self.take(struct.calcsize('<' + fmt)))[0]

    def compact_size(self):
        first = self.number('B')
        if first < 253:
            return first
        fmt, minimum = {253: ('H', 253), 254: ('I', 65536),
                        255: ('Q', 4294967296)}[first]
        value = self.number(fmt)
        if value < minimum:
            raise AssetParseError('nonminimal CompactSize')
        return value

    def varbytes(self):
        return self.take(self.compact_size())

    def string(self):
        try:
            return self.varbytes().decode('utf-8')
        except UnicodeError as e:
            raise AssetParseError('invalid RTM asset string') from e

    def finished(self):
        if self.cursor != len(self.data):
            raise AssetParseError('trailing RTM payload bytes')


def valid_asset_id(value):
    return (isinstance(value, str) and len(value) == 64
            and all(c in '0123456789abcdef' for c in value))


@dataclass(frozen=True)
class AssetTransfer:
    asset_id: str
    amount: int
    recipient_script: bytes
    original_script: bytes
    is_unique: bool
    unique_id: int
    canonical: bool

    @property
    def hashX(self):
        return sha256(self.recipient_script).digest()[:11]

    @property
    def unique_end(self):
        return self.unique_id + self.amount // COIN if self.is_unique else None


def parse_asset_script(script):
    '''Match Core IsAssetScript/GetTransferAsset, retaining noncanonical forms.

    Core extracts the recipient key at bytes 3:23 and ignores trailing bytes
    during transfer deserialization. Only canonical builder output is selectable.
    Recognized malformed data must never fall through to native coin selection.
    '''
    if len(script) <= 31 or script[25] != 0xbc:
        return None
    if script[27] == 0x72:
        if script[27:30] != b'rtm':
            return None
        start = 30
    elif script[28:31] == b'rtm':
        start = 31
    else:
        return None
    reader = Reader(script[start:])
    asset_id = reader.string()
    if not valid_asset_id(asset_id):
        raise AssetParseError('invalid asset creation transaction ID')
    unique_flag = reader.number('B')
    unique_id = reader.number('Q') if unique_flag else None
    amount = reader.number('q')
    if not 0 <= amount <= MAX_MONEY:
        raise AssetParseError('asset amount out of range')
    safe_interval = not unique_flag or (amount % COIN == 0
                                       and unique_id + amount // COIN <= 2**64)
    recipient = b'\x76\xa9\x14' + script[3:23] + b'\x88\xac'
    data = b'rtm' + script[start:start + reader.cursor]
    push = bytes([len(data)]) if len(data) < 76 else b'\x4c' + bytes([len(data)])
    canonical = (safe_interval and unique_flag in (0, 1)
                 and script == recipient + b'\xbc' + push + data + b'\x75')
    return AssetTransfer(asset_id, amount, recipient, script, bool(unique_flag),
                         unique_id, canonical)
