#!/usr/bin/env python3
'''Install one mainnet asset-capable ElectrumX service and optional explorer.'''

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import sys
from urllib.parse import quote, urlsplit

ROOT=Path(__file__).resolve().parents[1]
MARKER='# Managed by electrumx-RTM install_stack.py'
SERVICE='rtm-electrumx-assets.service'
TIMER='rtm-electrumx-assets.timer'
spec=importlib.util.spec_from_file_location('rtm_config',ROOT/'electrumx/explorer/config.py')
config_module=importlib.util.module_from_spec(spec);spec.loader.exec_module(config_module)
https_spec=importlib.util.spec_from_file_location('rtm_wallet_https',ROOT/'contrib/install_wallet_https.py')
https_module=importlib.util.module_from_spec(https_spec);https_spec.loader.exec_module(https_module)


def profile(choice,total_ram=None):
    if total_ram is None:
        try:
            line=next(line for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemTotal:'))
            total_ram=int(line.split()[1])*1024
        except (OSError,StopIteration,ValueError):
            total_ram=2*1024**3
    low=choice == 'low' or (choice == 'auto' and total_ram < 4*1024**3)
    return {'name':'low' if low else 'standard','cache_mb':16 if low else 64,
            'memory_mib':1024 if low else 1536,'swap_total_mib':1536 if low else 2048,
            'cpu_percent':50 if low else 100,'max_sessions':50 if low else 200}


def parser():
    p=argparse.ArgumentParser(description=__doc__)
    mode=p.add_mutually_exclusive_group()
    mode.add_argument('--image',help='Existing local Docker image built from this checkout')
    mode.add_argument('--venv',help='Existing installed Python environment, preferably under /opt')
    p.add_argument('--reinstall',action='store_true',help='Restore complete saved install options')
    p.add_argument('--core-config',default='/var/lib/rtm-mainnet/core/raptoreum.conf')
    p.add_argument('--config-dir',default='/etc/rtm-electrumx')
    p.add_argument('--runtime-dir',default='/opt/rtm-electrumx')
    p.add_argument('--db-dir',default='/var/lib/rtm-electrumx/assets-v1')
    p.add_argument('--systemd-dir',default='/etc/systemd/system',help=argparse.SUPPRESS)
    p.add_argument('--profile',choices=['auto','low','standard'],default='auto')
    p.add_argument('--tcp-port',type=int,default=50002)
    p.add_argument('--ws-port',type=int,default=50003)
    p.add_argument('--rpc-port',type=int,default=8001)
    p.add_argument('--wss-port',type=int,default=50004)
    p.add_argument('--tls-cert');p.add_argument('--tls-key')
    p.add_argument('--no-tls',action='store_true',help='Explicitly disable a previously configured WSS listener')
    p.add_argument('--with-explorer','--explorer',dest='with_explorer',action='store_true')
    p.add_argument('--port',type=int,default=8080,help='Explorer HTTP port')
    p.add_argument('--bind',choices=['0.0.0.0','127.0.0.1'],default='0.0.0.0',help='Explorer bind address')
    p.add_argument('--public-host')
    p.add_argument('--wallet-https',action='store_true',help='Install trusted password-free wallet HTTPS on 443')
    p.add_argument('--gateway-port',type=int,default=8090)
    p.add_argument('--https-config-dir',default='/etc/rtm-wallet-http')
    p.add_argument('--agree-acme-tos',action='store_true',help="Accept Let's Encrypt terms for wallet HTTPS")
    p.add_argument('--email',help='Optional ACME contact email')
    wallet=p.add_mutually_exclusive_group()
    wallet.add_argument('--wallet-api',dest='wallet_api',action='store_const',const=True)
    wallet.add_argument('--no-wallet-api',dest='wallet_api',action='store_const',const=False)
    p.set_defaults(wallet_api=None)
    p.add_argument('--dry-run',action='store_true')
    return p


def parse_options(argv=None):
    argv=list(sys.argv[1:] if argv is None else argv)
    p=parser();args=p.parse_args(argv)
    if args.reinstall:
        try:
            saved=json.loads((Path(args.config_dir)/'installation.json').read_text())
            p.set_defaults(**saved['options'])
        except (OSError,KeyError,TypeError,ValueError) as error:
            raise ValueError('No complete saved installation; repeat the original install options once.') from error
        args=p.parse_args(argv)
        if '--image' in argv:args.venv=None
        if '--venv' in argv:args.image=None
    validate(args)
    return args


def wallet_https_argv(args):
    options=['--image',args.image] if args.image else ['--venv',args.venv]
    options+=['--public-host',args.public_host or '', '--electrum-port',str(args.tcp_port),
              '--gateway-port',str(args.gateway_port),'--explorer-port',str(args.port if args.with_explorer else 0),
              '--config-dir',args.https_config_dir]
    if args.agree_acme_tos:options+=['--agree-acme-tos']
    if args.email:options+=['--email',args.email]
    # On subsequent runs the HTTPS installer also restores its certificate paths.
    if (Path(args.https_config_dir)/'installation.json').exists():options.insert(0,'--reinstall')
    return options


def wallet_https_options(args):
    return https_module.parse_options(wallet_https_argv(args))


def validate(args):
    if bool(args.image) == bool(args.venv):
        raise ValueError('Select --image or --venv, or use --reinstall for a saved installation.')
    ports=[args.tcp_port,args.ws_port,args.rpc_port]
    if bool(args.tls_cert) != bool(args.tls_key):
        raise ValueError('Supply both --tls-cert and --tls-key.')
    if args.no_tls and args.tls_cert:
        raise ValueError('Do not combine TLS paths and --no-tls.')
    if args.tls_cert:ports.append(args.wss_port)
    if args.with_explorer:ports.append(args.port)
    if args.wallet_https:
        ports.extend((80,443,args.gateway_port))
        wallet_https_options(args)
    if any(not 1 <= n <= 65535 for n in ports) or len(set(ports)) != len(ports):
        raise ValueError('Service ports must be valid and distinct.')
    for value in (args.core_config,args.config_dir,args.runtime_dir,args.db_dir,args.systemd_dir,args.https_config_dir,args.venv or ''):
        if any(ord(c) < 32 or c in '"%' for c in value):
            raise ValueError('Paths cannot contain control characters, quotes or percent signs.')
    if args.image and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/@-]*',args.image):
        raise ValueError('Invalid Docker image name.')
    if args.public_host and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*',args.public_host):
        raise ValueError('Public host must be an IPv4 address or domain.')
    if args.venv and sys.platform == 'linux':
        if any(Path(base) == Path(args.venv).resolve() or Path(base) in Path(args.venv).resolve().parents
               for base in ('/root','/home')):
            raise ValueError('A system service virtual environment must be outside protected home directories.')


