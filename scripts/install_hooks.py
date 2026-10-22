"""Enable privacy checks for this checkout."""
import subprocess

subprocess.run(['git', 'config', '--local', 'core.hooksPath', '.githooks'], check=True)
print('Privacy hooks enabled for commits and pushes.')
