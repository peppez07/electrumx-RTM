'''Optional real-host installer check: sudo python3 tests/explorer/check_systemd_install.py.

Requires systemd, uses a temporary unit and non-secret configuration, and cleans
up on both success and failure. Not run inside package-test containers.
'''
import grp
import importlib.util
import os
from pathlib import Path
import pwd
import subprocess
import tempfile
from types import SimpleNamespace


def main():
    if os.geteuid() != 0 or not Path('/run/systemd/system').exists():
        raise SystemExit('Requires a Linux host running systemd and root.')
    spec = importlib.util.spec_from_file_location('installer',
        Path(__file__).resolve().parents[2]/'contrib/install_explorer.py')
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    nobody = pwd.getpwnam('nobody')
    with tempfile.TemporaryDirectory(prefix='rtm-explorer-check-', dir='/run') as temporary:
        directory = Path(temporary)
        directory.chmod(0o755)
        config = directory/'config with spaces'
        config.mkdir(mode=0o755)
        (config/'explorer.env').write_text('RTM_INSTALL_TEST=loaded\n')
        name = directory.name+'.service'
        unit = Path('/run/systemd/system')/name
        text = installer.service_text(SimpleNamespace(config_dir=str(config),
            venv='/unused', image=None), nobody.pw_uid, nobody.pw_gid)
        lines = []
        for line in text.splitlines():
            if line == '[Service]':
                lines.extend([line, 'Type=oneshot'])
                continue
            if line.startswith('ExecStart='):
                line = 'ExecStart=/usr/bin/python3 -c "import os; assert os.environ.get(\'RTM_INSTALL_TEST\') == \'loaded\'"'
            if line.startswith('User='):
                line = 'User=nobody'
            if line.startswith('Group='):
                line = 'Group='+grp.getgrgid(nobody.pw_gid).gr_name
            if line.startswith('Restart='):
                line = 'Restart=no'
            lines.append(line)
        unit.write_text('\n'.join(lines)+'\n')
        try:
            subprocess.run(['systemctl','daemon-reload'],check=True)
            result = subprocess.run(['systemctl','start',name],capture_output=True,text=True)
            if result.returncode:
                print(subprocess.check_output(['journalctl','-u',name,'-n','12','--no-pager'],text=True))
                raise AssertionError('The installed native service did not load its environment file.')
            print('SYSTEMD_CONFIG_LOADING_PASSED (unprivileged user, path with spaces)')
        finally:
            subprocess.run(['systemctl','stop',name],capture_output=True)
            subprocess.run(['systemctl','reset-failed',name],capture_output=True)
            unit.unlink()
            subprocess.run(['systemctl','daemon-reload'],check=True)


if __name__ == '__main__':
    main()