def summary(args):
    limits=profile(args.profile)
    return {'network':'mainnet','service':SERVICE,'profile':limits['name'],
        'cache_mb':limits['cache_mb'],'backend_memory_mib':limits['memory_mib'],
        'waits_for_core_sync':True,'db_directory':str(Path(args.db_dir).resolve()),
        'tcp_port':args.tcp_port,'ws_port':args.ws_port,
        'explorer_url':'http://{}:{}/'.format(args.public_host or 'SERVER_IP',args.port) if args.with_explorer else None,
        'wallet_https_url':'https://'+args.public_host+'/api/v1/electrum' if args.wallet_https else None}


def unit_text(args,uid,gid):
    limits=profile(args.profile)
    config=Path(args.config_dir).resolve();runtime=Path(args.runtime_dir).resolve()
    data=Path(args.db_dir).resolve()
    q=lambda value:'"'+str(value).replace('\\','\\\\').replace('"','\\"')+'"'
    if args.image:
        command=('/usr/bin/docker run --rm --name rtm-electrumx-assets --network host '
            '--user {}:{} --memory {}m --memory-swap {}m --cpus {} '
            '--cap-drop ALL --security-opt no-new-privileges --read-only '
            '--tmpfs /tmp:rw,noexec,nosuid,size=16m --env-file {} -v {} -v {} {} electrumx_server').format(
            uid,gid,limits['memory_mib'],limits['swap_total_mib'],limits['cpu_percent']/100,
            q(config/'server.env'),q(str(data)+':/db'),q(str(config)+':/config:ro'),q(args.image))
        condition='/usr/bin/python3'
        identity=''
        dependency='Requires=docker.service\nAfter=docker.service network-online.target\n'
        stop='ExecStop=/usr/bin/docker stop -t 90 rtm-electrumx-assets\n'
        start_pre='ExecStartPre=-/usr/bin/docker rm rtm-electrumx-assets\n'
    else:
        command=q(Path(args.venv).resolve()/'bin/electrumx_server')
        condition=q(Path(args.venv).resolve()/'bin/python')
        identity='User=rtm-electrumx\nGroup=rtm-electrumx\n'
        dependency='After=network-online.target\n'
        stop=''
        start_pre=''
    return (MARKER+'\n[Unit]\nDescription=ElectrumX RTM mainnet coins and assets\nWants=network-online.target\n'+dependency+
        '[Service]\n'+identity+'EnvironmentFile='+str(config/'server.env')+'\n'
        'ExecCondition='+condition+' '+q(runtime/'core_ready.py')+' --config '+q(config/'core-rpc.json')+'\n'
        +start_pre+'ExecStart='+command+'\n'+stop+'Restart=on-failure\nRestartSec=30\nTimeoutStopSec=120\n'
        'NoNewPrivileges=true\nPrivateTmp=true\nProtectHome=true\nProtectSystem=strict\n'
        'ReadWritePaths='+q(data)+'\nMemoryMax='+str(limits['memory_mib'])+'M\n'
        'MemorySwapMax='+str(limits['swap_total_mib']-limits['memory_mib'])+'M\n'
        'CPUQuota='+str(limits['cpu_percent'])+'%\n[Install]\nWantedBy=multi-user.target\n')


