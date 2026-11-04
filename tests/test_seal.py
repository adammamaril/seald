import os
from pathlib import Path
import pty
import select
import shutil
import subprocess
import tempfile
import time
import unittest

SEAL = os.environ.get('SEAL_CLI', str(Path(__file__).resolve().parents[1] / 'bin' / 'seal'))
HAVE = all(shutil.which(t) for t in ('sops', 'jq', 'age-keygen'))
VALUE = 'line one\nit\'s "quoted" $HOME \\n {braces}\nlast'


@unittest.skipUnless(HAVE, 'sops, jq and age-keygen required')
class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.home = self.tmp / 'home'
        self.home.mkdir()
        self.key = self.tmp / 'key.txt'
        subprocess.run(['age-keygen', '-o', str(self.key)], check=True, capture_output=True)
        self.pub = subprocess.run(['age-keygen', '-y', str(self.key)], check=True, capture_output=True, text=True).stdout.strip()
        self.ledger = self.tmp / 'work' / 'secrets.enc.yaml'
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(('SEAL_', 'SOPS_')) and k != 'BASH_XTRACEFD'}
        self.env.update(HOME=str(self.home), SOPS_AGE_KEY_FILE=str(self.key),
                        SEAL_LEDGER=str(self.ledger), SEAL_RECIPIENTS=self.pub)

    def seal(self, *args, input=None, env=None, check=None, cwd=None):
        p = subprocess.run([SEAL, *args], input=input, capture_output=True, text=True,
                           env=env or self.env, cwd=cwd)
        if check is not None:
            self.assertEqual(p.returncode == 0, check, p.stderr)
        return p

    def put(self, key, value=VALUE):
        return self.seal('set', key, input=value, check=True)


