import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


def module():
    spec = importlib.util.spec_from_file_location('wallet_https_installer', ROOT/'contrib/install_wallet_https.py')
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def options(tmp_path, *extra):
    return module().parse_options(['--image', 'rtm:test', '--public-host', '144.202.18.233',
        '--config-dir', str(tmp_path/'config'), '--state-dir', str(tmp_path/'state'),
        '--systemd-dir', str(tmp_path/'units'), '--nginx-dir', str(tmp_path/'nginx'),
        '--certbot-venv', str(tmp_path/'certbot'), '--letsencrypt-dir', str(tmp_path/'certs'),
        *extra])


def test_stdlib_dry_run_is_password_free_and_does_not_write(tmp_path):
    result = subprocess.run([sys.executable, '-S', str(ROOT/'contrib/install_wallet_https.py'),
        '--image', 'rtm:test', '--public-host', '144.202.18.233', '--dry-run',
        '--config-dir', str(tmp_path/'missing')], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data['wallet_url'] == 'https://144.202.18.233/api/v1/electrum'
    assert data['gateway_bind'] == '127.0.0.1:8090'
    assert 'password' not in result.stdout.lower()
    assert not (tmp_path/'missing').exists()


@pytest.mark.parametrize('host', ['127.0.0.1', '192.168.1.2', '203.0.113.4', 'user@example.com',
    'https://example.com', 'example.com/path', 'example.com:443', 'bad;nginx', '*.example.com'])
def test_unsafe_or_nonpublic_hosts_are_rejected_before_writing(tmp_path, host):
    installer = module()
    with pytest.raises(ValueError):
        args = options(tmp_path, '--public-host', host)
        installer.stage(args, 123, 456)
    assert not (tmp_path/'config').exists()


def test_stage_isolated_gateway_and_bootstrap_nginx_then_tls(tmp_path):
    installer = module(); args = options(tmp_path)
    result = installer.stage(args, 123, 456)
    env = (tmp_path/'config/gateway.env').read_text()
    assert 'WALLET_HTTP_HOST=127.0.0.1' in env
    assert 'WALLET_ELECTRUM_HOST=127.0.0.1' in env
    assert 'WALLET_ELECTRUM_PORT=50002' in env
    assert 'WALLET_EXPLORER_PORT=0' in env
    assert 'DAEMON_URL' not in env and 'core-rpc' not in env
    unit = (tmp_path/'units/rtm-wallet-http.service').read_text()
    assert 'EnvironmentFile='+str(tmp_path/'config/gateway.env')+'\n' in unit
    assert 'electrumx.http_gateway' in unit and '--network host' in unit
    assert '--user 123:456' in unit and '--cap-drop ALL' in unit and '--read-only' in unit
    assert 'server.env' not in unit and 'core-rpc' not in unit and '/db' not in unit
    bootstrap = (tmp_path/'nginx/rtm-wallet-http.conf').read_text()
    assert 'listen 80;' in bootstrap and 'listen 443' not in bootstrap
    assert '/.well-known/acme-challenge/' in bootstrap
    installer.stage(args, 123, 456, tls=True)
    nginx = (tmp_path/'nginx/rtm-wallet-http.conf').read_text()
    assert 'listen 443 ssl default_server;' in nginx and 'proxy_pass http://127.0.0.1:8090;' in nginx
    assert 'proxy_next_upstream off;' in nginx and 'client_max_body_size' in nginx
    assert 'proxy_read_timeout' in nginx and 'server_name 144.202.18.233;' in nginx
    assert result['wallet_url'].startswith('https://')


def test_reinstall_restores_all_options_and_preserves_identity(tmp_path):
    installer = module()
    args = options(tmp_path, '--electrum-port', '51002', '--gateway-port', '8190',
        '--explorer-port', '8180', '--email', 'ops@example.com', '--agree-acme-tos')
    installer.stage(args, 123, 456)
    restored = installer.parse_options(['--reinstall', '--config-dir', str(tmp_path/'config')])
    assert restored.image == 'rtm:test' and restored.electrum_port == 51002
    assert restored.gateway_port == 8190 and restored.explorer_port == 8180
    assert restored.agree_acme_tos and restored.email == 'ops@example.com'
    assert restored.state_dir == str(tmp_path/'state')
    installer.stage(restored, 123, 456)
    before = (tmp_path/'config/installation.json').read_bytes()
    restored.public_host = 'wallet.example.com'
    with pytest.raises(ValueError, match='host'):
        installer.stage(restored, 123, 456)
    assert (tmp_path/'config/installation.json').read_bytes() == before


@pytest.mark.parametrize('target', ['units/rtm-wallet-http.service',
    'units/rtm-wallet-cert-renew.service', 'units/rtm-wallet-cert-renew.timer',
    'nginx/rtm-wallet-http.conf', 'config/gateway.env'])
def test_unrelated_managed_path_refused_without_partial_writes(tmp_path, target):
    installer = module(); args = options(tmp_path)
    path = tmp_path/target; path.parent.mkdir(parents=True); path.write_text('unrelated\n')
    with pytest.raises(ValueError, match='unrelated'):
        installer.stage(args, 123, 456)
    assert path.read_text() == 'unrelated\n'
    assert not (tmp_path/'config/installation.json').exists()


def test_unrelated_nginx_listener_rejected_and_never_rewritten(tmp_path):
    installer = module(); args = options(tmp_path)
    unrelated = tmp_path/'nginx/other.conf'; unrelated.parent.mkdir(); unrelated.write_text('server { listen 443 ssl; }')
    with pytest.raises(ValueError, match='nginx'):
        installer.stage(args, 123, 456)
    assert unrelated.read_text() == 'server { listen 443 ssl; }'
    assert not (tmp_path/'config').exists()


def test_active_custom_nginx_include_cannot_claim_https_before_changes(tmp_path, monkeypatch):
    installer = module(); args = options(tmp_path, '--agree-acme-tos')
    monkeypatch.setattr(installer.shutil, 'which', lambda name: '/usr/sbin/nginx' if name == 'nginx' else None)
    def run(command, **kwargs):
        stdout = '# configuration file /srv/unrelated/nginx-site:\nserver { listen 443 ssl; }\n' if command == ['nginx', '-T'] else ''
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr='')
    with pytest.raises(ValueError, match='nginx'):
        installer.preflight(args, run=run)
    assert not (tmp_path/'config').exists()


