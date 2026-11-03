"""Capture local artifact scripts for independent review and bound execution."""
import ast
import hashlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path, PureWindowsPath
import sys
import tempfile

from .local_checks import ActionNotExecuted

LIBRARIES = {'argparse', 'pathlib', 'math', 're', 'json', 'csv', 'datetime', 'decimal',
             'statistics', 'collections', 'itertools', 'functools', 'typing',
             'dataclasses', 'textwrap', 'unicodedata', 'string', 'enum',
             'openpyxl', 'docx', 'pptx', 'pypdf'}
HOST_ALIASES = {'os', 'sys', 'builtins', 'ctypes', 'subprocess', 'socket', 'importlib'}
DISTRIBUTIONS = {'docx': 'python-docx', 'pptx': 'python-pptx'}
DENIED = {'eval', 'exec', 'compile', '__import__', 'globals', 'locals', 'vars',
          'getattr', 'setattr', 'delattr', 'hasattr', 'dir', 'type', 'object',
          'super', 'memoryview', 'input', 'breakpoint', 'help'}


def invocation(command):
    if not isinstance(command, list) or not all(isinstance(x, str) for x in command):
        return None
    if not command or PureWindowsPath(command[0]).name.lower() not in {'python', 'python.exe', 'python3', 'python3.exe'}:
        return None
    values = command[1:]
    while values and values[0] in {'-B', '-u'}:
        values = values[1:]
    return values if values and not values[0].startswith('-') and values[0].endswith('.py') else None


def candidate(command):
    return invocation(command) is not None


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def command_identity(args):
    return {'command': args.get('command'), 'cwd': args.get('cwd', '.')}


def capture(job, args):
    from .tools import workspace, resolve_path
    if not candidate(args.get('command')):
        raise ValueError('Only a Python workspace script qualifies.')
    root = workspace(job['id']).resolve()
    cwd = resolve_path(job['id'], args.get('cwd', '.'))
    if not cwd.is_relative_to(root):
        raise ValueError('Artifact scripts must execute in their own job workspace.')
    call = invocation(args['command'])
    entry = resolve_path(job['id'], str(cwd / call[0]), True)
    base = entry.parent
    modules, packages = {}, {}

    def local(name):
        path = base.joinpath(*name.split('.'))
        if path.with_suffix('.py').is_file():
            return path.with_suffix('.py'), False
        if (path / '__init__.py').is_file():
            return path / '__init__.py', True
        return None

    def dependency(name):
        top = name.split('.')[0]
        if (any(part.startswith('__') for part in name.split('.'))
                or (top in sys.stdlib_module_names and top not in LIBRARIES)):
            raise ValueError('Host/runtime module access requires manual review: ' + name)
        if top in LIBRARIES:
            if local(top):
                raise ValueError('A local module shadows a trusted library: ' + top)
            if top not in packages:
                spec = importlib.util.find_spec(top)
                if spec is None:
                    raise ValueError('A required library is not installed: ' + top)
                origin = spec.origin
                packages[top] = {'version': importlib.metadata.version(DISTRIBUTIONS.get(top, top)) if top in {'openpyxl', 'docx', 'pptx', 'pypdf'} else 'stdlib',
                                 'origin': origin,
                                 'sha256': hashlib.sha256(Path(origin).read_bytes()).hexdigest() if origin and Path(origin).is_file() else ''}
            return
        installed = importlib.util.find_spec(top)
        if installed is not None:
            raise ValueError('A local dependency shadows an installed runtime/library module: ' + top)
        found = local(name)
        if not found:
            raise ValueError('Import requires manual review: ' + name)
        if '.' in name:
            dependency(name.rsplit('.', 1)[0])
        visit(name, *found)

    def visit(name, path, package=False):
        if name in modules:
            return
        path = resolve_path(job['id'], str(path), True)
        if 'context' in {part.lower() for part in path.relative_to(root).parts}:
            raise ValueError('Recovery archives cannot be executed as artifact scripts.')
        raw = path.read_bytes()
        if len(raw) > 40000 or len(modules) >= 12:
            raise ValueError('Script review source limit exceeded.')
        source = raw.decode('utf-8-sig')
        tree = ast.parse(source, filename=str(path))
        modules[name] = {'path': str(path), 'source': source,
                         'sha256': hashlib.sha256(raw).hexdigest(), 'package': package}
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and (node.id in DENIED or (node.id.startswith('__') and node.id not in {'__file__', '__name__'})):
                raise ValueError('Dynamic execution or introspection requires manual review.')
            if isinstance(node, ast.Attribute) and (node.attr.startswith('_') or node.attr in HOST_ALIASES or node.attr in {'format', 'format_map', 'modules', 'meta_path', 'path_hooks', 'environ', 'settrace', 'setprofile', 'f_globals', 'f_locals', 'gi_frame', 'cr_frame', 'tb_frame'}):
                raise ValueError('Private attributes or reflective formatting require manual review.')
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name.startswith('__') and node.name != '__init__':
                raise ValueError('Private hooks require manual review.')
            if isinstance(node, ast.Import):
                for alias in node.names:
                    dependency(alias.name)
            if isinstance(node, ast.ImportFrom):
                if any(alias.name.startswith('_') or alias.name == '*' or alias.name in HOST_ALIASES for alias in node.names):
                    raise ValueError('Private or wildcard imports require manual review.')
                imported = node.module or ''
                if node.level:
                    parent = name if package else name.rpartition('.')[0]
                    imported = importlib.util.resolve_name('.' * node.level + imported, parent)
                dependency(imported)
                if imported.split('.')[0] not in LIBRARIES:
                    for alias in node.names:
                        if local(imported + '.' + alias.name):
                            dependency(imported + '.' + alias.name)
    visit('__main__', entry)
    snapshot = {'root': str(root), 'cwd': str(cwd), 'argv': [str(entry), *call[1:]], 'modules': modules, 'packages': packages}
    if len(json.dumps(snapshot)) > 42000:
        raise ValueError('Complete script and dependency review exceeds the source limit.')
    return snapshot


