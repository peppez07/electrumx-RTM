#!/usr/bin/env python3
"""Install a password-free wallet HTTPS gateway on Linux with trusted ACME TLS.

Staging and --dry-run require only Python's standard library. Live installation
requires root, systemd and apt (Debian/Ubuntu); it never reads Core credentials.
"""

import argparse
import http.client
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen

MARKER = '# Managed by electrumx-RTM install_wallet_https.py'
SERVICE = 'rtm-wallet-http.service'
RENEW = 'rtm-wallet-cert-renew.service'
TIMER = 'rtm-wallet-cert-renew.timer'
SITE = 'rtm-wallet-http.conf'
CERT_NAME = 'rtm-wallet-http'
CONTAINER = 'rtm-wallet-http'


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--image', help='Existing Docker image built from this checkout')
    mode.add_argument('--venv', help='Installed ElectrumX Python environment under /opt')
    p.add_argument('--reinstall', action='store_true', help='Restore the saved installation options')
    p.add_argument('--public-host', help='Public IPv4 address or DNS name resolving to this server')
    p.add_argument('--gateway-port', type=int, default=8090)
    p.add_argument('--electrum-port', type=int, default=50002)
    p.add_argument('--explorer-port', type=int, default=0, help='Local status source; 0 disables status')
    p.add_argument('--agree-acme-tos', action='store_true', help="Accept Let's Encrypt terms for this installation")
    p.add_argument('--email', help='Optional ACME account contact email')
    p.add_argument('--config-dir', default='/etc/rtm-wallet-http')
    p.add_argument('--state-dir', default='/var/lib/rtm-wallet-http')
    p.add_argument('--certbot-venv', default='/opt/rtm-wallet-certbot')
    p.add_argument('--letsencrypt-dir', default='/etc/letsencrypt/rtm-wallet-http')
    p.add_argument('--systemd-dir', default='/etc/systemd/system', help=argparse.SUPPRESS)
    p.add_argument('--nginx-dir', default='/etc/nginx/conf.d', help=argparse.SUPPRESS)
    p.add_argument('--dry-run', action='store_true')
    return p


def parse_options(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    p = parser(); args = p.parse_args(argv)
    if args.reinstall:
        path = Path(args.config_dir)/'installation.json'
        try:
            saved = json.loads(path.read_text())
            if saved['installer'] != MARKER:
                raise ValueError('Unrelated installation manifest.')
            p.set_defaults(**saved['options'])
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise ValueError('No complete managed installation found; repeat the original install options.') from error
        args = p.parse_args(argv)
        if '--image' in argv:
            args.venv = None
        if '--venv' in argv:
            args.image = None
    validate(args)
    return args


def validate(args):
    if bool(args.image) == bool(args.venv):
        raise ValueError('Select --image or --venv, or use --reinstall for a saved installation.')
    host = args.public_host or ''
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        labels = host.split('.')
        if (len(labels) < 2 or len(host) > 253 or host.endswith('.') or
                re.fullmatch(r'[0-9.]+', host) or any(not re.fullmatch(
                    r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label) for label in labels)):
            raise ValueError('Public host must be a public IPv4 address or DNS name, without URL syntax.')
    else:
        if address.version != 4 or not address.is_global:
            raise ValueError('Public host must be a globally routable IPv4 address or DNS name.')
    ports = [80, 443, args.gateway_port, args.electrum_port]
    if args.explorer_port:
        ports.append(args.explorer_port)
    if any(not 1 <= port <= 65535 for port in ports) or len(set(ports)) != len(ports):
        raise ValueError('Gateway, Electrum, explorer and HTTPS ports must be valid and distinct.')
    if args.explorer_port < 0:
        raise ValueError('Explorer port must be 0 or a valid TCP port.')
    if args.image and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/@-]*', args.image):
        raise ValueError('Invalid Docker image name.')
    if args.email and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', args.email):
        raise ValueError('Invalid ACME contact email.')
    for key in ('config_dir', 'state_dir', 'certbot_venv', 'letsencrypt_dir', 'systemd_dir', 'nginx_dir', 'venv'):
        value = getattr(args, key)
        if value and (not (Path(value).is_absolute() or PurePosixPath(value).is_absolute()) or
                      any(ord(c) < 32 or c in '\"%$;`' for c in value)):
            raise ValueError('Installation paths must be absolute without control characters or shell metacharacters.')
    if args.venv and sys.platform == 'linux' and any(
            Path(base) == Path(args.venv).resolve() or Path(base) in Path(args.venv).resolve().parents
            for base in ('/root', '/home')):
        raise ValueError('The service virtual environment must be outside protected home directories.')


