import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SEAL = os.environ.get('SEAL_CLI', str(Path(__file__).resolve().parents[1] / 'bin' / 'seal'))


@unittest.skipUnless(shutil.which('sops') and shutil.which('age-keygen'), 'sops and age required')
class SealTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.key = self.tmp / 'key.txt'
        subprocess.run(['age-keygen', '-o', str(self.key)], check=True, capture_output=True)
        self.pub = subprocess.run(['age-keygen', '-y', str(self.key)], check=True, capture_output=True, text=True).stdout.strip()
        self.src = self.tmp / 'in.yaml'
        self.src.write_text('token: example-secret\n')
        self.out = self.tmp / 'out.enc.yaml'
        self.env = {k: v for k, v in os.environ.items() if k != 'SEAL_RECIPIENTS'}

    def run_seal(self, *args, env=None):
        return subprocess.run([SEAL, *args], capture_output=True, text=True, env=env or self.env)

    def test_round_trip_mode_and_no_overwrite(self):
        p = self.run_seal('-r', self.pub, str(self.src), str(self.out))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), str(self.out))
        self.assertNotIn('example-secret', p.stdout + p.stderr)
        self.assertEqual(self.out.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('example-secret', self.out.read_text())
        d = subprocess.run(['sops', '-d', str(self.out)], capture_output=True, text=True,
                           env=dict(self.env, SOPS_AGE_KEY_FILE=str(self.key)))
        self.assertEqual(d.returncode, 0, d.stderr)
        self.assertIn('example-secret', d.stdout)
        again = self.run_seal('-r', self.pub, str(self.src), str(self.out))
        self.assertEqual(again.returncode, 1)
        self.assertIn('exists', again.stderr)
        self.assertEqual(self.run_seal('-f', '-r', self.pub, str(self.src), str(self.out)).returncode, 0)

    def test_recipients_required_and_validated(self):
        self.assertIn('no recipients', self.run_seal(str(self.src), str(self.out)).stderr)
        self.assertIn('not an age public key', self.run_seal('-r', 'nope', str(self.src), str(self.out)).stderr)
        self.assertFalse(self.out.exists())

    def test_env_recipients(self):
        p = self.run_seal(str(self.src), str(self.out), env=dict(self.env, SEAL_RECIPIENTS=self.pub))
        self.assertEqual(p.returncode, 0, p.stderr)


if __name__ == '__main__':
    unittest.main()