def owned(path):
    if not path.exists():return True
    text=path.read_text()
    return MARKER in text or (
        path.name == SERVICE and 'Description=ElectrumX RTM asset index after mainnet synchronization' in text) or (
        path.name == TIMER and 'Description=Start the RTM asset index when mainnet Core is ready' in text)


def stage(args,uid,gid):
    validate(args)
    if args.wallet_https:https_module.check_files(wallet_https_options(args))
    core=config_module.load_core_config(args.core_config)
    settings=config_module.read_core_settings(args.core_config)
    enabled=lambda key:settings.get(key,'0').lower() in ('1','true')
    # ElectrumX indexes assets itself from raw blocks and serves confirmed
    # transactions through blockchain.transaction.get_in_block, so it runs beside
    # an unmodified node (for example a smartnode). The explorer reads Core's
    # transaction and asset indexes directly and still needs both.
    if args.with_explorer and not (enabled('txindex') and enabled('assetindex')):
        raise ValueError('The explorer requires Core txindex=1 and assetindex=1; configure/reindex Core explicitly or install without --with-explorer.')
    if settings.get('prune','0').lower() not in ('0','false'):
        raise ValueError('Core must be unpruned (prune=0).')
    if urlsplit(core['url']).port in (args.tcp_port,args.ws_port,args.rpc_port,args.port if args.with_explorer else 0,
                                     args.gateway_port if args.wallet_https else 0):
        raise ValueError('An ElectrumX/explorer port conflicts with Core RPC.')
    units=Path(args.systemd_dir);config=Path(args.config_dir);runtime=Path(args.runtime_dir);data=Path(args.db_dir)
    if any(not owned(units/name) for name in (SERVICE,TIMER)):
        raise ValueError('Refusing to replace an unrelated service or timer.')
    if (data/'chainstate').exists() or (data/'blocks/index').exists():
        raise ValueError('The ElectrumX database must be separate from Core data.')
    existing_rpc=config/'core-rpc.json'
    if existing_rpc.exists() and json.loads(existing_rpc.read_text()) != core:
        raise ValueError('Existing RPC settings differ; update the managed private configuration explicitly.')
    env_path=config/'server.env'
    env=dict(line.split('=',1) for line in env_path.read_text().splitlines()
             if '=' in line and not line.startswith('#')) if env_path.exists() else {}
    base_services='tcp://127.0.0.1:{},ws://127.0.0.1:{},rpc://127.0.0.1:{}'.format(args.tcp_port,args.ws_port,args.rpc_port)
    if env.get('SERVICES'):
        old_base=','.join(part for part in env['SERVICES'].split(',') if not part.startswith('wss://'))
        if old_base != base_services:
            raise ValueError('Existing ports differ; repeat the installed port options rather than replacing them silently.')
    manifest=config/'installation.json'
    if manifest.exists() and json.loads(manifest.read_text()).get('db_directory') != str(data.resolve()):
        raise ValueError('Existing database path differs; select the installed database explicitly.')
    tls_cert,tls_key=args.tls_cert,args.tls_key
    if not tls_cert and not args.no_tls and env.get('SSL_CERTFILE'):
        tls_cert,tls_key=str(config/'tls.crt'),str(config/'tls.key')
    if tls_cert:
        if args.wss_port == urlsplit(core['url']).port:
            raise ValueError('The WSS port conflicts with Core RPC.')
        if args.wss_port in (args.tcp_port,args.ws_port,args.rpc_port,args.port if args.with_explorer else 0):
            raise ValueError('The WSS port conflicts with another service.')
        old_wss=[part for part in env.get('SERVICES','').split(',') if part.startswith('wss://')]
        if old_wss and old_wss != ['wss://0.0.0.0:'+str(args.wss_port)]:
            raise ValueError('Repeat the installed WSS port option.')
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(tls_cert,tls_key)
    for directory in (units,config,runtime,data):directory.mkdir(parents=True,exist_ok=True)
    config.chmod(0o750);runtime.chmod(0o755)
    if not existing_rpc.exists():
        fd=os.open(str(existing_rpc),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o640)
        with os.fdopen(fd,'w') as output:json.dump(core,output)
    limits=profile(args.profile)
    url=urlsplit(core['url'])
    host='['+url.hostname+']' if ':' in url.hostname else url.hostname
    daemon='http://{}:{}@{}:{}/'.format(quote(core['username'],safe=''),quote(core['password'],safe=''),host,url.port)
    env.update(COIN='Raptoreum',NET='mainnet',DB_DIRECTORY='/db' if args.image else str(data.resolve()),
        DB_ENGINE='leveldb',DAEMON_URL=daemon,SERVICES=base_services,CACHE_MB=str(limits['cache_mb']),
        MAX_SESSIONS=str(limits['max_sessions']),PEER_DISCOVERY='off',PEER_ANNOUNCE='false')
    if tls_cert:
        for source,name in ((tls_cert,'tls.crt'),(tls_key,'tls.key')):
            destination=config/name
            if Path(source).resolve() != destination.resolve():shutil.copyfile(source,destination)
            destination.chmod(0o640)
        env['SERVICES']+=',wss://0.0.0.0:'+str(args.wss_port)
        env['SSL_CERTFILE']='/config/tls.crt' if args.image else str((config/'tls.crt').resolve())
        env['SSL_KEYFILE']='/config/tls.key' if args.image else str((config/'tls.key').resolve())
    else:
        env.pop('SSL_CERTFILE',None);env.pop('SSL_KEYFILE',None)
    fd=os.open(str(env_path),os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o640)
    with os.fdopen(fd,'w') as output:output.write(''.join(k+'='+v+'\n' for k,v in env.items()))
    shutil.copyfile(ROOT/'contrib/core_ready.py',runtime/'core_ready.py')
    (runtime/'core_ready.py').chmod(0o644)
    (units/SERVICE).write_text(unit_text(args,uid,gid))
    (units/TIMER).write_text(MARKER+'\n[Unit]\nDescription=Start the mainnet index after Core sync\n'
        '[Timer]\nOnBootSec=30\nOnUnitInactiveSec=60\nAccuracySec=5\nUnit='+SERVICE+
        '\n[Install]\nWantedBy=timers.target\n')
    result=summary(args)
    result['wss_port']=args.wss_port if tls_cert else None
    # Without txindex, standard blockchain.transaction.get serves only mempool
    # transactions; MetaYoshi uses blockchain.transaction.get_in_block instead.
    result['core_txindex']=enabled('txindex')
    saved={key:value for key,value in vars(args).items() if key not in ('reinstall','dry_run')}
    saved['profile']=limits['name']
    if tls_cert:
        saved.update(tls_cert=str(config/'tls.crt'),tls_key=str(config/'tls.key'))
    manifest_data=dict(result,options=saved)
    https_module.atomic_write(manifest,json.dumps(manifest_data,indent=2)+'\n',0o640)
    return result


