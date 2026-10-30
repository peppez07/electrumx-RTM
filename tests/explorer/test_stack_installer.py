import importlib.util
import json
from pathlib import Path
import shlex
import subprocess
import sys
from urllib.parse import unquote, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]


def module():
    spec = importlib.util.spec_from_file_location('install_stack',ROOT/'contrib/install_stack.py')
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def options(tmp_path, *extra):
    source = tmp_path/'raptoreum.conf'
    source.write_text('chain=main\ntxindex=1\nassetindex=1\nprune=0\nrpcuser=rtm@user\nrpcpassword=a#b%@c\n')
    return module().parser().parse_args(['--image','rtm:test','--core-config',str(source),
        '--config-dir',str(tmp_path/'etc'), '--runtime-dir',str(tmp_path/'runtime'),
        '--db-dir',str(tmp_path/'db'), '--systemd-dir',str(tmp_path/'units'),
        '--profile','low',*extra])


def test_stdlib_dry_run_changes_nothing_and_exposes_no_credentials(tmp_path):
    command=[sys.executable,'-S',str(ROOT/'contrib/install_stack.py'),'--image','rtm:test',
        '--profile','low','--dry-run','--with-explorer','--public-host','203.0.113.5',
        '--config-dir',str(tmp_path/'missing')]
    result=subprocess.run(command,capture_output=True,text=True)
    assert result.returncode == 0, result.stderr
    data=json.loads(result.stdout)
    assert data['cache_mb'] == 16 and data['backend_memory_mib'] == 1024
    assert data['waits_for_core_sync'] is True
    assert data['explorer_url'] == 'http://203.0.113.5:8080/'
    assert not (tmp_path/'missing').exists()


def test_auto_profile_uses_available_ram():
    installer=module()
    assert installer.profile('auto',2*1024**3)['cache_mb'] == 16
    assert installer.profile('auto',8*1024**3)['cache_mb'] == 64
    assert installer.profile('low',8*1024**3)['memory_mib'] == 1024


@pytest.mark.parametrize('selected_profile', ['low', 'standard'])
def test_backend_limits_leave_indexing_headroom_and_match_across_runtimes(tmp_path, selected_profile):
    installer=module(); args=options(tmp_path,'--profile',selected_profile)
    result=installer.stage(args,123,456)
    docker_unit=(Path(args.systemd_dir)/installer.SERVICE).read_text()
    docker_lines=dict(line.split('=',1) for line in docker_unit.splitlines() if '=' in line)
    command=shlex.split(docker_lines['ExecStart'])
    ram_mib=int(command[command.index('--memory')+1].removesuffix('m'))
    total_mib=int(command[command.index('--memory-swap')+1].removesuffix('m'))
    # Production OOM kills occurred at up to 519 MiB RSS. CACHE_MB excludes
    # interpreter, DB, prefetch and parsed-block allocations; allow headroom.
    assert ram_mib >= 519+256
    assert total_mib-ram_mib == 512
    assert ram_mib <= 1536 and total_mib <= 2048
    assert int(docker_lines['MemoryMax'].removesuffix('M')) == ram_mib
    assert int(docker_lines['MemorySwapMax'].removesuffix('M')) == total_mib-ram_mib
    assert result['backend_memory_mib'] == ram_mib
    args.image=None; args.venv='/opt/rtm/venv'
    installer.stage(args,123,456)
    native_unit=(Path(args.systemd_dir)/installer.SERVICE).read_text()
    native_lines=dict(line.split('=',1) for line in native_unit.splitlines() if '=' in line)
    assert native_lines['MemoryMax'] == docker_lines['MemoryMax']
    assert native_lines['MemorySwapMax'] == docker_lines['MemorySwapMax']


@pytest.mark.parametrize('selected_profile,legacy_ram', [('low',256), ('standard',512)])
def test_reinstall_upgrades_legacy_memory_budget_preserving_existing_data_and_ports(tmp_path, selected_profile, legacy_ram):
    installer=module(); args=options(tmp_path,'--profile',selected_profile,
        '--tcp-port','51002','--ws-port','51003','--rpc-port','8101')
    installer.stage(args,123,456)
    manifest=Path(args.config_dir)/'installation.json'
    saved=json.loads(manifest.read_text())
    saved['backend_memory_mib']=legacy_ram
    manifest.write_text(json.dumps(saved))
    unit=Path(args.systemd_dir)/installer.SERVICE
    unit.write_text(installer.MARKER+'\n[Service]\nMemoryMax='+str(legacy_ram)+'M\nMemorySwapMax=256M\n')
    marker=Path(args.db_dir)/'indexed-data'; marker.write_bytes(b'existing index')
    core=Path(args.core_config); original_core=core.read_bytes()
    rpc=Path(args.config_dir)/'core-rpc.json'; original_rpc=rpc.read_bytes()
    restored=installer.parse_options(['--reinstall','--config-dir',args.config_dir])
    result=installer.stage(restored,123,456)
    assert result['backend_memory_mib'] >= 519+256
    assert result['profile'] == selected_profile
    assert 'MemoryMax='+str(result['backend_memory_mib'])+'M' in unit.read_text()
    assert 'MemorySwapMax=512M' in unit.read_text()
    assert marker.read_bytes() == b'existing index'
    assert core.read_bytes() == original_core and rpc.read_bytes() == original_rpc
    assert result['db_directory'] == str(Path(args.db_dir).resolve())
    assert (result['tcp_port'],result['ws_port']) == (51002,51003)
    assert 'rpc://127.0.0.1:8101' in (Path(args.config_dir)/'server.env').read_text()


