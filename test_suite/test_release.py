import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('release_version', ROOT / 'release_tools/release_version.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ReleaseVersionTests(unittest.TestCase):
    def test_blank_uses_unreleased_version(self):
        self.assertEqual(MODULE.choose_version('1.0.4', ['v1.0.2', 'v1.0.3']), '1.0.4')

    def test_blank_increments_published_version(self):
        self.assertEqual(MODULE.choose_version('1.0.4', ['v1.0.4']), '1.0.5')

    def test_blank_uses_highest_version_if_file_lags(self):
        self.assertEqual(MODULE.choose_version('1.0.3', ['v1.1.8']), '1.1.9')

    def test_explicit_version_syncs_without_prefix(self):
        self.assertEqual(MODULE.choose_version('1.0.4', ['v1.0.3'], 'v1.2.3'), '1.2.3')

    def test_equal_current_unpublished_is_allowed(self):
        self.assertEqual(MODULE.choose_version('1.0.4', [], '1.0.4'), '1.0.4')

    def test_existing_tag_and_downgrade_rejected(self):
        for requested in ('1.0.3', '0.9.9', '1.0.2'):
            with self.subTest(requested=requested), self.assertRaises(ValueError):
                MODULE.choose_version('1.0.4', ['v1.0.3'], requested)

    def test_semantic_numeric_order(self):
        self.assertEqual(MODULE.choose_version('1.0.9', ['v1.0.9', 'v1.0.10']), '1.0.11')

    def test_first_release_uses_version_file(self):
        self.assertEqual(MODULE.choose_version('1.0.4', []), '1.0.4')

    def test_invalid_and_injection_inputs_rejected(self):
        for value in ('v1.2.3; touch /tmp/x', '$(whoami)', '1.2', '1.2.3.4', 'v01.2.3', '1.2.-3', '1.2.3\ninvalid'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                MODULE.choose_version('1.0.4', [], value)

    def test_unrelated_tags_ignored(self):
        self.assertEqual(MODULE.choose_version('1.0.4', ['beta', 'vnext', '1.9.9']), '1.0.4')

    def test_cli_writes_version_and_action_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(['git', 'init', '-q', tmp], check=True)
            (root / 'VERSION').write_text('1.0.4\n')
            env = dict(os.environ, RELEASE_VERSION='v1.2.0', GITHUB_OUTPUT=str(root / 'out'))
            subprocess.run(['python3', str(ROOT / 'release_tools/release_version.py')], cwd=root, env=env, check=True, capture_output=True)
            self.assertEqual((root / 'VERSION').read_text(), '1.2.0\n')
            self.assertEqual((root / 'out').read_text(), 'version=1.2.0\ntag=v1.2.0\n')

    def test_archive_and_tag_include_synced_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(['git', 'init', '-q', tmp], check=True)
            subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=root, check=True)
            subprocess.run(['git', 'config', 'user.email', 'test@example.invalid'], cwd=root, check=True)
            (root / 'VERSION').write_text('1.2.0\n')
            subprocess.run(['git', 'add', 'VERSION'], cwd=root, check=True)
            subprocess.run(['git', 'commit', '-qm', 'version'], cwd=root, check=True)
            subprocess.run(['git', 'tag', 'v1.2.0'], cwd=root, check=True)
            import io, tarfile
            archive = subprocess.check_output(['git', 'archive', '--format=tar', '--prefix=tg-persona/', 'v1.2.0'], cwd=root)
            with tarfile.open(fileobj=io.BytesIO(archive)) as tf:
                self.assertEqual(tf.extractfile('tg-persona/VERSION').read(), b'1.2.0\n')


if __name__ == '__main__':
    unittest.main()
