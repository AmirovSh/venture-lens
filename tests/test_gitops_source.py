"""Exercise the lab mirror with real full and shallow Git repositories."""
import importlib.util
from pathlib import Path
import subprocess
import pytest

spec=importlib.util.spec_from_file_location('gitops_lab',Path(__file__).parents[1]/'platform/lab.py')
lab=importlib.util.module_from_spec(spec);spec.loader.exec_module(lab)

def git(path,*args):
    return subprocess.check_output(['git','-c','commit.gpgsign=false',*args],cwd=path,stderr=subprocess.PIPE,text=True).strip()

def repository(tmp_path):
    path=tmp_path/'source';path.mkdir()
    git(path,'init','-b','master');git(path,'config','user.name','CI fixture');git(path,'config','user.email','fixture@example.invalid')
    for i in range(2):
        (path/'value.txt').write_text(str(i));git(path,'add','value.txt');git(path,'commit','-m','Fixture '+str(i))
    return path

def test_lab_mirror_preserves_full_history_and_exact_checked_out_revision(tmp_path,monkeypatch):
    source=repository(tmp_path);state=tmp_path/'state'
    monkeypatch.setattr(lab,'ROOT',source);monkeypatch.setattr(lab,'STATE',state)
    lab.sync_mirror();mirror=state/'git/repo.git'
    assert git(mirror,'rev-parse','lab')==git(source,'rev-parse','HEAD')
    assert git(mirror,'rev-list','--count','lab')=='2'
    # Our disposable mirror also supports testing an explicit source rollback.
    git(source,'checkout','--detach','HEAD~1');lab.sync_mirror()
    assert git(mirror,'rev-parse','lab')==git(source,'rev-parse','HEAD')

def test_lab_rejects_shallow_history_before_starting_an_unreadable_git_source(tmp_path,monkeypatch):
    source=repository(tmp_path);shallow=tmp_path/'shallow'
    git(tmp_path,'clone','--depth','1',source.as_uri(),str(shallow))
    assert git(shallow,'rev-parse','--is-shallow-repository')=='true'
    state=tmp_path/'state';monkeypatch.setattr(lab,'ROOT',shallow);monkeypatch.setattr(lab,'STATE',state)
    with pytest.raises(RuntimeError,match='fetch-depth: 0'):lab.sync_mirror()
    assert not (state/'git/repo.git').exists()