def execute(store, job, args, run_host):
    from .tools import DATA, ROOT, file_record
    import os
    token = args.get('_workspace_script_review')
    key = 'reviewed-script:' + job['id'] + ':' + str(token)
    try:
        bound = store.state(key)
        if not bound or bound['command'] != command_identity(args):
            raise ValueError('The script review is missing or expired.')
        snapshot = capture(job, args)
        if digest(snapshot) != bound['digest']:
            raise ValueError('Script, dependencies, or library installation changed; request a fresh automatic review.')
    except Exception as error:
        raise ActionNotExecuted(str(error)) from error
    # This server-owned bundle is outside tool-writable workspace paths. The
    # child verifies its digest and executes only captured code, even if a source
    # changes after the launch check. Local imports use the same captured bundle.
    try:
        private = DATA / 'jobs' / job['id'] / 'script-executions'
        private.mkdir(parents=True, exist_ok=True)
        folder = Path(tempfile.mkdtemp(prefix='review-', dir=private))
        bundle = folder / 'bundle.json'
        bundle.write_bytes(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode())
        receipt = folder / 'outputs.json'
        store.set_state(key, None)
        env = {k: v for k, v in os.environ.items() if k.upper() in {'SYSTEMROOT', 'WINDIR', 'PATH', 'PATHEXT', 'LANG', 'LC_ALL'}}
        command = [sys.executable, '-I', '-B', str(ROOT / 'cos' / 'script_runner.py'),
                   str(bundle), bound['digest'], str(receipt)]
    except Exception as error:
        raise ActionNotExecuted('Cannot prepare reviewed script execution: ' + str(error)) from error
    result = run_host(command, cwd=Path(snapshot['cwd']), env=env)
    outputs = []
    if receipt.exists():
        from .tools import resolve_path
        for value in json.loads(receipt.read_text(encoding='utf-8')):
            path = resolve_path(job['id'], value, True)
            if path.is_file():
                outputs.append(file_record(store, job['id'], path))
    return {'exit_code': result.returncode, 'stdout': result.stdout[-24000:],
            'stderr': result.stderr[-12000:], 'execution': 'reviewed-workspace-script',
            'review_digest': bound['digest'], 'outputs': outputs}