def test_native_gateway_uses_unprivileged_identity_and_no_core_access(tmp_path):
    installer = module(); args = options(tmp_path)
    args.image = None; args.venv = '/opt/rtm/venv'
    installer.stage(args, 123, 456)
    unit = (tmp_path/'units/rtm-wallet-http.service').read_text().replace('\\', '/').replace('//', '/')
    assert 'User=rtm-wallet-http' in unit and ' -m electrumx.http_gateway' in unit
    assert '/opt/rtm/venv/bin/python' in unit
    assert 'InaccessiblePaths=-/etc/rtm-electrumx -/var/lib/rtm-mainnet' in unit


def test_ip_and_domain_certificate_commands_and_frequent_renewal(tmp_path):
    installer = module(); args = options(tmp_path, '--agree-acme-tos')
    command = installer.certificate_command(args)
    assert command[command.index('--ip-address')+1] == '144.202.18.233'
    assert command[command.index('--required-profile')+1] == 'shortlived'
    assert '--webroot' in command and '--agree-tos' in command
    assert '--force-renewal' not in command and '--keep-until-expiring' in command
    assert '--register-unsafely-without-email' in command
    args.public_host = 'wallet.example.com'; args.email = 'ops@example.com'
    command = installer.certificate_command(args)
    assert command[command.index('-d')+1] == 'wallet.example.com'
    assert '--ip-address' not in command and '--required-profile' not in command
    assert command[command.index('--email')+1] == 'ops@example.com'
    installer.stage(args, 123, 456)
    timer = (tmp_path/'units/rtm-wallet-cert-renew.timer').read_text()
    renew = (tmp_path/'units/rtm-wallet-cert-renew.service').read_text()
    assert 'OnUnitActiveSec=12h' in timer and 'Persistent=true' in timer
    import shlex
    command = shlex.split(next(line.split('=', 1)[1] for line in renew.splitlines() if line.startswith('ExecStart=')))
    assert 'renew' in command and '--deploy-hook' in command
    assert command[command.index('--deploy-hook')+1] == '/usr/bin/systemctl reload nginx'


