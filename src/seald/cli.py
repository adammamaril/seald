"""A tiny age-encrypted secret store."""
import argparse
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import termios

STORE = Path(os.environ.get('SEALD_HOME', Path.home() / '.local/share/seald')).expanduser()
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z')


def fail(message):
    print(f'seald: {message}', file=sys.stderr)
    raise SystemExit(1)


def executable(name):
    result = shutil.which(name)
    if not result:
        fail(f'{name} is required; install age first')
    return result


def check_store():
    if not STORE.is_dir() or not (STORE / 'identity.txt').is_file():
        fail('store not initialized; run seald init')


def secret_path(name):
    if not NAME.fullmatch(name):
        fail('name must begin with a letter or number and contain only letters, numbers, dot, underscore, or dash')
    return STORE / (name + '.age')


def init():
    executable('age-keygen')
    STORE.mkdir(parents=True, mode=0o700, exist_ok=True)
    key = STORE / 'identity.txt'
    try:
        fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        fail('identity already exists; refusing to replace it')
    try:
        with os.fdopen(fd, 'wb') as out:
            subprocess.run(['age-keygen'], stdout=out, stderr=subprocess.DEVNULL, check=True)
    except BaseException:
        key.unlink(missing_ok=True)
        raise
    print(f'Initialized {STORE}. Back up identity.txt somewhere safe.')


def seal(name):
    executable('age')
    executable('age-keygen')
    check_store()
    dst = secret_path(name)
    if dst.exists() or dst.is_symlink():
        fail(f'{name} already exists; refusing to overwrite it')
    try:
        tty = open('/dev/tty', 'r+b', buffering=0)
        old = termios.tcgetattr(tty.fileno())
    except (OSError, termios.error):
        fail('a working terminal is required for hidden input')
    recipient = subprocess.run(['age-keygen', '-y', str(STORE / 'identity.txt')], capture_output=True, text=True, check=True).stdout.strip()
    fd, tmpname = tempfile.mkstemp(prefix='.seald-', dir=STORE)
    try:
        os.fchmod(fd, 0o600)
        attrs = termios.tcgetattr(tty.fileno())
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(tty.fileno(), termios.TCSANOW, attrs)
        try:
            print('Paste your secret, press Enter, then Ctrl+D on an empty line. Input is hidden.', file=sys.stderr, flush=True)
            with os.fdopen(fd, 'wb') as out:
                subprocess.run(['age', '-r', recipient], stdin=tty, stdout=out, check=True)
        finally:
            termios.tcsetattr(tty.fileno(), termios.TCSANOW, old)
            tty.close()
            print(file=sys.stderr)
        try:
            os.link(tmpname, dst)
        except FileExistsError:
            fail(f'{name} already exists; refusing to overwrite it')
    finally:
        Path(tmpname).unlink(missing_ok=True)
    print(f'Sealed {name}.')


def show(name):
    executable('age')
    check_store()
    path = secret_path(name)
    if not path.is_file():
        fail(f'{name} not found')
    subprocess.run(['age', '-d', '-i', str(STORE / 'identity.txt'), str(path)], check=True)


def main():
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    parser = argparse.ArgumentParser(prog='seald', description='A tiny age-encrypted secret store.')
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('init', help='create a new local age identity')
    for action, help_text in [('seal', 'paste and encrypt a secret'), ('open', 'decrypt a secret to standard output')]:
        sub.add_parser(action, help=help_text).add_argument('name')
    sub.add_parser('list', help='list sealed secret names')
    args = parser.parse_args()
    try:
        if args.action == 'init': init()
        elif args.action == 'seal': seal(args.name)
        elif args.action == 'open': show(args.name)
        else:
            check_store()
            for path in sorted(STORE.glob('*.age')):
                print(path.name[:-4])
    except subprocess.CalledProcessError:
        fail('age command failed')
    except (OSError, termios.error) as exc:
        fail(str(exc))
    except KeyboardInterrupt:
        print('\nseald: interrupted', file=sys.stderr)
        raise SystemExit(130)

