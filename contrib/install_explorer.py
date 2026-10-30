#!/usr/bin/env python3
'''Configure the bundled mainnet explorer as an independent systemd service.'''

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import urlopen

# The Docker installer must run on a host with only standard-library Python.
# Import the dependency-free config reader without electrumx's server imports.
_config_spec = importlib.util.spec_from_file_location('rtm_explorer_config',
    Path(__file__).resolve().parents[1]/'electrumx/explorer/config.py')
_config = importlib.util.module_from_spec(_config_spec)
_config_spec.loader.exec_module(_config)
load_core_config = _config.load_core_config
METRICS_DIR = '/var/lib/rtm-explorer/metrics'
METRICS_MARKER = '# Managed by electrumx-RTM host metrics'


def stage_metrics(directory, runtime, units):
    directory, runtime, units = Path(directory), Path(runtime), Path(units)
    names = ('rtm-explorer-metrics.service', 'rtm-explorer-metrics.timer')
    for name in names:
        path = units/name
        if path.exists() and METRICS_MARKER not in path.read_text():
            raise ValueError('Refusing to replace an unrelated host metrics service.')
    for folder in (directory, runtime, units):
        folder.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o750)
    runtime.chmod(0o755)
    shutil.copyfile(Path(__file__).parent/'host_metrics.py', runtime/'host_metrics.py')
    (runtime/'host_metrics.py').chmod(0o644)
    q = lambda path: '"'+str(path).replace('\\','\\\\').replace('"','\\"').replace('%','%%')+'"'
    (units/names[0]).write_text(METRICS_MARKER+'\n[Unit]\nDescription=Raptoreum host resource sampler\n'
        '[Service]\nType=oneshot\nUser=rtm-explorer\nGroup=rtm-explorer\n'
        'ExecStart=/usr/bin/python3 '+q(runtime/'host_metrics.py')+' --directory '+q(directory)+'\n'
        'Nice=19\nIOSchedulingClass=idle\nMemoryMax=48M\nCPUQuota=5%\nTimeoutStartSec=15\n'
        'NoNewPrivileges=true\nPrivateTmp=true\nProtectHome=true\nProtectSystem=strict\n'
        'ReadWritePaths='+q(directory)+'\nRestrictAddressFamilies=AF_UNIX\n')
    (units/names[1]).write_text(METRICS_MARKER+'\n[Unit]\nDescription=Sample VPS resources once per minute\n'
        '[Timer]\nOnBootSec=15\nOnUnitActiveSec=60\nAccuracySec=5\nUnit='+names[0]+'\n'
        '[Install]\nWantedBy=timers.target\n')


def wait_for_health(port, attempts=15):
    for attempt in range(attempts):
        try:
            with urlopen('http://127.0.0.1:{}/api/v1/health'.format(port), timeout=2) as reply:
                if json.loads(reply.read(4096)) == {'service':'electrumx-rtm-explorer','version':1}:
                    return
        except (OSError, ValueError):
            pass
        if attempt+1 < attempts:
            time.sleep(1)
    raise RuntimeError('Explorer startup health check failed; inspect journalctl -u rtm-explorer.service.')


def service_text(args, uid, gid):
    config_dir = Path(args.config_dir).resolve()
    env_path = config_dir/'explorer.env'
    quote = lambda value: '"'+str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')+'"'
    if args.image:
        command = ('/usr/bin/docker run --rm --name rtm-explorer --network host '
            '--user {}:{} --memory 192m --memory-swap 256m --cpus 0.5 '
            '--cap-drop ALL --security-opt no-new-privileges --read-only '
            '--tmpfs /tmp:rw,noexec,nosuid,size=16m --env-file {} '
            '-v {} -v {} {} electrumx_explorer').format(uid, gid, quote(env_path),
                quote(str(config_dir)+':'+str(config_dir)+':ro'),
                quote(METRICS_DIR+':'+METRICS_DIR+':ro'), quote(args.image))
        runtime = 'Requires=docker.service\nAfter=docker.service network-online.target'
        user = ''
        stop = 'ExecStop=/usr/bin/docker stop -t 20 rtm-explorer\n'
    else:
        command = '{} -m electrumx.explorer'.format(quote(Path(args.venv).resolve()/'bin/python'))
        runtime = 'After=network-online.target'
        user = 'User=rtm-explorer\nGroup=rtm-explorer\n'
        stop = ''
    return ('[Unit]\nDescription=Raptoreum mainnet explorer\nWants=network-online.target\n'+runtime+
        '\n[Service]\n'+user+'EnvironmentFile='+str(env_path).replace('%', '%%')+'\nExecStart='+command+'\n'+stop+
        'Restart=on-failure\nRestartSec=10\nTimeoutStopSec=30\n'
        'NoNewPrivileges=true\nProtectHome=true\nProtectSystem=strict\nPrivateTmp=true\n'
        'MemoryMax=256M\nCPUQuota=50%\n'
        '\n[Install]\nWantedBy=multi-user.target\n')