@pytest.mark.parametrize('replacement',[('chain=main','chain=test'),
    ('assetindex=1','assetindex=0'),('txindex=1','txindex=0'),('prune=0','prune=550')])
def test_bad_core_settings_fail_without_changing_source_or_staging(tmp_path,replacement):
    installer=module(); args=options(tmp_path,'--with-explorer')
    source=Path(args.core_config)
    source.write_text(source.read_text().replace(*replacement))
    original=source.read_bytes()
    with pytest.raises(ValueError):
        installer.stage(args,123,456)
    assert source.read_bytes() == original
    assert not Path(args.config_dir).exists()


def test_staging_creates_one_backend_with_private_escaped_rpc_and_wait_gate(tmp_path):
    installer=module(); args=options(tmp_path)
    result=installer.stage(args,123,456)
    env=dict(line.split('=',1) for line in (Path(args.config_dir)/'server.env').read_text().splitlines())
    parsed=urlsplit(env['DAEMON_URL'])
    assert unquote(parsed.username) == 'rtm@user'
    assert unquote(parsed.password) == 'a#b%@c'
    assert env['COIN'] == 'Raptoreum' and env['NET'] == 'mainnet'
    assert env['CACHE_MB'] == '16'
    assert env['SERVICES'] == 'tcp://127.0.0.1:50002,ws://127.0.0.1:50003,rpc://127.0.0.1:8001'
    unit=(Path(args.systemd_dir)/'rtm-electrumx-assets.service').read_text()
    assert 'ExecCondition=' in unit and 'core_ready.py' in unit
    assert '--user 123:456' in unit and '--memory 1024m' in unit
    assert '--memory-swap 1536m' in unit and '--cpus 0.5' in unit
    assert '--read-only' in unit and 'rtm-core.service' not in unit
    assert (Path(args.runtime_dir)/'core_ready.py').is_file()
    assert result['waits_for_core_sync'] is True


def test_reinstall_preserves_rpc_db_and_unknown_config(tmp_path):
    installer=module(); args=options(tmp_path)
    installer.stage(args,123,456)
    config=Path(args.config_dir)/'core-rpc.json'; before=config.read_bytes()
    marker=Path(args.db_dir)/'user-data'; marker.write_text('preserve')
    env=Path(args.config_dir)/'server.env'
    env.write_text(env.read_text()+'CUSTOM_SETTING=preserve\n')
    installer.stage(args,123,456)
    assert config.read_bytes() == before
    assert marker.read_text() == 'preserve'
    assert 'CUSTOM_SETTING=preserve' in env.read_text()
    assert len(list(Path(args.systemd_dir).glob('*.service'))) == 1


def test_unrelated_service_is_not_replaced(tmp_path):
    installer=module(); args=options(tmp_path)
    units=Path(args.systemd_dir); units.mkdir()
    unit=units/'rtm-electrumx-assets.service'; unit.write_text('[Unit]\nDescription=Unrelated\n')
    with pytest.raises(ValueError):
        installer.stage(args,123,456)
    assert 'Unrelated' in unit.read_text()
    assert not Path(args.config_dir).exists()


def test_native_service_uses_installed_executable_and_dedicated_user(tmp_path):
    installer=module(); args=options(tmp_path)
    args.image=None; args.venv='/opt/rtm/venv'
    installer.stage(args,123,456)
    unit=(Path(args.systemd_dir)/'rtm-electrumx-assets.service').read_text()
    normalized=unit.replace(chr(92),'/').replace('//','/')
    assert 'User=rtm-electrumx' in unit and '/opt/rtm/venv/bin/electrumx_server' in normalized
    assert 'MemoryMax=1024M' in unit and 'MemorySwapMax=512M' in unit


def test_half_tls_pair_and_conflicting_ports_are_rejected(tmp_path):
    installer=module(); args=options(tmp_path)
    args.tls_cert='cert.pem'
    with pytest.raises(ValueError): installer.stage(args,123,456)


def test_reinstall_cannot_silently_change_existing_ports(tmp_path):
    installer=module();args=options(tmp_path)
    installer.stage(args,123,456)
    environment=Path(args.config_dir)/'server.env';before=environment.read_bytes()
    args.ws_port=51003
    with pytest.raises(ValueError):installer.stage(args,123,456)
    assert environment.read_bytes() == before


