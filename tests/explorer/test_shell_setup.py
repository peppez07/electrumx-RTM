'''Validate shell setup without package installation or host-service changes.'''
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark=pytest.mark.skipif(sys.platform == 'win32',reason='POSIX installer exercised on Linux')
SCRIPT=Path(__file__).resolve().parents[2]/'contrib/install_electrumx.sh'


def test_setup_dry_run_does_not_install_or_write_a_virtualenv(tmp_path):
    environment=dict(os.environ,PYTHON=sys.executable,VENV_DIR=str(tmp_path/'venv'))
    result=subprocess.run(['sh',str(SCRIPT),'--setup','--dry-run','--with-explorer',
        '--profile','low','--public-host','203.0.113.5'],env=environment,capture_output=True,text=True)
    assert result.returncode == 0,result.stderr
    assert json.loads(result.stdout)['explorer_url'] == 'http://203.0.113.5:8080/'
    assert not (tmp_path/'venv').exists()


def test_bad_setup_arguments_fail_before_package_installation(tmp_path):
    environment=dict(os.environ,PYTHON=sys.executable,VENV_DIR=str(tmp_path/'venv'))
    result=subprocess.run(['sh',str(SCRIPT),'--setup','--profile','invalid'],
        env=environment,capture_output=True,text=True)
    assert result.returncode != 0
    assert not (tmp_path/'venv').exists()


def test_old_explorer_shortcut_runs_complete_stack_dry_run(tmp_path):
    environment=dict(os.environ,PYTHON=sys.executable,VENV_DIR=str(tmp_path/'venv'))
    result=subprocess.run(['sh',str(SCRIPT),'--explorer','--dry-run'],
        env=environment,capture_output=True,text=True)
    assert result.returncode == 0,result.stderr
    assert json.loads(result.stdout)['waits_for_core_sync'] is True
    assert not (tmp_path/'venv').exists()