def summary(args):
    return {'wallet_url': 'https://'+args.public_host+'/api/v1/electrum',
            'health_url': 'https://'+args.public_host+'/api/v1/health',
            'gateway_bind': '127.0.0.1:'+str(args.gateway_port),
            'electrum_upstream': '127.0.0.1:'+str(args.electrum_port),
            'explorer_port': args.explorer_port, 'service': SERVICE,
            'certificate_profile': 'shortlived' if is_ip(args.public_host) else 'default',
            'requires_provider_firewall_tcp': [80, 443]}


def is_ip(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def quote(value):
    return '"'+str(value).replace('\\', '\\\\').replace('"', '\\"')+'"'


def atomic_write(path, text, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.'+path.name+'.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', newline='\n') as output:
            output.write(text)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def managed_paths(args):
    return [Path(args.systemd_dir)/name for name in (SERVICE, RENEW, TIMER)] + [
        Path(args.nginx_dir)/SITE, Path(args.config_dir)/'gateway.env']


def check_files(args):
    validate(args)
    for path in managed_paths(args):
        if path.is_symlink() or (path.exists() and not path.read_text().startswith(MARKER+'\n')):
            raise ValueError('Refusing to replace unrelated file: '+str(path))
    manifest = Path(args.config_dir)/'installation.json'
    if manifest.is_symlink():
        raise ValueError('Refusing an unrelated installation manifest symlink.')
    if manifest.exists():
        saved = json.loads(manifest.read_text())
        if saved.get('installer') != MARKER:
            raise ValueError('Refusing an unrelated installation manifest.')
        installed = saved.get('options', {})
        for key in ('public_host', 'gateway_port', 'electrum_port', 'explorer_port',
                    'config_dir', 'state_dir', 'systemd_dir', 'nginx_dir', 'letsencrypt_dir'):
            if installed.get(key) != getattr(args, key):
                raise ValueError('Existing '+key+' differs; use the installed host/ports/paths with --reinstall.')
    certbot = Path(args.certbot_venv)
    owner = certbot/'.rtm-wallet-http-owner'
    if certbot.exists() and (not owner.is_file() or owner.read_text() != MARKER+'\n'):
        raise ValueError('Refusing an unrelated Certbot environment: '+str(certbot))
    certificates = Path(args.letsencrypt_dir)
    if certificates.exists() and any(certificates.iterdir()) and not manifest.exists():
        raise ValueError('Refusing unrelated certificate state without the matching installation manifest.')
    # Conservative ownership: dedicated 80/443 setup. Existing unrelated sites
    # remain untouched; an administrator must explicitly arrange coexistence.
    directories = [Path(args.nginx_dir)]
    if Path(args.nginx_dir) == Path('/etc/nginx/conf.d'):
        directories += [Path('/etc/nginx/sites-enabled')]
    for directory in directories:
        if not directory.exists():
            continue
        for path in directory.iterdir():
            if path.name == SITE and directory == Path(args.nginx_dir):
                continue
            if not path.is_file() or (directory.name != 'sites-enabled' and path.suffix != '.conf'):
                continue
            text = re.sub(r'#.*', '', path.read_text())
            if re.search(r'\blisten\s+(?:\[[^]]+\]:|[0-9.*]+:)?(?:80|443)(?:\s|;)', text):
                raise ValueError('Existing nginx site uses port 80/443: '+str(path)+
                                 '; arrange a dedicated listener before installation.')


def gateway_unit(args, uid, gid):
    env = Path(args.config_dir)/'gateway.env'
    if args.image:
        identity = ''
        dependency = 'Requires=docker.service\nAfter=docker.service network-online.target\n'
        command = ('/usr/bin/docker run --rm --name '+CONTAINER+
            ' --label io.rtm.wallet-http=managed --network host --user '+str(uid)+':'+str(gid)+
            ' --cap-drop ALL --security-opt no-new-privileges --read-only --memory 192m --cpus 0.5'
            ' --tmpfs /tmp:rw,noexec,nosuid,size=16m --env-file '+quote(env)+' '+quote(args.image)+
            ' python -m electrumx.http_gateway')
        stop = 'ExecStop=/usr/bin/docker stop -t 15 '+CONTAINER+'\n'
    else:
        identity = 'User=rtm-wallet-http\nGroup=rtm-wallet-http\n'
        dependency = 'After=network-online.target\n'
        command = quote(Path(args.venv)/'bin/python')+' -m electrumx.http_gateway'
        stop = ''
    return (MARKER+'\n[Unit]\nDescription=RTM public wallet HTTPS loopback gateway\n'
            'Wants=network-online.target\n'+dependency+'[Service]\n'+identity+
            'EnvironmentFile='+str(env)+'\nExecStart='+command+'\n'+stop+
            'Restart=on-failure\nRestartSec=5\nTimeoutStopSec=30\n'
            'NoNewPrivileges=true\nPrivateTmp=true\nProtectHome=true\nProtectSystem=strict\n'
            'InaccessiblePaths=-/etc/rtm-electrumx -/var/lib/rtm-mainnet\n'
            'MemoryMax=256M\nTasksMax=64\n[Install]\nWantedBy=multi-user.target\n')


def nginx_text(args, tls):
    challenge = ('    location ^~ /.well-known/acme-challenge/ {\n'
                 '        root '+quote(Path(args.state_dir)/'acme')+';\n'
                 '        default_type text/plain;\n    }\n')
    text = (MARKER+'\nserver {\n    listen 80;\n    server_name '+args.public_host+';\n'+challenge+
            ('    location / { return 308 https://'+args.public_host+'$request_uri; }\n' if tls
             else '    location / { return 503; }\n')+'}\n')
    if not tls:
        return text
    cert = Path(args.letsencrypt_dir)/'live'/CERT_NAME
    return text+('server {\n    listen 443 ssl default_server;\n    server_name '+args.public_host+';\n'
        '    ssl_certificate '+quote(cert/'fullchain.pem')+';\n'
        '    ssl_certificate_key '+quote(cert/'privkey.pem')+';\n'
        '    ssl_protocols TLSv1.2 TLSv1.3;\n'
        '    client_max_body_size 1m;\n    client_body_timeout 15s;\n'
        '    location ^~ /api/v1/ {\n        proxy_pass http://127.0.0.1:'+str(args.gateway_port)+';\n'
        '        proxy_http_version 1.1;\n        proxy_set_header Host $host;\n'
        '        proxy_set_header Connection "";\n        proxy_set_header X-Forwarded-Proto https;\n'
        '        proxy_connect_timeout 5s;\n        proxy_read_timeout 40s;\n'
        '        proxy_send_timeout 15s;\n        proxy_next_upstream off;\n'
        '        proxy_buffering off;\n    }\n    location / { return 404; }\n}\n')


def certbot_base(args):
    return [str(Path(args.certbot_venv)/'bin/certbot'), '--config-dir', args.letsencrypt_dir,
            '--work-dir', str(Path(args.state_dir)/'certbot-work'),
            '--logs-dir', str(Path(args.state_dir)/'certbot-logs')]


def certificate_command(args):
    command = certbot_base(args)+['certonly', '--non-interactive', '--webroot', '-w',
        str(Path(args.state_dir)/'acme'), '--preferred-challenges', 'http',
        '--cert-name', CERT_NAME, '--keep-until-expiring']
    if args.agree_acme_tos:
        command.append('--agree-tos')
    if args.email:
        command += ['--email', args.email]
    else:
        command.append('--register-unsafely-without-email')
    if is_ip(args.public_host):
        command += ['--ip-address', args.public_host, '--required-profile', 'shortlived']
    else:
        command += ['-d', args.public_host]
    return command


def renewal_unit(args):
    command = certbot_base(args)+['renew', '--non-interactive', '--cert-name', CERT_NAME,
                                 '--deploy-hook', '/usr/bin/systemctl reload nginx']
    if is_ip(args.public_host):
        command += ['--required-profile', 'shortlived']
    return (MARKER+'\n[Unit]\nDescription=Renew trusted RTM wallet HTTPS certificate\n'
            'Wants=network-online.target\nAfter=network-online.target nginx.service\n'
            '[Service]\nType=oneshot\nExecStart='+' '.join(quote(part) for part in command)+'\n'
            'PrivateTmp=true\nProtectHome=true\nNoNewPrivileges=true\n')


def stage(args, uid, gid, tls=None):
    check_files(args)
    site = Path(args.nginx_dir)/SITE
    if tls is None:
        tls = site.exists() and 'listen 443 ssl' in site.read_text()
    env = (MARKER+'\nWALLET_HTTP_HOST=127.0.0.1\nWALLET_HTTP_PORT='+str(args.gateway_port)+
           '\nWALLET_ELECTRUM_HOST=127.0.0.1\nWALLET_ELECTRUM_PORT='+str(args.electrum_port)+
           '\nWALLET_EXPLORER_PORT='+str(args.explorer_port)+'\n')
    for directory in (Path(args.config_dir), Path(args.state_dir)/'acme/.well-known/acme-challenge',
                      Path(args.systemd_dir), Path(args.nginx_dir)):
        directory.mkdir(parents=True, exist_ok=True)
    atomic_write(Path(args.config_dir)/'gateway.env', env)
    atomic_write(Path(args.systemd_dir)/SERVICE, gateway_unit(args, uid, gid))
    atomic_write(Path(args.systemd_dir)/RENEW, renewal_unit(args))
    atomic_write(Path(args.systemd_dir)/TIMER, MARKER+
        '\n[Unit]\nDescription=Check RTM wallet certificate renewal twice daily\n'
        '[Timer]\nOnBootSec=5min\nOnUnitActiveSec=12h\nRandomizedDelaySec=30min\n'
        'Persistent=true\nUnit='+RENEW+'\n[Install]\nWantedBy=timers.target\n')
    atomic_write(site, nginx_text(args, tls))
    result = summary(args)
    saved = {key: value for key, value in vars(args).items() if key not in ('reinstall', 'dry_run')}
    atomic_write(Path(args.config_dir)/'installation.json',
                 json.dumps(dict(result, installer=MARKER, options=saved), indent=2)+'\n')
    return result


def preflight(args, run=subprocess.run):
    check_files(args)
    if not args.agree_acme_tos:
        raise ValueError('Certificate issuance requires explicit --agree-acme-tos (review https://letsencrypt.org/repository/).')
    if shutil.which('nginx'):
        active = run(['nginx', '-T'], check=True, capture_output=True, text=True).stdout
        source = ''
        for line in active.splitlines():
            if line.startswith('# configuration file ') and line.endswith(':'):
                source = line[len('# configuration file '):-1]
                continue
            if Path(source) == Path(args.nginx_dir)/SITE:
                continue
            if re.search(r'\blisten\s+(?:\[[^]]+\]:|[0-9.*]+:)?(?:80|443)(?:\s|;)', line.split('#', 1)[0]):
                raise ValueError('Existing nginx listener in '+source+' uses port 80/443; resolve the collision first.')
    container_pid = None
    if args.image:
        # A stopped container still reserves its name. Check independently of
        # the TCP listener before creating a unit whose ExecStop uses that name.
        inspected = run(['docker', 'inspect', '--type', 'container', '--format',
            '{{ index .Config.Labels "io.rtm.wallet-http" }} {{.State.Pid}}', CONTAINER],
            capture_output=True, text=True)
        if inspected.returncode:
            absent = re.fullmatch(r'(?:Error: |Error response from daemon: )?No such (?:container|object): '+
                                  re.escape(CONTAINER), inspected.stderr.strip())
            if not absent:
                raise ValueError('Cannot inspect the Docker container name; check Docker daemon access before retrying.')
        else:
            parts = inspected.stdout.split()
            managed_setup = all(path.is_file() for path in (
                Path(args.systemd_dir)/SERVICE, Path(args.config_dir)/'gateway.env',
                Path(args.config_dir)/'installation.json'))
            if len(parts) != 2 or parts[0] != 'managed' or not parts[1].isdecimal() or not managed_setup:
                raise ValueError('Existing container '+CONTAINER+' does not belong to this managed installation; preserve it and choose an unused setup.')
            container_pid = parts[1]
    listeners = run(['ss', '-H', '-ltnp'], check=True, capture_output=True, text=True).stdout
    for line in listeners.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        try:
            port = int(fields[3].rsplit(':', 1)[1])
        except (IndexError, ValueError):
            continue
        if port not in (80, 443, args.gateway_port):
            continue
        if port in (80, 443) and '"nginx"' in line and (Path(args.nginx_dir)/SITE).exists():
            continue
        if port == args.gateway_port and (Path(args.systemd_dir)/SERVICE).exists():
            pid = container_pid if args.image else run(
                ['systemctl', 'show', SERVICE, '--property=MainPID', '--value'],
                check=True, capture_output=True, text=True).stdout.strip()
            if pid and pid.isdecimal() and int(pid) > 0 and re.search(r'\bpid='+pid+r'\b', line):
                continue
        raise ValueError('Port '+str(port)+' is occupied by another listener; preserve it and resolve the collision first.')


def verify_health(args, attempts=1):
    """Gateway liveness is valid even while the private index is synchronizing."""
    error = None
    for attempt in range(attempts):
        try:
            with urlopen('http://127.0.0.1:'+str(args.gateway_port)+'/api/v1/health', timeout=5) as response:
                data = json.load(response)
                if response.status != 200 or not isinstance(data, dict) or data.get('ok') is not True:
                    raise ValueError('Unexpected gateway health response.')
            return
        except (OSError, ValueError) as failure:
            error = failure
            if attempt+1 < attempts:
                time.sleep(1)
    raise ValueError('Gateway health check failed; inspect journalctl -u '+SERVICE) from error


def verify_https_health(args):
    """Use system CA trust and the public identity, connected over local TLS."""
    connection = http.client.HTTPSConnection(args.public_host, 443, timeout=5)
    try:
        connection.sock = ssl.create_default_context().wrap_socket(
            socket.create_connection(('127.0.0.1', 443), timeout=5), server_hostname=args.public_host)
        connection.request('GET', '/api/v1/health')
        response = connection.getresponse()
        data = json.loads(response.read(65536))
        if response.status != 200 or not isinstance(data, dict) or data.get('ok') is not True:
            raise ValueError('Unexpected HTTPS gateway health response.')
    finally:
        connection.close()


def open_firewall(run=subprocess.run):
    ufw = shutil.which('ufw')
    if not ufw:
        return
    status = run([ufw, 'status'], capture_output=True, text=True, env=dict(os.environ, LC_ALL='C'))
    if status.returncode == 0 and 'Status: active' in status.stdout:
        for port in (80, 443):
            run([ufw, 'allow', str(port)+'/tcp', 'comment', 'RTM wallet HTTPS'], check=True)


def install(args, run=subprocess.run):
    """Live path; run is injectable so command sequencing can be tested off-host."""
    preflight(args, run=run)
    smoke = 'import electrumx.http_gateway'
    run(['docker', 'run', '--rm', '--network', 'none', args.image, 'python', '-I', '-c', smoke]
        if args.image else [str(Path(args.venv)/'bin/python'), '-I', '-c', smoke], check=True)
    # nginx creates a default site on Debian. Remove only the symlink created by
    # this first package install; all pre-existing configuration was preflighted.
    fresh_nginx = not Path('/etc/nginx').exists()
    packages = ['python3-venv']+(['nginx'] if not shutil.which('nginx') else [])
    if not shutil.which('apt-get'):
        raise ValueError('Live setup currently requires apt-get (Debian/Ubuntu).')
    run(['apt-get', 'update'], check=True)
    run(['apt-get', 'install', '-y', *packages], check=True)
    default = Path('/etc/nginx/sites-enabled/default')
    if fresh_nginx and default.is_symlink() and default.resolve() == Path('/etc/nginx/sites-available/default'):
        default.unlink()
    check_files(args)
    certbot_python = Path(args.certbot_venv)/'bin/python'
    if Path(args.certbot_venv).exists():
        # check_files already proved ownership. A failed initial pip download
        # can leave a valid venv without Certbot; retry pip in that environment.
        if not certbot_python.is_file() or not os.access(certbot_python, os.X_OK):
            raise ValueError('Owned Certbot environment lacks an executable Python; inspect it before proceeding.')
    else:
        run(['/usr/bin/python3', '-m', 'venv', args.certbot_venv], check=True)
        atomic_write(Path(args.certbot_venv)/'.rtm-wallet-http-owner', MARKER+'\n')
    run([str(certbot_python), '-m', 'pip', 'install',
         '--index-url', 'https://pypi.org/simple', 'certbot>=5.4,<6'], check=True)
    import pwd
    try:
        user = pwd.getpwnam('rtm-wallet-http')
    except KeyError:
        run(['useradd', '--system', '--user-group', '--home-dir', '/nonexistent',
             '--shell', '/usr/sbin/nologin', 'rtm-wallet-http'], check=True)
        user = pwd.getpwnam('rtm-wallet-http')
    result = stage(args, user.pw_uid, user.pw_gid)
    run(['systemctl', 'daemon-reload'], check=True)
    run(['systemctl', 'enable', '--now', SERVICE], check=True)
    run(['systemctl', 'restart', SERVICE], check=True)
    verify_health(args, attempts=20)
    run(['nginx', '-t'], check=True)
    run(['systemctl', 'enable', '--now', 'nginx'], check=True)
    run(['systemctl', 'reload', 'nginx'], check=True)
    open_firewall(run=run)
    print('Allow inbound TCP 80 and 443 in the Vultr/provider firewall before ACME validation.', flush=True)
    run(certificate_command(args), check=True)
    stage(args, user.pw_uid, user.pw_gid, tls=True)
    run(['nginx', '-t'], check=True)
    run(['systemctl', 'reload', 'nginx'], check=True)
    run(['systemctl', 'enable', '--now', TIMER], check=True)
    verify_https_health(args)
    return result


def main(argv=None):
    p = parser()
    try:
        args = parse_options(argv)
        check_files(args)
        if args.dry_run:
            print(json.dumps(summary(args)))
            return 0
        if sys.platform != 'linux' or os.geteuid() != 0:
            raise ValueError('Live installation requires Linux systemd and root; use --dry-run elsewhere.')
        if Path(args.systemd_dir) != Path('/etc/systemd/system') or Path(args.nginx_dir) != Path('/etc/nginx/conf.d'):
            raise ValueError('Live installation must use the standard systemd and nginx directories.')
        result = install(args)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print('Wallet HTTPS setup refused: '+str(error), file=sys.stderr)
        return 2
    print(json.dumps(result))
    print('HTTPS gateway is healthy. Chain balances/transfers require a synchronized ElectrumX index.')
    print('Renewal: systemctl status '+TIMER+'. Keep provider TCP 80/443 open for renewal.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
