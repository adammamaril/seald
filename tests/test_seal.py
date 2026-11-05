import os
from pathlib import Path
import pty
import select
import signal
import shutil
import subprocess
import tempfile
import time
import unittest

SEAL = os.environ.get('SEAL_CLI', str(Path(__file__).resolve().parents[1] / 'seal'))
HAVE = all(shutil.which(t) for t in ('sops', 'age-keygen'))
VALUE = 'line one\nit\'s "quoted" $HOME \\n {braces}\nlast'


@unittest.skipUnless(HAVE, 'sops and age-keygen required')
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
                    if not k.startswith(('SEAL_', 'SOPS_')) and k not in ('BASH_XTRACEFD', 'XDG_CONFIG_HOME')}
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

    HARD_VALUES = [
        b'multi\nline\nvalue',
        b'quo"te \\back\\slash and a literal \\n',
        b'tab\there\r\nCRLF\r\n',
        b'ctrl\x01\x07\x08\x0c\x1b[31m\x1f del\x7f',
        'ünï©ødé 🔐 日本語'.encode(),
        b'{"looks": "like json"}',
        b'true',
        b'123',
        b'$(touch pwned) `id` ${HOME}',
        b'trailing newline\n',
    ]

    def test_hard_values_round_trip_byte_exact(self):
        for i, v in enumerate(self.HARD_VALUES):
            with self.subTest(value=v):
                k = f'HARD_{i}'
                p = subprocess.run([SEAL, 'set', k], input=v, capture_output=True, env=self.env)
                self.assertEqual(p.returncode, 0, p.stderr)
                got = subprocess.run([SEAL, 'get', k], capture_output=True, env=self.env)
                self.assertEqual(got.stdout, v)
        self.assertFalse((self.tmp / 'work' / 'pwned').exists())
        self.assertFalse(Path('pwned').exists())

    def test_hard_value_reaches_exec_env(self):
        v = 'a "b" \\c\nd\te $(x)'
        self.put('HARD', v)
        p = self.seal('exec', '--', 'sh', '-c', 'printf %s "$HARD"', check=True)
        self.assertEqual(p.stdout, v)

    def test_sops_key_name_reserved(self):
        self.put('A', 'a')
        p = self.seal('set', 'sops', input='x')
        self.assertEqual(p.returncode, 1)
        self.assertIn('reserved', p.stderr)
        self.assertEqual(self.seal('get', 'A').stdout, 'a')

    def test_list_needs_no_identity(self):
        self.put('ONE', 'x')
        self.put('TWO', 'y')
        env = dict(self.env, SOPS_AGE_KEY_FILE=str(self.tmp / 'nonexistent'))
        self.assertEqual(self.seal('list', env=env, check=True).stdout, 'ONE\nTWO\n')

    def test_non_yaml_ledger_rejected(self):
        p = self.seal('-l', str(self.tmp / 'secrets.json'), 'set', 'A', input='x')
        self.assertEqual(p.returncode, 1)
        self.assertIn('.yaml', p.stderr)


