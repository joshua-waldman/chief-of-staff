"""Launch captured background helpers without allocating Windows consoles."""
import os
import subprocess


def background_options():
    # Capturing stdout/stderr alone does not suppress a console when COS has
    # no console of its own. Leave deliberately interactive launches separate.
    if os.name == 'nt':
        return {'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)}
    return {}
