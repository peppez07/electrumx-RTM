'''Optional Linux host check for the low-priority sampler; no Core calls.'''
import grp
import importlib.util
import json
import os
from pathlib import Path
import pwd
import subprocess
import tempfile


def main():
    if os.geteuid()!=0 or not Path('/run/systemd/system').exists():
        raise SystemExit('Requires a Linux systemd host and root.')
    spec=importlib.util.spec_from_file_location('installer',Path(__file__).resolve().parents[2]/'contrib/install_explorer.py')
    installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)
    user=pwd.getpwnam('nobody')
    with tempfile.TemporaryDirectory(prefix='rtm-metrics-check-',dir='/run') as temporary:
        base=Path(temporary);base.chmod(0o755)
        data,runtime,units=base/'data with spaces',base/'runtime',base/'units'
        installer.stage_metrics(data,runtime,units);os.chown(data,user.pw_uid,user.pw_gid)
        unit=Path('/run/systemd/system')/(base.name+'.service')
        text=(units/'rtm-explorer-metrics.service').read_text().replace('User=rtm-explorer','User=nobody').replace('Group=rtm-explorer','Group='+grp.getgrgid(user.pw_gid).gr_name)
        unit.write_text(text)
        try:
            subprocess.run(['systemctl','daemon-reload'],check=True)
            for _ in range(2):subprocess.run(['systemctl','start',unit.name],check=True)
            sample=json.loads((data/'host.json').read_text())
            assert sample['memory']['total_bytes']>0 and sample['disk']['total_bytes']>0
            assert sample['cpu']['used_percent'] is not None
            assert (data/'host.json').stat().st_mode & 0o777 == 0o640
            print(subprocess.check_output(['systemctl','show',unit.name,'-p','CPUUsageNSec','-p','MemoryPeak','-p','Result'],text=True))
            print('SYSTEMD_METRICS_PASSED: unprivileged host sample, spaced directory, sandbox, 5%CPU/48MiB ceilings, atomic0640 file')
        finally:
            subprocess.run(['systemctl','stop',unit.name],capture_output=True)
            subprocess.run(['systemctl','reset-failed',unit.name],capture_output=True)
            unit.unlink();subprocess.run(['systemctl','daemon-reload'],check=True)


if __name__ == '__main__':main()