class PromptTests(Base):
    def spawn(self, *args):
        pid, master = pty.fork()
        if pid == 0:
            os.execve(SEAL, [SEAL, *args], self.env)
        self.addCleanup(lambda: os.close(master) if master is not None else None)
        return pid, master

    def wait_exit(self, pid, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                return os.waitstatus_to_exitcode(status)
            time.sleep(.05)
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        self.fail('seal did not exit in time')

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
        self.assertEqual(self.wait_exit(pid), 0)
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
        self.wait_exit(pid)
        self.assertFalse(self.ledger.exists())

    def test_f8_stdin_flag_on_terminal_refused(self):
        pid, master = self.spawn('set', 'K', '--stdin')
        self.read_until(master, b'needs a pipe')
        self.assertEqual(self.wait_exit(pid), 1)
        self.assertFalse(self.ledger.exists())

    def test_f9_prompt_keeps_edge_whitespace(self):
        pid, master = self.spawn('set', 'WS')
        t = self.read_until(master, b'value for WS:')
        time.sleep(.2)
        os.write(master, b'  pass phrase \t\n')
        self.read_until(master, b'again:', t)
        time.sleep(.2)
        os.write(master, b'  pass phrase \t\n')
        self.assertEqual(self.wait_exit(pid), 0)
        self.assertEqual(self.seal('get', 'WS').stdout, '  pass phrase \t')


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


class HardeningTests(Base):
    """One test per fix from the security review (finding numbers in the names)."""

    def keypair(self, name):
        k = self.tmp / name
        subprocess.run(['age-keygen', '-o', str(k)], check=True, capture_output=True)
        return k, subprocess.run(['age-keygen', '-y', str(k)], check=True, capture_output=True, text=True).stdout.strip()

    def decrypts_with(self, keyfile, key='K'):
        env = dict(self.env, SOPS_AGE_KEY_FILE=str(keyfile))
        return self.seal('get', key, env=env).returncode == 0

    def test_f1_failed_write_keeps_ledger_and_writes_rename(self):
        self.put('A', 'a')
        self.put('B', 'b')
        before, ino = self.ledger.read_bytes(), self.ledger.stat().st_ino
        # a sops that truncates its target and dies, as on a crash or full disk
        fake = self.tmp / 'fakebin'
        fake.mkdir()
        (fake / 'sops').write_text('#!/bin/sh\nfor a; do f=$p; p=$a; done\n: > "$f"\nexit 1\n')
        (fake / 'sops').chmod(0o755)
        env = dict(self.env, PATH=f'{fake}:{self.env["PATH"]}')
        self.assertEqual(self.seal('set', 'C', input='c', env=env).returncode, 1)
        self.assertEqual(self.seal('rm', 'A', env=env).returncode, 1)
        self.assertEqual(self.ledger.read_bytes(), before)
        self.assertEqual(os.listdir(self.ledger.parent), ['secrets.enc.yaml'])
        self.put('C', 'c')
        self.assertNotEqual(self.ledger.stat().st_ino, ino)
        self.assertEqual(self.seal('get', 'A').stdout, 'a')
        self.ledger.write_bytes(b'')
        self.assertIn('empty', self.seal('get', 'A').stderr)

    def test_f2_stray_sops_yaml_cannot_leave_cleartext(self):
        src = self.tmp / 'in.yaml'
        src.write_text('token: CANARY-f2\n')
        (self.tmp / '.sops.yaml').write_text('creation_rules:\n  - unencrypted_regex: ".*"\n')
        out = self.tmp / 'out.enc.yaml'
        self.seal('file', '-r', self.pub, str(src), str(out), cwd=self.tmp, check=True)
        self.assertNotIn('CANARY-f2', out.read_text())
        self.put('K', 'CANARY-ledger')  # ledger writes ignore it too
        self.assertNotIn(b'CANARY', self.ledger.read_bytes())

    def test_f3_unencrypted_suffix_refused(self):
        p = self.seal('set', 'API_unencrypted', input='CANARY')
        self.assertEqual(p.returncode, 1)
        self.assertIn('_unencrypted', p.stderr)
        self.assertFalse(self.ledger.exists())

    def test_f4_xdg_elsewhere_still_finds_readme_key(self):
        keys = self.home / '.config/sops/age/keys.txt'
        keys.parent.mkdir(parents=True)
        shutil.copy(self.key, keys)
        env = {k: v for k, v in self.env.items() if k != 'SOPS_AGE_KEY_FILE'}
        env['XDG_CONFIG_HOME'] = str(self.tmp / 'xdg')
        self.seal('set', 'K', input='v', env=env, check=True)
        self.seal('set', 'K2', input='w', env=env, check=True)
        self.assertEqual(self.seal('get', 'K2', env=env).stdout, 'w')

    def test_f4_failed_first_set_leaves_no_ledger(self):
        _, other_pub = self.keypair('other.txt')  # we hold no key for this recipient
        p = self.seal('set', 'K', input='v', env=dict(self.env, SEAL_RECIPIENTS=other_pub))
        self.assertEqual(p.returncode, 1)
        self.assertFalse(self.ledger.exists())
        self.assertEqual(os.listdir(self.ledger.parent), [])

    def test_f6_errors_never_print_private_key_or_value(self):
        priv = next(l for l in self.key.read_text().splitlines() if l.startswith('AGE-SECRET-KEY-'))
        src = self.tmp / 'in.yaml'
        src.write_text('a: b\n')
        rf = self.tmp / 'r.txt'
        rf.write_text(priv + '\n')
        for p in (self.seal('file', '-r', priv, str(src), str(self.tmp / 'o.yaml')),
                  self.seal('file', '-R', str(rf), str(src), str(self.tmp / 'o.yaml')),
                  self.seal('set', 'K', input='v', env=dict(self.env, SEAL_RECIPIENTS=priv))):
            self.assertEqual(p.returncode, 1)
            self.assertNotIn(priv, p.stdout + p.stderr)
            self.assertNotIn(priv[15:], p.stdout + p.stderr)
        p = self.seal('set', 'hunter2-value!', input='v')
        self.assertEqual(p.returncode, 1)
        self.assertNotIn('hunter2', p.stderr)

    def test_f7_bad_recipients_line_warns_and_keeps_good_keys(self):
        k2, p2 = self.keypair('k2.txt')
        k3, p3 = self.keypair('k3.txt')
        pubs = self.tmp / 'pubs.txt'
        pubs.write_bytes(f'{self.pub}\r\n  {p2}   # backup\nAGE1TYPO\n{p3}'.encode())
        env = {k: v for k, v in self.env.items() if k != 'SEAL_RECIPIENTS'}
        env['SEAL_RECIPIENTS_FILE'] = str(pubs)
        p = self.seal('set', 'K', input='v', env=env, check=True)
        self.assertIn('ignoring a bad line', p.stderr)
        for k in (self.key, k2, k3):
            self.assertTrue(self.decrypts_with(k), k)

    def test_f7_newline_separated_env_recipients(self):
        k2, p2 = self.keypair('k2.txt')
        self.seal('set', 'K', input='v', env=dict(self.env, SEAL_RECIPIENTS=f'{self.pub}\n{p2}\n'), check=True)
        self.assertTrue(self.decrypts_with(k2))

    def test_f9_nul_refused(self):
        p = subprocess.run([SEAL, 'set', 'K'], input=b'abc\0def', capture_output=True, env=self.env)
        self.assertEqual(p.returncode, 1)
        self.assertIn(b'NUL', p.stderr)
        self.assertFalse(self.ledger.exists())

    def test_f12_yaml_reserved_word_keys_reachable(self):
        for k in ('Yes', 'NO', 'null', 'True'):
            self.put(k, k.lower() + '-v')
        self.assertEqual(self.seal('list').stdout, 'NO\nTrue\nYes\nnull\n')
        self.assertEqual(self.seal('get', 'Yes').stdout, 'yes-v')
        self.seal('rm', 'null', check=True)
        self.assertNotIn('null', self.seal('list').stdout)

    def test_f13_exec_child_gets_no_sops_key_vars(self):
        self.put('TOKEN', 't')
        priv = next(l for l in self.key.read_text().splitlines() if l.startswith('AGE-SECRET-KEY-'))
        env = dict(self.env, SOPS_AGE_KEY_CMD='true', SOPS_AGE_KEY=priv)
        p = self.seal('exec', '--', 'sh', '-c', 'env', env=env, check=True)
        self.assertIn('TOKEN=t', p.stdout)
        self.assertNotIn('SOPS_AGE_', p.stdout)

    def test_f13_sigterm_to_exec_leaves_no_orphan(self):
        self.put('TOKEN', 't')
        pidfile = self.tmp / 'child.pid'
        proc = subprocess.Popen([SEAL, 'exec', '--', 'sh', '-c', f'echo $$ > {pidfile}; exec sleep 30'], env=self.env)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        deadline = time.monotonic() + 10
        while not (pidfile.exists() and pidfile.read_text().strip()) and time.monotonic() < deadline:
            time.sleep(.05)
        child = int(pidfile.read_text())
        proc.terminate()
        proc.wait(timeout=10)
        time.sleep(.2)
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)

    def test_f18_malformed_sops_yaml_above_ledger_is_ignored(self):
        self.put('K', 'v')
        (self.ledger.parent / '.sops.yaml').write_text('creation_rules: [unclosed\n')
        self.put('K2', 'w')
        self.assertEqual(self.seal('get', 'K2', check=True).stdout, 'w')
        self.seal('rm', 'K', check=True)
        self.assertEqual(self.seal('exec', '--', 'sh', '-c', 'printf %s "$K2"', check=True).stdout, 'w')

    def test_f6_comma_joined_r_value_rejected(self):
        src = self.tmp / 'in.yaml'
        src.write_text('a: b\n')
        p = self.seal('file', '-r', f'{self.pub},{self.pub}', str(src), str(self.tmp / 'o.yaml'))
        self.assertEqual(p.returncode, 1)
        self.assertFalse((self.tmp / 'o.yaml').exists())

    def test_f15_f17_symlinked_ledger_and_dash_command_refused(self):
        self.put('K', 'v')
        link = self.tmp / 'link.enc.yaml'
        link.symlink_to(self.ledger)
        self.assertIn('symlink', self.seal('-l', str(link), 'get', 'K').stderr)
        self.assertEqual(self.seal('exec', '--', '-c', 'true').returncode, 2)


if __name__ == '__main__':
    unittest.main()
