'''Installation contracts; clean package/container smoke checks run on Linux.'''

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_docker_installs_this_checkout_for_rtm_mainnet():
    dockerfile = (ROOT / 'contrib' / 'Dockerfile').read_text()
    assert 'https://github.com/spesmilo/' not in dockerfile
    assert 'COPY . /src' in dockerfile
    assert 'pip install' in dockerfile and '/src' in dockerfile
    assert 'COIN=Raptoreum' in dockerfile
    assert 'NET=mainnet' in dockerfile
    assert 'USER electrumx' in dockerfile
    assert 'ALLOW_ROOT=true' not in dockerfile


def test_pi_installer_uses_local_installer_without_os_upgrade():
    installer = (ROOT / 'contrib' / 'raspberrypi3' / 'install_electrumx.sh').read_text()
    assert 'github.com/spesmilo/' not in installer
    assert 'dist-upgrade' not in installer
    assert 'contrib/install_electrumx.sh' in installer
    assert b'\r\n' not in (ROOT / 'contrib' / 'raspberrypi3' / 'install_electrumx.sh').read_bytes()


def test_local_installer_exists_and_keeps_python_dependencies_isolated():
    installer = ROOT / 'contrib' / 'install_electrumx.sh'
    assert installer.is_file()
    script = installer.read_text()
    assert '-m venv' in script
    assert 'VENV_DIR' in script
    assert 'sudo pip' not in script