def main(argv=None):
    p=parser()
    try:args=parse_options(argv)
    except ValueError as error:p.error(str(error))
    if args.dry_run:
        print(json.dumps(summary(args)));return 0
    if sys.platform != 'linux' or os.geteuid() != 0:
        p.error('Service setup requires Linux systemd and root; use --dry-run elsewhere.')
    if Path(args.systemd_dir).resolve() != Path('/etc/systemd/system'):
        p.error('Live installation must use the system service directory.')
    if args.wallet_https:
        try:https_module.preflight(wallet_https_options(args))
        except (OSError,ValueError,subprocess.SubprocessError) as error:p.error(str(error))
    # Prove installation before provisioning accounts or service files.
    smoke=('from electrumx.lib.coins import Raptoreum; '
        'from electrumx.server.raptoreum_db import SCHEMA_VERSION; assert SCHEMA_VERSION == b"2"; '
        'assert Raptoreum.DAEMON.__name__ == "RaptoreumDaemon"; '
        'assert Raptoreum.DBCLS.__name__ == "RaptoreumDB"; '
        'assert Raptoreum.SESSIONCLS.__name__ == "RaptoreumElectrumX"; '
        'from pathlib import Path; import electrumx.explorer as e; '
        'assert (Path(e.__file__).parent/"static/index.html").is_file()')
    command=(['docker','run','--rm','--network','none',args.image,'python','-I','-c',smoke]
             if args.image else [str(Path(args.venv)/'bin/python'),'-I','-c',smoke])
    subprocess.run(command,check=True)
    import pwd
    try:user=pwd.getpwnam('rtm-electrumx')
    except KeyError:
        subprocess.run(['useradd','--system','--user-group','--home-dir','/nonexistent',
                        '--shell','/usr/sbin/nologin','rtm-electrumx'],check=True)
        user=pwd.getpwnam('rtm-electrumx')
    data=Path(args.db_dir)
    if data.exists() and any(data.iterdir()) and data.stat().st_uid != user.pw_uid:
        p.error('Existing database belongs to another user; preserve it and choose a fresh database directory.')
    try:result=stage(args,user.pw_uid,user.pw_gid)
    except (OSError,ValueError,ssl.SSLError):
        print('Setup refused: check indexed/unpruned mainnet Core settings, private config, TLS pair and managed service paths.',file=sys.stderr)
        return 2
    config=Path(args.config_dir)
    os.chown(config,0,user.pw_gid)
    for name in ('core-rpc.json','server.env','tls.crt','tls.key','installation.json'):
        path=config/name
        if path.exists():os.chown(path,0,user.pw_gid);path.chmod(0o640)
    os.chown(data,user.pw_uid,user.pw_gid);data.chmod(0o750)
    subprocess.run(['systemctl','daemon-reload'],check=True)
    subprocess.run(['systemctl','enable',SERVICE],check=True)
    subprocess.run(['systemctl','enable','--now',TIMER],check=True)
    subprocess.run(['systemctl','restart',SERVICE],check=True)
    if args.with_explorer:
        options=['--image',args.image] if args.image else ['--venv',args.venv]
        options+=['--core-config',args.core_config,'--electrum-port',str(args.tcp_port),
                  '--port',str(args.port),'--bind',args.bind]
        if args.public_host:options+=['--public-host',args.public_host]
        if args.wallet_api is not None:options+=['--wallet-api' if args.wallet_api else '--no-wallet-api']
        subprocess.run([sys.executable,str(ROOT/'contrib/install_explorer.py'),*options],check=True)
    if args.wallet_https:
        subprocess.run([sys.executable,str(ROOT/'contrib/install_wallet_https.py'),*wallet_https_argv(args)],check=True)
    if result.get('wss_port'):
        ufw=shutil.which('ufw')
        if ufw:
            status=subprocess.run([ufw,'status'],capture_output=True,text=True,env=dict(os.environ,LC_ALL='C'))
            if status.returncode == 0 and 'Status: active' in status.stdout:
                subprocess.run([ufw,'allow',str(args.wss_port)+'/tcp','comment','RTM wallet WSS'],check=True)
    print(json.dumps(result))
    print('Configured one mainnet coin/asset indexer. The start timer waits until Core is synchronized.')
    print('Core settings and blockchain data were not modified. Inspect: systemctl status '+SERVICE)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
