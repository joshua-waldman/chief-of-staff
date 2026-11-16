"""Preview or run a direct SSH tunnel from the laptop to the COS host.

Standard library only; does not import COS or create a local data directory.
An approved private route, SSH server/account and independently verified host key
must already exist. No SSH configuration files are loaded by this helper.
"""
import argparse
import json
import re
import shutil
import socket
import subprocess


def port(value):
    number = int(value)
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError('Port must be between 1 and 65535.')
    return number


def command(host, user, local_port=8765, remote_port=8765, ssh_port=22):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', host):
        raise ValueError('Use a direct desktop DNS name or IPv4 address.')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@\\-]*', user):
        raise ValueError('Use an SSH account name, without whitespace or options.')
    for value in (local_port, remote_port, ssh_port):
        port(value)
    return ['ssh', '-F', 'none', '-N', '-T', '-a', '-x', '-S', 'none',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'ExitOnForwardFailure=yes',
            '-o', 'GatewayPorts=no', '-o', 'PermitLocalCommand=no',
            '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3',
            '-o', 'ConnectTimeout=10', '-o', 'ControlMaster=no',
            '-L', f'127.0.0.1:{local_port}:127.0.0.1:{remote_port}',
            '-p', str(ssh_port), '-l', user, host]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True)
    parser.add_argument('--user', required=True)
    parser.add_argument('--local-port', type=port, default=8765)
    parser.add_argument('--remote-port', type=port, default=8765)
    parser.add_argument('--ssh-port', type=port, default=22)
    parser.add_argument('--connect', action='store_true', help='Run SSH; default only prints a plan.')
    args = parser.parse_args(argv)
    try:
        cmd = command(args.host, args.user, args.local_port, args.remote_port, args.ssh_port)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps({'argv': cmd, 'url': f'http://127.0.0.1:{args.local_port}',
                      'mode': 'connect' if args.connect else 'preview'}, indent=2), flush=True)
    if not args.connect:
        return 0
    executable = shutil.which('ssh')
    if not executable:
        parser.exit(1, 'SSH client not found. Use your approved software provisioning process.\n')
    # Best-effort conflict check. SSH handles any subsequent race and fails closed.
    try:
        with socket.socket() as listener:
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind(('127.0.0.1', args.local_port))
    except OSError:
        parser.exit(1, 'Local port unavailable. Stop the conflicting listener or choose --local-port.\n')
    print('Requires an approved route/account and a verified host key already in known_hosts.\n'
          'After authentication, open the URL to verify COS. Keep this terminal open; Ctrl+C closes the tunnel.\n'
          'SSH startup does not prove the remote COS listener or UI works.', flush=True)
    try:
        return subprocess.call([executable, *cmd[1:]])
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
