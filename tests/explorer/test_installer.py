import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[2]
SCRIPT = SOURCE/'contrib/install_explorer.py'


def installer():
    spec = importlib.util.spec_from_file_location('install_explorer', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def args(tmp_path):
    core = tmp_path/'source.conf'
    core.write_text('chain=main\nrpcuser=private\nrpcpassword=secret-password\nrpcport=10225\n')
    return argparse.Namespace(core_config=str(core), config_dir=str(tmp_path/'config'),
        venv='/opt/rtm/venv', image=None, port=8080, public_host='203.0.113.5',
        electrum_port=50001, bind='0.0.0.0', dry_run=True)


def test_dry_run_prints_reachable_url_without_writing_or_secrets(tmp_path):
    config_dir = tmp_path/'must-not-exist'
    result = subprocess.run([sys.executable, '-S', str(SCRIPT), '--dry-run', '--image', 'rtm:local',
        '--config-dir', str(config_dir), '--public-host', '203.0.113.5'],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output['url'] == 'http://203.0.113.5:8080/'
    assert not config_dir.exists()


def test_staging_copies_only_required_credentials_and_preserves_existing_config(tmp_path):
    module, options = installer(), args(tmp_path)
    Path(options.core_config).write_text(Path(options.core_config).read_text()+'wallet=private-wallet\n')
    module.stage_files(options)
    saved = Path(options.config_dir)/'core.conf'
    assert 'secret-password' in saved.read_text() and 'wallet=' not in saved.read_text()
    first = saved.read_bytes()
    Path(options.core_config).write_text('rpcuser=new\nrpcpassword=changed\n')
    module.stage_files(options)
    assert saved.read_bytes() == first
    env = (Path(options.config_dir)/'explorer.env').read_text()
    assert 'EXPLORER_PORT=8080' in env and 'secret-password' not in env


def test_install_rejects_testnet_without_staging_anything(tmp_path):
    module, options = installer(), args(tmp_path)
    Path(options.core_config).write_text('testnet=1\nrpcuser=user\nrpcpassword=secret\n')
    with pytest.raises(ValueError):
        module.stage_files(options)
    assert not Path(options.config_dir).exists()


def test_rendered_service_is_unprivileged_and_cannot_restart_core(tmp_path):
    module, options = installer(), args(tmp_path)
    unit = module.service_text(options, 123, 456)
    assert 'User=rtm-explorer' in unit
    assert 'electrumx.explorer' in unit
    assert 'Restart=on-failure' in unit
    assert 'rtm-core.service' not in unit
    options.image = 'rtm:test'
    unit = module.service_text(options, 123, 456)
    assert '--user 123:456' in unit and '--read-only' in unit


def test_opens_only_explorer_port_in_active_ufw(tmp_path, monkeypatch):
    module, options = installer(), args(tmp_path)
    commands = []
    monkeypatch.setattr(module.shutil, 'which', lambda name: '/usr/sbin/ufw')
    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout='Status: active\n')
    monkeypatch.setattr(module.subprocess, 'run', run)
    assert module.configure_firewall(options)
    assert commands == [['/usr/sbin/ufw','status'],
                         ['/usr/sbin/ufw','allow','8080/tcp','comment','RTM explorer']]


def test_loopback_install_never_modifies_firewall(tmp_path, monkeypatch):
    module, options = installer(), args(tmp_path)
    options.bind = '127.0.0.1'
    def forbidden(*args, **kwargs):
        raise AssertionError('Firewall must remain untouched for a local bind')
    monkeypatch.setattr(module.subprocess,'run',forbidden)
    assert not module.configure_firewall(options)


def test_wallet_relay_requires_explicit_flag_and_reinstall_preserves_choice(tmp_path):
    module, options = installer(), args(tmp_path)
    module.stage_files(options)
    environment = Path(options.config_dir)/'explorer.env'
    assert 'EXPLORER_WALLET_API=1' not in environment.read_text()
    options.wallet_api = True
    module.stage_files(options)
    assert 'EXPLORER_WALLET_API=1' in environment.read_text()
    configured = environment.read_bytes()
    options.wallet_api = None
    module.stage_files(options)
    assert environment.read_bytes() == configured
    options.wallet_api = False
    module.stage_files(options)
    assert 'EXPLORER_WALLET_API=0' in environment.read_text()
