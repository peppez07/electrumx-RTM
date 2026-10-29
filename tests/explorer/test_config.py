from pathlib import Path

import pytest

from electrumx.explorer.config import load_core_config


def test_loads_existing_mainnet_rpc_configuration(tmp_path):
    config = tmp_path/'core.conf'
    config.write_text('chain=main\ntestnet=0\nregtest=0\nrpcuser=explorer\nrpcpassword=abc#def\nrpcport=10225\n')
    assert load_core_config(config) == {'url': 'http://127.0.0.1:10225/',
                                       'username': 'explorer', 'password': 'abc#def'}


@pytest.mark.parametrize('extra', ['testnet=1', 'regtest=1', 'chain=test',
                                  'rpcconnect=203.0.113.1', 'includeconf=other.conf'])
def test_rejects_non_mainnet_or_remote_rpc(tmp_path, extra):
    config = tmp_path/'core.conf'
    config.write_text('rpcuser=user\nrpcpassword=secret\n'+extra+'\n')
    with pytest.raises(ValueError):
        load_core_config(config)


def test_config_error_never_contains_secrets(tmp_path):
    config = tmp_path/'core.conf'
    config.write_text('rpcpassword=secret-value\n')
    with pytest.raises(ValueError) as exc:
        load_core_config(config)
    assert 'secret-value' not in str(exc.value)
