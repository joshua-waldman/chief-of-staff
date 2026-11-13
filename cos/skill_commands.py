"""Apply owner skill defaults before effects review and exact-action approval."""
from pathlib import Path
from pathlib import PureWindowsPath

from .reviewed_scripts import invocation


def prepare(job, args):
    call = invocation(args.get('command'))
    if call is None:
        return args
    # One canonical invocation for review; execution selects the server interpreter.
    values = args['command'][1:]
    if PureWindowsPath(args['command'][0]).name.lower() in {'py', 'py.exe'} and values[0] == '-3':
        values = values[1:]
    command = ['python', *values]
    if Path(call[0]).name == 'create_event.py' and not any(x in call[1:] for x in ('--help', '-h')):
        from .integrations import skill_dir
        from .tools import resolve_path
        try:
            calendar = skill_dir('m365-calendar') / 'scripts' / 'create_event.py'
        except ValueError:
            calendar = None
        if calendar is not None:
            cwd = resolve_path(job['id'], args.get('cwd', '.'))
            entry = Path(call[0])
            entry = (cwd / entry).resolve() if not entry.is_absolute() else entry.resolve()
            if entry == calendar.resolve() and '--teams' not in call[1:]:
                command.append('--teams')
    return {**args, 'command': command}