def test_core_data_directory_is_never_used_as_electrum_database(tmp_path):
    installer=module();args=options(tmp_path)
    data=Path(args.db_dir);(data/'chainstate').mkdir(parents=True)
    with pytest.raises(ValueError):installer.stage(args,123,456)
    assert not Path(args.config_dir).exists()


def test_tls_survives_reinstall_without_repeating_key_paths(tmp_path):
    import shutil
    openssl=shutil.which('openssl')
    if not openssl:pytest.skip('Actual TLS pair test runs on Linux with openssl')
    installer=module();args=options(tmp_path)
    cert=tmp_path/'source.crt';key=tmp_path/'source.key'
    subprocess.run([openssl,'req','-x509','-newkey','ec','-pkeyopt','ec_paramgen_curve:prime256v1',
        '-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost'],
        check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    args.tls_cert=str(cert);args.tls_key=str(key)
    installer.stage(args,123,456)
    args.tls_cert=args.tls_key=None
    installer.stage(args,123,456)
    env=(Path(args.config_dir)/'server.env').read_text()
    assert 'wss://0.0.0.0:50004' in env and 'SSL_KEYFILE=/config/tls.key' in env
    args.no_tls=True
    installer.stage(args,123,456)
    assert 'wss://' not in (Path(args.config_dir)/'server.env').read_text()
    args.tls_cert=None; args.ws_port=args.tcp_port
    with pytest.raises(ValueError): installer.stage(args,123,456)

@pytest.mark.parametrize('preserved',[False,True])
def test_wss_cannot_take_core_rpc_port(tmp_path,preserved):
    installer=module();args=options(tmp_path)
    if preserved:
        installer.stage(args,123,456)
        env=Path(args.config_dir)/'server.env'
        env.write_text(env.read_text()+'SSL_CERTFILE=/config/tls.crt\nSSL_KEYFILE=/config/tls.key\n')
    else:
        args.tls_cert='certificate.pem';args.tls_key='key.pem'
    args.wss_port=10225
    with pytest.raises(ValueError,match='Core RPC'):
        installer.stage(args,123,456)


@pytest.mark.parametrize('replacement',[('assetindex=1','assetindex=0'),('txindex=1','txindex=0')])
def test_electrumx_alone_runs_beside_an_unmodified_node(tmp_path,replacement):
    installer=module(); args=options(tmp_path)
    source=Path(args.core_config)
    source.write_text(source.read_text().replace(*replacement))
    original=source.read_bytes()
    result=installer.stage(args,123,456)
    assert source.read_bytes() == original
    assert result['core_txindex'] is (replacement[0] != 'txindex=1')


@pytest.mark.parametrize('replacement',[('chain=main','chain=test'),('prune=0','prune=550')])
def test_electrumx_alone_still_requires_unpruned_mainnet(tmp_path,replacement):
    installer=module(); args=options(tmp_path)
    source=Path(args.core_config)
    source.write_text(source.read_text().replace(*replacement))
    with pytest.raises(ValueError):
        installer.stage(args,123,456)


def test_https_runs_beside_core_without_txindex_or_explorer(tmp_path):
    installer=module();args=options(tmp_path,'--wallet-https','--public-host','144.202.18.233','--agree-acme-tos')
    source=Path(args.core_config)
    source.write_text(source.read_text().replace('txindex=1','txindex=0').replace('assetindex=1','assetindex=0'))
    result=installer.stage(args,123,456)
    assert result['wallet_https_url'] == 'https://144.202.18.233/api/v1/electrum'
    assert result['explorer_url'] is None and result['core_txindex'] is False
    https_args=installer.wallet_https_options(args)
    assert https_args.electrum_port == 50002 and https_args.explorer_port == 0
    assert https_args.agree_acme_tos


def test_reinstall_restores_selected_stack_ports_paths_and_https(tmp_path):
    installer=module();args=options(tmp_path,'--wallet-https','--public-host','144.202.18.233',
        '--agree-acme-tos','--tcp-port','51002','--ws-port','51003','--rpc-port','8101',
        '--gateway-port','8190','--with-explorer','--port','8180')
    installer.stage(args,123,456)
    restored=installer.parse_options(['--reinstall','--config-dir',args.config_dir])
    assert restored.image == 'rtm:test' and restored.wallet_https and restored.with_explorer
    assert restored.core_config == args.core_config and restored.db_dir == args.db_dir
    assert restored.tcp_port == 51002 and restored.ws_port == 51003 and restored.rpc_port == 8101
    assert restored.gateway_port == 8190 and restored.port == 8180 and restored.agree_acme_tos
    assert installer.wallet_https_options(restored).explorer_port == 8180
    installer.stage(restored,123,456)


def test_https_gateway_cannot_take_core_or_private_service_ports(tmp_path):
    installer=module();args=options(tmp_path,'--wallet-https','--public-host','144.202.18.233')
    for port in (50002,8001,10225,80,443):
        args.gateway_port=port
        with pytest.raises(ValueError):
            installer.stage(args,123,456)
    assert not Path(args.config_dir).exists()
