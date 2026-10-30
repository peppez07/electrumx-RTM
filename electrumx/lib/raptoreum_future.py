'''Future-output eligibility from Core 2.0.3.01 consensus/tx_verify.cpp.

This is advisory input-selection information, not transaction validation.
Core uses next-block height OR adjusted wall time (not median time past).
'''


def future_spendability(record, indexed_height, adjusted_time):
    result = dict(record)
    if 'future' not in record and record.get('source_type') == 7:
        result.update(spendable=False, spendable_reason='future_metadata_unavailable')
        return result
    future = record.get('future')
    if future is None:
        return result
    result['future'] = dict(future)
    result['spendable'] = False
    if record.get('spendable') is not True:
        result['spendable_reason'] = 'unsupported_script'
        return result
    height = record.get('height')
    if type(height) is not int or height <= 0:
        result['spendable_reason'] = 'future_unconfirmed'
        return result
    maturity, seconds = future.get('maturity'), future.get('lock_time')
    confirmed = future.get('confirmed_time')
    if (type(maturity) is not int or type(seconds) is not int or
            not -2**31 <= maturity < 2**31 or not -2**31 <= seconds < 2**31 or
            type(indexed_height) is not int or indexed_height < height or
            type(confirmed) is not int or not 0 <= confirmed <= 0xffffffff or
            type(adjusted_time) is not int or adjusted_time < 0):
        result['spendable_reason'] = 'future_context_unavailable'
        return result
    block_mature = maturity >= 0 and indexed_height+1-height >= maturity
    time_mature = seconds >= 0 and adjusted_time-confirmed >= seconds
    result['future'].update(block_mature=block_mature, time_mature=time_mature)
    result['spendable'] = block_mature or time_mature
    result['spendable_reason'] = 'eligible' if result['spendable'] else 'future_locked'
    return result