class LedgerTests(Base):
    def test_stdin_round_trip_and_ledger_is_sealed(self):
        self.put('TOKEN')
        self.assertEqual(self.seal('get', 'TOKEN', check=True).stdout, VALUE)
        self.assertEqual(self.ledger.stat().st_mode & 0o777, 0o600)
        raw = self.ledger.read_bytes()
        self.assertNotIn(b'line one', raw)
        self.assertNotIn(b'quoted', raw)
        self.assertEqual(os.listdir(self.ledger.parent), ['secrets.enc.yaml'])

    def test_stdin_flag_and_trailing_newline_kept(self):
        self.seal('set', 'A', '--stdin', input='v\n', check=True)
        self.assertEqual(self.seal('get', 'A').stdout, 'v\n')

    def test_set_never_prints_the_value(self):
        p = self.put('TOKEN', 'zz-secret-zz')
        self.assertNotIn('zz-secret-zz', p.stdout + p.stderr)
        self.assertNotIn('zz-secret-zz', self.seal('list').stdout)

    def test_list_names_only(self):
        self.put('B_KEY', 'bbb-value')
        self.put('A_KEY', 'aaa-value')
        p = self.seal('list', check=True)
        self.assertEqual(p.stdout, 'A_KEY\nB_KEY\n')
        self.assertNotIn('value', p.stdout + p.stderr)

    def test_rotation_overwrites(self):
        self.put('TOKEN', 'old')
        self.put('TOKEN', 'new')
        self.assertEqual(self.seal('get', 'TOKEN').stdout, 'new')
        self.assertEqual(self.seal('list').stdout, 'TOKEN\n')

    def test_rm(self):
        self.put('TOKEN')
        self.put('KEEP', 'x')
        self.seal('rm', 'TOKEN', check=True)
        self.assertEqual(self.seal('list').stdout, 'KEEP\n')
        self.assertEqual(self.ledger.stat().st_mode & 0o777, 0o600)
        p = self.seal('rm', 'TOKEN', check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn('not found', p.stderr)

    def test_exec_exposes_keys_and_quotes_args(self):
        self.put('TOKEN', VALUE)
        work = self.tmp / 'cwd'
        work.mkdir()
        p = self.seal('exec', '--', 'sh', '-c', 'printf "%s|%s|%s|%s" "$TOKEN" "$1" "$2" "$(pwd)"', 'sh',
                      "it's a $b", 'x;y', cwd=work, check=True)
        self.assertEqual(p.stdout, f"{VALUE}|it's a $b|x;y|{work.resolve()}")

    def test_exec_passes_exit_status_and_stdin(self):
        self.put('TOKEN', 'v')
        self.assertEqual(self.seal('exec', '--', 'sh', '-c', 'exit 7').returncode, 7)
        self.assertEqual(self.seal('exec', '--', 'cat', input='piped').stdout, 'piped')

    def test_bad_key_names_rejected(self):
        for bad in ['bad-name', '1abc', 'a b', 'x"]', 'a/b', '../x']:
            p = self.seal('set', bad, input='v')
            self.assertEqual(p.returncode, 1, bad)
            self.assertIn('seal: bad key name', p.stderr)
            self.assertEqual(self.seal('get', bad).returncode, 1)
        self.assertFalse(self.ledger.exists())

    def test_missing_key_on_get(self):
        self.put('TOKEN')
        p = self.seal('get', 'NOPE')
        self.assertEqual((p.returncode, p.stdout), (1, ''))
        self.assertIn('NOPE not found', p.stderr)

    def test_missing_ledger(self):
        for args in (['get', 'K'], ['list'], ['rm', 'K'], ['exec', '--', 'true']):
            p = self.seal(*args)
            self.assertEqual(p.returncode, 1, args)
            self.assertIn('no ledger', p.stderr)

    def test_empty_stdin_rejected(self):
        p = self.seal('set', 'K', input='')
        self.assertEqual(p.returncode, 1)
        self.assertFalse(self.ledger.exists())

    def test_no_recipients_clear_error_no_ledger(self):
        env = {k: v for k, v in self.env.items() if k != 'SEAL_RECIPIENTS'}
        p = self.seal('set', 'K', input='v', env=env)
        self.assertEqual(p.returncode, 1)
        self.assertIn('no recipients', p.stderr)
        self.assertFalse(self.ledger.parent.exists())

    def test_recipients_file_and_validation(self):
        env = {k: v for k, v in self.env.items() if k != 'SEAL_RECIPIENTS'}
        pubs = self.tmp / 'pubs.txt'
        pubs.write_text(f'# team\nnot-a-key\n{self.pub}\n')
        env['SEAL_RECIPIENTS_FILE'] = str(pubs)
        self.seal('set', 'K', input='v', env=env, check=True)
        self.assertEqual(self.seal('get', 'K').stdout, 'v')
        bad = dict(self.env, SEAL_RECIPIENTS='nope', SEAL_LEDGER=str(self.tmp / 'other.yaml'))
        self.assertIn('not an age public key', self.seal('set', 'K', input='v', env=bad).stderr)
        self.assertFalse((self.tmp / 'other.yaml').exists())

    def test_default_pubs_file_and_default_ledger_path(self):
        env = {k: v for k, v in self.env.items() if k not in ('SEAL_RECIPIENTS', 'SEAL_LEDGER')}
        pubs = self.home / '.config/sops/age/pubs.txt'
        pubs.parent.mkdir(parents=True)
        pubs.write_text(self.pub + '\n')
        self.seal('set', 'K', input='v', env=env, check=True)
        default = self.home / '.local/share/seal/secrets.enc.yaml'
        self.assertTrue(default.is_file())
        self.assertEqual(default.stat().st_mode & 0o777, 0o600)
        self.assertEqual(default.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.seal('get', 'K', env=env).stdout, 'v')

    def test_l_flag_overrides_env(self):
        other = self.tmp / 'o.enc.yaml'
        self.seal('-l', str(other), 'set', 'K', input='v', check=True)
        self.assertTrue(other.exists())
        self.assertFalse(self.ledger.exists())
        self.assertEqual(self.seal('-l', str(other), 'get', 'K').stdout, 'v')

    def test_relative_ledger_and_stray_sops_yaml(self):
        work = self.tmp / 'rel'
        work.mkdir()
        (work / '.sops.yaml').write_text('creation_rules:\n  - path_regex: .*\n    age: age1invalidinvalid\n')
        env = dict(self.env, SEAL_LEDGER='sub/l.enc.yaml')
        (work / 'sub').mkdir()
        self.seal('set', 'K', input='v', env=env, cwd=work, check=True)
        self.assertEqual(self.seal('get', 'K', env=env, cwd=work).stdout, 'v')

    def test_refuses_xtrace(self):
        p = subprocess.run(['bash', '-x', SEAL, 'set', 'K'], input='v', capture_output=True, text=True, env=self.env)
        self.assertEqual(p.returncode, 2)
        self.assertIn('xtrace', p.stderr)
        self.assertFalse(self.ledger.exists())
        p = subprocess.run([SEAL, 'list'], capture_output=True, text=True, env=dict(self.env, SHELLOPTS='xtrace'))
        self.assertEqual(p.returncode, 2)

    def test_usage(self):
        p = self.seal('-h')
        self.assertEqual(p.returncode, 0)
        err = p.stdout + p.stderr
        for word in ('set', 'get', 'list', 'rm', 'exec', 'file', 'printf %s "$VAL" | seal set KEY'):
            self.assertIn(word, err)
        self.assertEqual(self.seal().returncode, 2)
        self.assertEqual(self.seal('bogus').returncode, 2)


class PromptTests(Base):
    def spawn(self, *args):
        pid, master = pty.fork()
        if pid == 0:
            os.execve(SEAL, [SEAL, *args], self.env)
        self.addCleanup(lambda: os.close(master) if master is not None else None)
        return pid, master

    def read_until(self, master, needle, transcript=b''):
        deadline = time.monotonic() + 10
        while needle not in transcript and time.monotonic() < deadline:
            if select.select([master], [], [], 1)[0]:
                try:
                    transcript += os.read(master, 4096)
                except OSError:
                    break
        self.assertIn(needle, transcript)
        return transcript

    def test_hidden_prompt_no_echo_and_mismatch_reprompts(self):
        pid, master = self.spawn('set', 'PROMPTED')
        t = self.read_until(master, b'value for PROMPTED:')
        time.sleep(.2)  # let the shell turn echo off after printing the prompt
        os.write(master, b'first-secret\n')
        t = self.read_until(master, b'again:', t)
        time.sleep(.2)
        os.write(master, b'different-secret\n')
        t = self.read_until(master, b'did not match', t)
        t = self.read_until(master, b'value for PROMPTED:', t[t.index(b'did not match'):])
        time.sleep(.2)
        os.write(master, b'final-secret\n')
        t = self.read_until(master, b'again:', t)
        time.sleep(.2)
        os.write(master, b'final-secret\n')
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        while select.select([master], [], [], 0)[0]:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            t += chunk
        for secret in (b'first-secret', b'different-secret', b'final-secret'):
            self.assertNotIn(secret, t)
        self.assertEqual(self.seal('get', 'PROMPTED').stdout, 'final-secret')

    def test_empty_value_reprompts(self):
        pid, master = self.spawn('set', 'E')
        t = self.read_until(master, b'value for E:')
        time.sleep(.2)
        os.write(master, b'\n')
        t = self.read_until(master, b'again:', t)
        time.sleep(.2)
        os.write(master, b'\n')
        t = self.read_until(master, b'did not match', t)
        os.kill(pid, 15)
        os.waitpid(pid, 0)
        self.assertFalse(self.ledger.exists())


class FileModeTests(Base):
    """The one-shot `seal file` mode (whole YAML file in, sealed YAML file out)."""

    def setUp(self):
        super().setUp()
        self.src = self.tmp / 'in.yaml'
        self.src.write_text('token: example-secret\n')
        self.out = self.tmp / 'out.enc.yaml'
        self.env.pop('SEAL_RECIPIENTS')

    def test_round_trip_mode_and_no_overwrite(self):
        p = self.seal('file', '-r', self.pub, str(self.src), str(self.out))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), str(self.out))
        self.assertNotIn('example-secret', p.stdout + p.stderr)
        self.assertEqual(self.out.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('example-secret', self.out.read_text())
        d = subprocess.run(['sops', '-d', str(self.out)], capture_output=True, text=True, env=self.env)
        self.assertEqual(d.returncode, 0, d.stderr)
        self.assertIn('example-secret', d.stdout)
        again = self.seal('file', '-r', self.pub, str(self.src), str(self.out))
        self.assertEqual(again.returncode, 1)
        self.assertIn('exists', again.stderr)
        self.assertEqual(self.seal('file', '-f', '-r', self.pub, str(self.src), str(self.out)).returncode, 0)

    def test_recipients_required_and_validated(self):
        self.assertIn('no recipients', self.seal('file', str(self.src), str(self.out)).stderr)
        self.assertIn('not an age public key', self.seal('file', '-r', 'nope', str(self.src), str(self.out)).stderr)
        self.assertFalse(self.out.exists())

    def test_env_recipients(self):
        p = self.seal('file', str(self.src), str(self.out), env=dict(self.env, SEAL_RECIPIENTS=self.pub))
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_recipients_file_flag(self):
        rf = self.tmp / 'r.txt'
        rf.write_text(f'# team\n\n{self.pub} # me\n')
        self.seal('file', '-R', str(rf), str(self.src), str(self.out), check=True)
        d = subprocess.run(['sops', '-d', str(self.out)], capture_output=True, text=True, env=self.env)
        self.assertIn('example-secret', d.stdout)

    def test_default_recipients_file_fallback(self):
        pubs = self.home / '.config/sops/age/pubs.txt'
        pubs.parent.mkdir(parents=True)
        pubs.write_text(self.pub + '\n')
        p = self.seal('file', str(self.src), str(self.out), check=True)
        self.assertIn('1 recipient', p.stderr)
        d = subprocess.run(['sops', '-d', str(self.out)], capture_output=True, text=True, env=self.env)
        self.assertIn('example-secret', d.stdout)

    def test_failure_leaves_nothing_behind(self):
        self.src.write_text('token: [unclosed\n')
        p = self.seal('file', '-r', self.pub, str(self.src), str(self.out))
        self.assertEqual(p.returncode, 1)
        self.assertEqual(sorted(x.name for x in self.tmp.iterdir()),
                         ['home', 'in.yaml', 'key.txt'])

    def test_force_replace_keeps_old_output_on_failure(self):
        self.assertEqual(self.seal('file', '-r', self.pub, str(self.src), str(self.out)).returncode, 0)
        before = self.out.read_bytes()
        self.src.write_text('token: [unclosed\n')
        self.assertEqual(self.seal('file', '-f', '-r', self.pub, str(self.src), str(self.out)).returncode, 1)
        self.assertEqual(self.out.read_bytes(), before)

    def test_refuses_xtrace(self):
        p = subprocess.run(['bash', '-x', SEAL, 'file', '-r', self.pub, str(self.src), str(self.out)],
                           capture_output=True, text=True, env=self.env)
        self.assertEqual(p.returncode, 2)
        self.assertFalse(self.out.exists())


if __name__ == '__main__':
    unittest.main()