def stage_files(args):
    directory = Path(args.config_dir)
    core_file, env_file = directory/'core.conf', directory/'explorer.env'
    config = load_core_config(core_file if core_file.exists() else args.core_config)
    if env_file.exists():
        existing = dict(line.split('=', 1) for line in env_file.read_text().splitlines()
                        if '=' in line and not line.startswith('#'))
        expected = {'EXPLORER_PORT': str(args.port), 'EXPLORER_HOST': args.bind,
                    'EXPLORER_ELECTRUM_PORT': str(args.electrum_port),
                    'EXPLORER_CORE_CONFIG': str(core_file.resolve())}
        if any(existing.get(k) != v for k, v in expected.items()):
            raise ValueError('Existing explorer configuration differs; update it explicitly before reinstalling.')
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o750)
    if not core_file.exists():
        parsed = urlsplit(config['url'])
        text = ('chain=main\ntestnet=0\nregtest=0\nrpcconnect={}\nrpcport={}\n'
                'rpcuser={}\nrpcpassword={}\n').format(parsed.hostname, parsed.port,
                                                      config['username'], config['password'])
        fd = os.open(str(core_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        with os.fdopen(fd, 'w') as output:
            output.write(text)
    if not env_file.exists():
        fd = os.open(str(env_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        with os.fdopen(fd, 'w') as output:
            output.write('EXPLORER_CORE_CONFIG={}\nEXPLORER_HOST={}\nEXPLORER_PORT={}\n'
                'EXPLORER_ELECTRUM_HOST=127.0.0.1\nEXPLORER_ELECTRUM_PORT={}\n'.format(
                core_file.resolve(), args.bind, args.port, args.electrum_port))
    metrics_setting = 'EXPLORER_METRICS_FILE='+METRICS_DIR+'/host.json'
    existing_metrics = [line for line in env_file.read_text().splitlines() if line.startswith('EXPLORER_METRICS_FILE=')]
    if existing_metrics and existing_metrics != [metrics_setting]:
        raise ValueError('Managed explorer requires its shared host metrics path.')
    if not existing_metrics:
        with env_file.open('a') as output:
            output.write(metrics_setting+'\n')
    wallet_mode = getattr(args, 'wallet_api', None)
    if wallet_mode is not None:
        lines = [line for line in env_file.read_text().splitlines()
                 if not line.startswith('EXPLORER_WALLET_API=')]
        lines.append('EXPLORER_WALLET_API='+('1' if wallet_mode else '0'))
        updated = '\n'.join(lines)+'\n'
        if updated != env_file.read_text():
            with tempfile.NamedTemporaryFile(mode='w', dir=str(directory),
                    prefix='.explorer-env-', delete=False) as temporary:
                temporary.write(updated)
                temporary_path = Path(temporary.name)
            try:
                temporary_path.chmod(0o640)
                os.replace(str(temporary_path), str(env_file))
            finally:
                if temporary_path.exists():
                    temporary_path.unlink()
    return core_file, env_file


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--core-config', default='/var/lib/rtm-mainnet/core/raptoreum.conf')
    p.add_argument('--config-dir', default='/etc/rtm-explorer')
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--venv', help='Existing installed virtual environment (Python 3.10 recommended)')
    mode.add_argument('--image', help='Existing locally built Docker image containing this checkout')
    p.add_argument('--port', type=int, default=8080)
    p.add_argument('--electrum-port', type=int, default=50001)
    p.add_argument('--bind', choices=['0.0.0.0', '127.0.0.1'], default='0.0.0.0')
    p.add_argument('--public-host', help='Server IP or domain printed in the URL; otherwise uses a local route address')
    p.add_argument('--dry-run', action='store_true', help='Print deployment settings without reading secrets or changing files')
    wallet = p.add_mutually_exclusive_group()
    wallet.add_argument('--wallet-api', dest='wallet_api', action='store_const', const=True,
                        help='Enable decoding and relay of client-signed transactions; never server signing')
    wallet.add_argument('--no-wallet-api', dest='wallet_api', action='store_const', const=False,
                        help='Disable the HTTP transaction POST routes')
    p.set_defaults(wallet_api=None)
    return p


def configure_firewall(args):
    if args.bind == '127.0.0.1':
        return False
    ufw = shutil.which('ufw')
    if not ufw:
        return False
    result = subprocess.run([ufw, 'status'], check=False, capture_output=True, text=True,
                            env=dict(os.environ, LC_ALL='C'))
    if result.returncode != 0 or 'Status: active' not in result.stdout:
        return False
    subprocess.run([ufw, 'allow', str(args.port)+'/tcp', 'comment', 'RTM explorer'], check=True)
    return True


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if not all(1 <= port <= 65535 for port in (args.port, args.electrum_port)):
        p.error('Ports must be between 1 and 65535.')
    for value in (args.core_config, args.config_dir, args.venv or '', args.image or ''):
        if any(ord(c) < 32 for c in value) or (value and ('"' in value or '%' in value)):
            p.error('Configuration paths/image cannot contain control characters, quotes or percent signs.')
    host = args.public_host
    if not host:
        import socket
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
                route.connect(('1.1.1.1', 80))
                host = route.getsockname()[0]
        except OSError:
            host = '127.0.0.1'
    if not re.fullmatch(r'[A-Za-z0-9.-]+', host) or host.startswith('-'):
        p.error('Public host must be an IPv4 address or domain.')
    summary = {'url': 'http://{}:{}/'.format(host, args.port),
        'network': 'mainnet', 'config_dir': str(Path(args.config_dir).resolve()),
        'service': 'rtm-explorer.service', 'mode': 'docker' if args.image else 'venv'}
    summary['wallet_api_requested'] = args.wallet_api
    if args.dry_run:
        print(json.dumps(summary))
        return 0
    if sys.platform != 'linux' or os.geteuid() != 0:
        p.error('Installation requires Linux systemd and root; use --dry-run to inspect elsewhere.')
    if args.venv and not (Path(args.venv)/'bin/python').is_file():
        p.error('Install this checkout into the selected virtual environment first.')
    if args.image:
        subprocess.run(['docker', 'image', 'inspect', args.image], check=True, stdout=subprocess.DEVNULL)
    import pwd
    try:
        user = pwd.getpwnam('rtm-explorer')
    except KeyError:
        subprocess.run(['useradd', '--system', '--user-group', '--home-dir', '/nonexistent',
                        '--shell', '/usr/sbin/nologin', 'rtm-explorer'], check=True)
        user = pwd.getpwnam('rtm-explorer')
    try:
        files = stage_files(args)
    except (OSError, ValueError):
        print('Could not prepare a valid mainnet explorer config. Existing files are preserved; check paths and port settings.', file=sys.stderr)
        return 2
    directory = Path(args.config_dir)
    os.chown(directory, 0, user.pw_gid)
    for file in files:
        os.chown(file, 0, user.pw_gid)
        file.chmod(0o640)
    unit = Path('/etc/systemd/system/rtm-explorer.service')
    if unit.exists() and 'Description=Raptoreum mainnet explorer' not in unit.read_text():
        print('An unrelated rtm-explorer.service already exists; refusing to replace it.', file=sys.stderr)
        return 2
    unit.write_text(service_text(args, user.pw_uid, user.pw_gid))
    unit.chmod(0o644)
    metrics = Path(METRICS_DIR)
    stage_metrics(metrics, Path('/opt/rtm-explorer'), Path('/etc/systemd/system'))
    metrics.parent.chmod(0o755)
    os.chown(metrics, user.pw_uid, user.pw_gid)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', '--now', 'rtm-explorer-metrics.timer'], check=True)
    subprocess.run(['systemctl', 'start', 'rtm-explorer-metrics.service'], check=True)
    subprocess.run(['systemctl', 'enable', 'rtm-explorer.service'], check=True)
    subprocess.run(['systemctl', 'restart', 'rtm-explorer.service'], check=True)
    wait_for_health(args.port)
    firewall_opened = configure_firewall(args)
    print(json.dumps(summary))
    print('Explorer configured. '+('Active UFW permits the explorer port. ' if firewall_opened else '')+
          'Any additional host/provider firewall must permit TCP {}.'.format(args.port))
    print('For HTTPS, put a domain reverse proxy in front and bind the explorer to 127.0.0.1.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