def test_live_preflight_requires_tos_and_refuses_occupied_port_before_commands(tmp_path):
    installer = module(); args = options(tmp_path)
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        if command[:2] == ['docker', 'inspect']:
            return subprocess.CompletedProcess(command, 1, stdout='', stderr='Error: No such object: rtm-wallet-http')
        return subprocess.CompletedProcess(command, 0, stdout='LISTEN 0 4096 0.0.0.0:443 0.0.0.0:* users:(("other",pid=12,fd=3))\n', stderr='')
    with pytest.raises(ValueError, match='agree-acme-tos'):
        installer.preflight(args, run=run)
    assert not commands
    args.agree_acme_tos = True
    with pytest.raises(ValueError, match='443'):
        installer.preflight(args, run=run)
    assert not (tmp_path/'config').exists()


def test_health_accepts_live_gateway_while_electrum_is_synchronizing(tmp_path):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading
    installer = module(); args = options(tmp_path)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"ok":true}')
        def log_message(self, *args):
            pass
    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        args.gateway_port = server.server_port
        installer.verify_health(args)
    finally:
        server.shutdown(); thread.join(); server.server_close()


@pytest.mark.parametrize('target', ['certbot', 'certs'])
def test_unowned_certificate_environment_refused_before_writing(tmp_path, target):
    installer = module(); args = options(tmp_path)
    directory = tmp_path/target; directory.mkdir(); (directory/'unrelated').write_text('preserve')
    with pytest.raises(ValueError, match='unrelated'):
        installer.stage(args, 123, 456)
    assert not (tmp_path/'config').exists()


def test_reinstall_ports_must_belong_to_the_managed_process(tmp_path):
    installer = module(); args = options(tmp_path, '--agree-acme-tos')
    installer.stage(args, 123, 456)
    def run(command, **kwargs):
        if command[0] == 'ss':
            return subprocess.CompletedProcess(command, 0, stdout='LISTEN 0 4096 127.0.0.1:8090 0.0.0.0:* users:(("python",pid=22,fd=3))\n', stderr='')
        if command[0] == 'systemctl':
            return subprocess.CompletedProcess(command, 0, stdout='11\n', stderr='')
        return subprocess.CompletedProcess(command, 0, stdout='managed 22\n', stderr='')
    installer.preflight(args, run=run)
    args.image = None; args.venv = '/opt/rtm/venv'
    with pytest.raises(ValueError, match='8090'):
        installer.preflight(args, run=run)


@pytest.mark.parametrize('identity, installed', [('unrelated 0\n', False), ('unrelated 55\n', False),
    ('managed 0\n', False), ('unrelated 0\n', True), ('unrelated 55\n', True)])
def test_docker_name_collision_without_gateway_listener_refuses_before_staging(tmp_path, identity, installed):
    installer = module(); args = options(tmp_path, '--agree-acme-tos')
    if installed:
        installer.stage(args, 123, 456)
        original = (tmp_path/'config/installation.json').read_bytes()
    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0,
            stdout=identity if command[0] == 'docker' else '', stderr='')
    with pytest.raises(ValueError, match='container'):
        installer.preflight(args, run=run)
    if installed:
        assert (tmp_path/'config/installation.json').read_bytes() == original
    else:
        assert not (tmp_path/'config').exists()
        assert not (tmp_path/'units').exists()


@pytest.mark.parametrize('failure', [
    'Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?',
    'permission denied while trying to connect to the Docker daemon socket',
    'Error: No such image: rtm-wallet-http',
])
def test_docker_inspection_failure_is_not_treated_as_container_absence(tmp_path, failure):
    installer = module(); args = options(tmp_path, '--agree-acme-tos')
    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1 if command[0] == 'docker' else 0,
            stdout='', stderr=failure if command[0] == 'docker' else '')
    with pytest.raises(ValueError, match='Docker'):
        installer.preflight(args, run=run)
    assert not (tmp_path/'config').exists()


@pytest.mark.parametrize('missing', ['Error: No such object: rtm-wallet-http', 'Error: No such container: rtm-wallet-http'])
def test_confirmed_absent_docker_name_can_install(tmp_path, missing):
    installer = module(); args = options(tmp_path, '--agree-acme-tos')
    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1 if command[0] == 'docker' else 0,
            stdout='', stderr=missing if command[0] == 'docker' else '')
    installer.preflight(args, run=run)
    assert not (tmp_path/'config').exists()


