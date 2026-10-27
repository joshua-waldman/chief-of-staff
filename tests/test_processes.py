import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from cos import processes, integrations
from cos.local_checks import run_host, ActionNotExecuted


def test_background_options_are_portable(monkeypatch):
    monkeypatch.setattr(processes, 'os', SimpleNamespace(name='posix'))
    assert processes.background_options()=={}
    monkeypatch.setattr(processes, 'os', SimpleNamespace(name='nt'))
    assert processes.background_options()=={'creationflags':0x08000000}


def test_background_capture_preserves_output_and_exit_status(tmp_path):
    code="import sys; print('result'); print('diagnostic',file=sys.stderr); sys.exit(7)"
    result=run_host([sys.executable,'-c',code],cwd=tmp_path,env=dict(os.environ))
    assert result.returncode==7
    assert result.stdout.strip()=='result'
    assert result.stderr.strip()=='diagnostic'


@pytest.mark.skipif(os.name!='nt',reason='Windows console allocation regression')
def test_background_python_has_no_console(tmp_path):
    code='import ctypes,json; print(json.dumps({"console":ctypes.windll.kernel32.GetConsoleWindow()}))'
    result=run_host([sys.executable,'-c',code],cwd=tmp_path,env=dict(os.environ))
    assert result.returncode==0
    assert json.loads(result.stdout)['console']==0


def test_skill_helpers_get_background_launch_options(tmp_path,monkeypatch):
    monkeypatch.setattr(integrations,'skill_dir',lambda name:tmp_path)
    monkeypatch.setattr(integrations,'background_options',lambda:{'creationflags':123})
    calls=[]
    def run(command,**kwargs):
        calls.append((command,kwargs))
        return subprocess.CompletedProcess(command,0,'{"ok":true}','')
    monkeypatch.setattr(integrations.subprocess,'run',run)
    assert integrations.run_skill('example','helper.py',[])=={'ok':True}
    assert integrations.communication('calendar_events',{})=={'ok':True}
    assert all(kwargs['creationflags']==123 for _,kwargs in calls)
    assert all(kwargs['capture_output'] for _,kwargs in calls)


def test_hidden_launch_failure_remains_not_executed(tmp_path):
    with pytest.raises(ActionNotExecuted):
        run_host([str(tmp_path/'missing-program.exe')],cwd=tmp_path,env=dict(os.environ))
