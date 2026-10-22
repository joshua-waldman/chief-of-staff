"""Narrow runner for pure Python functions and unittest suites; not a general OS sandbox."""
import ast
import os
from pathlib import Path
import sys

PURE={'re','unicodedata','math','unittest'}
DENIED={'eval','exec','compile','open','input','breakpoint','__import__','globals','locals','vars','getattr','setattr','delattr','hasattr','help','dir','memoryview','type','object','super'}
ALLOWED_UNITTEST={'TestCase','main','skip','skipIf','skipUnless','expectedFailure'}
def validate(root):
    for path in root.rglob('*.py'):
        if any(x in path.parts for x in {'.venv','dependencies','__pycache__'}):
            continue
        if path.stat().st_size>200000:
            raise ValueError('Source too large for restricted execution')
        tree=ast.parse(path.read_text(encoding='utf-8-sig'),filename=str(path))
        parents={child:parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        unit_aliases={'unittest'}
        local_names={p.stem for p in root.glob('*.py')}
        for node in ast.walk(tree):
            if isinstance(node,ast.Import):
                for alias in node.names:
                    if alias.name not in PURE|local_names:
                        raise ValueError('Import requires sandbox or review: '+alias.name)
                    if alias.name=='unittest':
                        unit_aliases.add(alias.asname or 'unittest')
            if isinstance(node,ast.ImportFrom):
                if node.level or node.module not in PURE|local_names:
                    raise ValueError('Import requires sandbox or review: '+str(node.module))
                if any(a.name=='*' or a.name.startswith('_') for a in node.names):
                    raise ValueError('Wildcard/private imports are not allowed')
                if node.module=='unittest' and any(a.name!='TestCase' for a in node.names):
                    raise ValueError('Unsupported unittest API')
            if isinstance(node,ast.Name) and (node.id in DENIED or (node.id.startswith('__') and node.id!='__name__')):
                raise ValueError('Dynamic introspection or host access requires review')
            if isinstance(node,ast.Name) and node.id in unit_aliases:
                parent=parents.get(node)
                if not isinstance(parent,ast.Attribute) or parent.value is not node:
                    raise ValueError('Aliasing the unittest module requires review')
            if isinstance(node,ast.Attribute):
                if node.attr.startswith('_') or node.attr in {'format','format_map'}:
                    raise ValueError('Private attributes and dynamic formatting require review')
                if isinstance(node.value,ast.Name) and node.value.id in unit_aliases and node.attr not in ALLOWED_UNITTEST:
                    raise ValueError('Unsupported unittest API')
                if node.attr=='main':
                    parent=parents.get(node)
                    if not isinstance(parent,ast.Call) or parent.func is not node:
                        raise ValueError('Aliasing unittest.main is not supported')
            if isinstance(node,ast.Call):
                if not isinstance(node.func,(ast.Name,ast.Attribute)):
                    raise ValueError('Indirect callable dispatch requires sandbox or review')
                if isinstance(node.func,ast.Attribute) and node.func.attr=='main':
                    if node.args or any(k.arg!='verbosity' for k in node.keywords):
                        raise ValueError('unittest.main only supports verbosity here')
            if isinstance(node,(ast.Global,ast.Nonlocal)):
                raise ValueError('Global mutation requires sandbox or review')

def limit_memory():
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes as w
        class IO(ctypes.Structure):
            _fields_=[(n,ctypes.c_ulonglong) for n in ['readOps','writeOps','otherOps','readBytes','writeBytes','otherBytes']]
        class BASIC(ctypes.Structure):
            _fields_=[('processTime',ctypes.c_longlong),('jobTime',ctypes.c_longlong),('flags',w.DWORD),('minimum',ctypes.c_size_t),('maximum',ctypes.c_size_t),('active',w.DWORD),('affinity',ctypes.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
        class EXTENDED(ctypes.Structure):
            _fields_=[('basic',BASIC),('io',IO),('processMemory',ctypes.c_size_t),('jobMemory',ctypes.c_size_t),('peakProcess',ctypes.c_size_t),('peakJob',ctypes.c_size_t)]
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateJobObjectW.argtypes=[ctypes.c_void_p,w.LPCWSTR]
        kernel.CreateJobObjectW.restype=w.HANDLE
        kernel.SetInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD]
        kernel.SetInformationJobObject.restype=w.BOOL
        kernel.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE]
        kernel.AssignProcessToJobObject.restype=w.BOOL
        kernel.GetCurrentProcess.restype=w.HANDLE
        handle=kernel.CreateJobObjectW(None,None)
        limits=EXTENDED()
        limits.basic.flags=0x100|0x2000
        limits.processMemory=512*1024*1024
        if not handle or not kernel.SetInformationJobObject(handle,9,ctypes.byref(limits),ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(handle,kernel.GetCurrentProcess()):
            raise RuntimeError('Cannot establish Windows execution memory limit')
        return handle
    else:
        import resource
        resource.setrlimit(resource.RLIMIT_AS,(512*1024*1024,512*1024*1024))
        return None

def run(root,script):
    memory_handle=limit_memory()
    root=Path(root).resolve()
    script=(root/script).resolve()
    if root not in script.parents:
        raise ValueError('Script outside workspace')
    validate(root)
    import re,unicodedata,math,unittest,runpy
    sys.path.insert(0,str(root))
    sys.dont_write_bytecode=True
    trusted=Path(sys.base_prefix).resolve()
    def audit(event,args):
        if event.startswith(('socket.','subprocess.','ctypes.','winreg.','os.exec','os.spawn')) or event in {'os.system','os.remove','os.rename','os.rmdir','os.mkdir','os.putenv','os.unsetenv','os.startfile','os.link','os.symlink','os.chmod','os.truncate'}:
            raise PermissionError('Restricted validation blocks network, host mutations and subprocesses')
        if event=='open':
            path,mode,flags=args
            if isinstance(path,int):
                raise PermissionError('Descriptor access blocked')
            p=Path(path).resolve()
            if not (p==root or root in p.parents or trusted in p.parents):
                raise PermissionError('Reading outside workspace and Python runtime is blocked')
            if (mode and any(c in mode for c in 'wax+')) or (isinstance(flags,int) and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND)):
                raise PermissionError('Restricted validation is read-only')
    sys.addaudithook(audit)
    sys.argv=[str(script)]
    os.chdir(root)
    runpy.run_path(str(script),run_name='__main__')

if __name__=='__main__':
    run(sys.argv[1],sys.argv[2])