@pytest.mark.parametrize('pid', [0, 22])
def test_existing_owned_docker_container_can_reinstall_without_listener(tmp_path, pid):
    installer = module(); args = options(tmp_path, '--agree-acme-tos')
    installer.stage(args, 123, 456)
    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0,
            stdout='managed '+str(pid)+'\n' if command[0] == 'docker' else '', stderr='')
    installer.preflight(args, run=run)


def test_owned_certbot_environment_recovers_from_failed_first_pip_install(tmp_path, monkeypatch):
    import types
    installer = module(); args = options(tmp_path, '--agree-acme-tos')
    monkeypatch.setitem(sys.modules, 'pwd', types.SimpleNamespace(
        getpwnam=lambda name: types.SimpleNamespace(pw_uid=123, pw_gid=456)))
    monkeypatch.setattr(installer.shutil, 'which', lambda name: '/usr/bin/apt-get' if name == 'apt-get' else None)
    monkeypatch.setattr(installer, 'verify_health', lambda *a, **kw: None)
    monkeypatch.setattr(installer, 'verify_https_health', lambda *a, **kw: None)
    attempts = []
    def run(command, **kwargs):
        if command[:2] == ['docker', 'inspect']:
            return subprocess.CompletedProcess(command, 1, stdout='', stderr='Error: No such object: rtm-wallet-http')
        if command[:3] == ['/usr/bin/python3', '-m', 'venv']:
            python = tmp_path/'certbot/bin/python'; python.parent.mkdir(parents=True); python.write_text('python')
            python.chmod(0o755)
        if 'pip' in command:
            attempts.append(command)
            if len(attempts) == 1:
                raise subprocess.CalledProcessError(1, command)
            (tmp_path/'certbot/bin/certbot').write_text('certbot')
        return subprocess.CompletedProcess(command, 0, stdout='', stderr='')
    with pytest.raises(subprocess.CalledProcessError):
        installer.install(args, run=run)
    assert (tmp_path/'certbot/.rtm-wallet-http-owner').is_file()
    assert not (tmp_path/'config').exists()
    installer.install(args, run=run)
    assert len(attempts) == 2
    assert (tmp_path/'config/installation.json').exists()


def test_install_emits_provisioning_issuance_and_renewal_commands_in_order(tmp_path, monkeypatch):
    import types
    installer = module(); args = options(tmp_path, '--agree-acme-tos')
    fake_pwd = types.SimpleNamespace(getpwnam=lambda name: types.SimpleNamespace(pw_uid=123, pw_gid=456))
    monkeypatch.setitem(sys.modules, 'pwd', fake_pwd)
    monkeypatch.setattr(installer.shutil, 'which', lambda name: {'apt-get': '/usr/bin/apt-get', 'ufw': '/usr/sbin/ufw'}.get(name))
    monkeypatch.setattr(installer, 'verify_health', lambda *a, **kw: None)
    monkeypatch.setattr(installer, 'verify_https_health', lambda *a, **kw: None)
    emitted = []
    def run(command, **kwargs):
        emitted.append(command)
        if command[:2] == ['docker', 'inspect']:
            return subprocess.CompletedProcess(command, 1, stdout='', stderr='Error: No such object: rtm-wallet-http')
        stdout = 'Status: active\n' if command == ['/usr/sbin/ufw', 'status'] else ''
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr='')
    result = installer.install(args, run=run)
    assert result['gateway_bind'] == '127.0.0.1:8090'
    assert ['apt-get', 'install', '-y', 'python3-venv', 'nginx'] in emitted
    assert ['/usr/bin/python3', '-m', 'venv', str(tmp_path/'certbot')] in emitted
    pip = next(command for command in emitted if 'pip' in command)
    assert pip[-1] == 'certbot>=5.4,<6' and pip[pip.index('--index-url')+1] == 'https://pypi.org/simple'
    issuance = next(command for command in emitted if 'certonly' in command)
    assert issuance[issuance.index('--ip-address')+1] == '144.202.18.233'
    assert emitted.index(['systemctl', 'reload', 'nginx']) < emitted.index(issuance)
    assert ['systemctl', 'enable', '--now', 'rtm-wallet-cert-renew.timer'] in emitted
    assert ['/usr/sbin/ufw', 'allow', '443/tcp', 'comment', 'RTM wallet HTTPS'] in emitted
    assert not any('rtm-electrumx-assets.service' in command for command in emitted)
    assert 'listen 443 ssl default_server;' in (tmp_path/'nginx/rtm-wallet-http.conf').read_text()
