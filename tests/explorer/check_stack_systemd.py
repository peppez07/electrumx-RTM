'''Optional real systemd check with a local recorded Core response, never live chain queries.'''
import grp
import importlib.util
import json
import os
from pathlib import Path
import pwd
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def main():
    if os.geteuid() != 0 or not Path('/run/systemd/system').exists():
        raise SystemExit('Requires root on a Linux systemd host.')
    root=Path(__file__).resolve().parents[2]
    spec=importlib.util.spec_from_file_location('stack',root/'contrib/install_stack.py')
    installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)
    state={'chain':'main','blocks':100,'headers':100,'initialblockdownload':True}
    methods=[]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            methods.append(request['method'])
            data=json.dumps({'id':request['id'],'result':state,'error':None}).encode()
            self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    user=pwd.getpwnam('nobody')
    try:
        with tempfile.TemporaryDirectory(prefix='rtm-stack-check-',dir='/run') as tmp:
            base=Path(tmp);base.chmod(0o755)
            venv=base/'venv with spaces';(venv/'bin').mkdir(parents=True)
            (venv/'bin/python').symlink_to('/usr/bin/python3')
            source=base/'source.conf'
            source.write_text('chain=main\ntxindex=1\nassetindex=1\nprune=0\nrpcuser=fixture\nrpcpassword=fixture\nrpcport='+str(server.server_port)+'\n')
            args=installer.parser().parse_args(['--venv',str(venv),'--core-config',str(source),
                '--config-dir',str(base/'config with spaces'),'--runtime-dir',str(base/'runtime'),
                '--db-dir',str(base/'database with spaces'),'--systemd-dir',str(base/'units')])
            installer.stage(args,user.pw_uid,user.pw_gid)
            config=Path(args.config_dir);os.chown(config,0,user.pw_gid)
            for file in config.iterdir():os.chown(file,0,user.pw_gid);file.chmod(0o640)
            data=Path(args.db_dir);os.chown(data,user.pw_uid,user.pw_gid);data.chmod(0o750)
            marker=data/'started'
            executable=venv/'bin/electrumx_server'
            executable.write_text('#!/usr/bin/python3\nimport pathlib,os\nassert os.environ["COIN"] == "Raptoreum"\npathlib.Path('+repr(str(marker))+').write_text("started")\n')
            executable.chmod(0o755)
            unit=Path('/run/systemd/system')/(base.name+'.service')
            text=installer.unit_text(args,user.pw_uid,user.pw_gid).replace('User=rtm-electrumx','User=nobody').replace('Group=rtm-electrumx','Group='+grp.getgrgid(user.pw_gid).gr_name).replace('Restart=on-failure','Restart=no').replace('[Service]','[Service]\nType=oneshot')
            unit.write_text(text)
            try:
                subprocess.run(['systemctl','daemon-reload'],check=True)
                subprocess.run(['systemctl','start',unit.name],check=True)
                assert not marker.exists(),'Backend started before sync'
                state['initialblockdownload']=False
                subprocess.run(['systemctl','start',unit.name],check=True)
                assert marker.read_text() == 'started'
                assert methods == ['getblockchaininfo','getblockchaininfo']
                print('SYSTEMD_BACKEND_GATE_PASSED: synced transition, unprivileged native service, spaced paths, private config/environment')
            finally:
                subprocess.run(['systemctl','stop',unit.name],capture_output=True)
                subprocess.run(['systemctl','reset-failed',unit.name],capture_output=True)
                unit.unlink();subprocess.run(['systemctl','daemon-reload'],check=True)
    finally:
        server.shutdown();server.server_close()


if __name__ == '__main__':main()
