import os
from pathlib import Path
import pty
import select
import shlex
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
# Command under test. Default: the repo-root shim. Override with e.g.
# SEALD_CLI=seald (an installed entrypoint) or SEALD_CLI="python3 -m seald".
CLI = shlex.split(os.environ.get('SEALD_CLI') or str(ROOT / 'seald'))


class SealdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = dict(os.environ, SEALD_HOME=self.tmp.name + '/store')
        self.run_cli('init', success=True)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args, success=True):
        p = subprocess.run([*CLI, *args], env=self.env, capture_output=True, text=True)
        self.assertEqual(p.returncode == 0, success, p.stderr)
        return p

    def test_hidden_paste_round_trip_and_no_overwrite(self):
        pid, master = pty.fork()
        if pid == 0:
            argv = [*CLI, 'seal', 'example']
            os.execvpe(argv[0], argv, self.env)
        try:
            transcript = b''
            deadline = time.monotonic() + 10
            while b'Input is hidden.' not in transcript and time.monotonic() < deadline:
                readable, _, _ = select.select([master], [], [], 1)
                if readable:
                    transcript += os.read(master, 4096)
            self.assertIn(b'Input is hidden.', transcript)
            # Wait for echo to be disabled after the prompt has been printed.
            time.sleep(.2)
            os.write(master, b'example-secret\nsecond-line\n\x04')
            _, status = os.waitpid(pid, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            while select.select([master], [], [], 0)[0]:
                try: transcript += os.read(master, 4096)
                except OSError: break
            self.assertNotIn(b'example-secret', transcript)
        finally:
            os.close(master)
        path = Path(self.env['SEALD_HOME']) / 'example.age'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(b'example-secret', path.read_bytes())
        self.assertEqual(self.run_cli('open', 'example').stdout, 'example-secret\nsecond-line\n')
        self.assertEqual(self.run_cli('list').stdout, 'example\n')
        self.assertIn('refusing to overwrite', self.run_cli('seal', 'example', success=False).stderr)

    def test_bad_name_and_terminal_required(self):
        self.assertIn('name must', self.run_cli('seal', '../escape', success=False).stderr)
        self.assertIn('terminal', self.run_cli('seal', 'valid', success=False).stderr)
        self.assertFalse((Path(self.env['SEALD_HOME']) / 'valid.age').exists())

    def test_init_never_replaces_key(self):
        path = Path(self.env['SEALD_HOME']) / 'identity.txt'
        before = path.read_bytes()
        self.run_cli('init', success=False)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_list_empty_store(self):
        p = self.run_cli('list')
        self.assertEqual((p.stdout, p.stderr), ('', ''))

    def test_open_missing_name(self):
        p = self.run_cli('open', 'nope', success=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn('nope not found', p.stderr)
        self.assertEqual(p.stdout, '')


class ModuleTests(unittest.TestCase):
    def test_python_m_seald_help(self):
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(
            [str(ROOT / 'src')] + ([os.environ['PYTHONPATH']] if os.environ.get('PYTHONPATH') else [])))
        p = subprocess.run([sys.executable, '-m', 'seald', '--help'], env=env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('usage: seald', p.stdout)
        for action in ('init', 'seal', 'open', 'list'):
            self.assertIn(action, p.stdout)


if __name__ == '__main__':
    unittest.main()
