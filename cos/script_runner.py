"""Guarded execution of server-captured artifact code; not an OS sandbox."""
import hashlib
import importlib.abc
import importlib.util
import json
import os
from pathlib import Path
import sys
import sysconfig
import tempfile


def run(bundle_path, expected_digest, receipt_path):
    raw = Path(bundle_path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_digest:
        raise ValueError('Reviewed bundle changed before execution.')
    snapshot = json.loads(raw)
    root = Path(snapshot['root']).resolve()
    cwd = Path(snapshot['cwd']).resolve()
    if not cwd.is_relative_to(root):
        raise ValueError('Working directory escaped the workspace.')
    modules = snapshot['modules']
    for item in modules.values():
        if not Path(item['path']).resolve().is_relative_to(root):
            raise ValueError('Reviewed source escaped the workspace.')
    sys.dont_write_bytecode = True
    # Only server-installed libraries are available. In particular, workspace
    # sitecustomize, .pth files, plugins and dependency directories are not loaded.
    sys.path = [p for p in sys.path if p and not Path(p).resolve().is_relative_to(root)]
    for name in snapshot['packages']:
        __import__(name)
    if any(name != '__main__' and name in sys.modules for name in modules):
        raise ValueError('A local dependency shadows a loaded runtime/library module.')
    spec = importlib.util.spec_from_file_location('cos_script_limits', Path(__file__).with_name('pure_runner.py'))
    limits = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(limits)
    memory_handle = limits.limit_memory()
    temp_root = Path(tempfile.mkdtemp(prefix='tmp-', dir=Path(bundle_path).parent)).resolve()
    tempfile.tempdir = str(temp_root)
    os.environ['TEMP'] = os.environ['TMP'] = str(temp_root)
    receipt = open(receipt_path, 'w', encoding='utf-8')
    trusted = {Path(sysconfig.get_path(name)).resolve() for name in ('stdlib', 'purelib', 'platlib')}
    trusted.add((Path(sys.base_prefix) / 'DLLs').resolve())
    blocked_parts = {'.git', '.ssh', '.aws', '.codex', '.claude', 'dependencies', 'context', '__pycache__'}
    blocked_names = {'config.local.json', 'token.json', 'credentials.json', '.credentials.json'}
    writes = set()

    def checked(value, write=False):
        if isinstance(value, int):
            raise PermissionError('Raw file descriptors require manual review.')
        path = Path(os.fsdecode(value)).resolve()
        if path.is_relative_to(temp_root):
            return path
        if path.is_relative_to(root):
            parts = path.relative_to(root).parts
            if ({part.lower() for part in parts} & blocked_parts or path.name.lower() in blocked_names
                    or any(part.lower().startswith('.env') for part in parts)):
                raise PermissionError('Script access to credentials or reserved files is blocked.')
            if write and path.suffix.lower() in {'.py', '.pyc', '.pyd', '.exe', '.dll', '.ps1', '.bat', '.cmd', '.pth'}:
                raise PermissionError('Artifact execution cannot write executable files.')
            return path
        if not write and any(path.is_relative_to(parent) for parent in trusted):
            return path
        raise PermissionError('Artifact script file access must remain in its job workspace.')

    def audit(event, args):
        if event.startswith(('socket.', 'subprocess.', 'ctypes.', 'winreg.', 'os.exec', 'os.spawn')) or event in {
                'os.system', 'os.startfile', 'os.link', 'os.symlink', 'os.chmod',
                'os.chown', 'os.putenv', 'os.unsetenv'}:
            raise PermissionError('Artifact scripts cannot use network, processes, credentials or permission changes.')
        if event == 'open':
            value, mode, flags = args
            write = bool((mode and any(c in mode for c in 'wax+')) or (isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)))
            path = checked(value, write)
            if write:
                writes.add(path)
        elif event in {'os.listdir', 'os.scandir'}:
            checked(args[0] if args[0] is not None else cwd)
        elif event in {'os.remove', 'os.rmdir', 'os.mkdir', 'os.truncate', 'os.chdir'}:
            checked(args[0], True)
            if len(args) > 1 and event in {'os.remove', 'os.rmdir', 'os.mkdir'} and args[-1] not in {None, -1}:
                raise PermissionError('Directory descriptor mutations are blocked.')
        elif event in {'os.rename', 'os.replace'}:
            checked(args[0], True)
            checked(args[1], True)
            if any(value not in {None, -1} for value in args[2:]):
                raise PermissionError('Directory descriptor mutations are blocked.')

    class CapturedLoader(importlib.abc.MetaPathFinder, importlib.abc.Loader):
        def find_spec(self, fullname, path=None, target=None):
            if fullname in modules:
                return importlib.util.spec_from_loader(fullname, self, is_package=modules[fullname]['package'])
            return None

        def create_module(self, spec):
            return None

        def exec_module(self, module):
            item = modules[module.__name__]
            module.__file__ = item['path']
            if item['package']:
                module.__path__ = []
            exec(compile(item['source'], item['path'], 'exec'), module.__dict__)

    sys.meta_path.insert(0, CapturedLoader())
    sys.argv = list(snapshot['argv'])
    os.chdir(cwd)
    sys.addaudithook(audit)
    try:
        entry = modules['__main__']
        exec(compile(entry['source'], entry['path'], 'exec'), {'__name__': '__main__', '__file__': entry['path'], '__package__': None})
    finally:
        # The receipt descriptor was opened by the trusted runner before the
        # guard. Script globals never receive it or the private bundle location.
        outputs = [str(path) for path in sorted(writes) if path.is_file() and not path.is_relative_to(temp_root)]
        json.dump(outputs, receipt)
        receipt.close()


if __name__ == '__main__':
    run(*sys.argv[1:])
