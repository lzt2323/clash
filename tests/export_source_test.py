#!/usr/bin/env python3
import importlib.util
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('export_source', ROOT / 'packaging/export_source.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ExportSourceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'source'
        self.root.mkdir()
        for name in module.ROOT_FILES:
            self.write(name, 'source fixture\n')
        self.write('clash', '#!/bin/sh\nexit 0\n').chmod(0o755)
        self.write('temp/templete_config.yaml', "port: 7890\nsecret: 'legacy-value'\n")
        self.output = self.root / 'runtime/source-export'

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_allowlist_excludes_user_data_and_preserves_fixtures(self):
        included = ['scripts/main.py', 'scripts/tests/check.sh', 'tests/test.py',
                    'tests/fixtures/provider.yaml', 'packaging/runtime-lock.json',
                    '.github/workflows/release.yml', 'checksums/core.sha256']
        for name in included:
            self.write(name, 'sample\n')
        excluded = ['.env', '.env.bak', '.clash-install.json', '.git/config',
                    'conf/config.yaml', 'runtime/mvp/state.json', 'bin/mihomo',
                    'python/bin/python3', 'public/index.html', 'tools/subconverter/bin',
                    'asserts/secret.png', 'research-checkout/.env',
                    'scripts/.env', 'scripts/main.py.bak', 'scripts/__pycache__/main.pyc',
                    'tests/private.yaml', 'packaging/password.txt']
        for name in excluded:
            self.write(name, 'https://private.example.org/sub?token=private\n')
        result = module.export_source(self.root, self.output)
        self.assertEqual(set(result['files']), set(module.ROOT_FILES) | set(included))
        with tarfile.open(result['archive'], 'r:gz') as bundle:
            members = bundle.getmembers()
            self.assertEqual({m.name for m in members}, {'clash-linux-source/' + name for name in result['files']})
            self.assertTrue(all(m.isfile() for m in members))
            self.assertFalse(any(b'private.example.org' in bundle.extractfile(m).read() for m in members))
        self.assertEqual((self.output / 'clash').stat().st_mode & 0o777, 0o755)
        self.assertNotIn('legacy-value', (self.output / 'temp/templete_config.yaml').read_text())
        self.assertIn('legacy-value', (self.root / 'temp/templete_config.yaml').read_text())
        self.assertFalse((self.output / '.git').exists())

    def test_repeatable_and_custom_archive(self):
        archive = self.root / 'dist/source-v1.tar.gz'
        first = module.export_source(self.root, self.output, archive)
        before = archive.read_bytes()
        second = module.export_source(self.root, self.output, archive)
        self.assertEqual(first, second)
        self.assertEqual(archive.read_bytes(), before)
        self.write('scripts/new.py', 'print("updated")\n')
        module.export_source(self.root, self.output, archive)
        self.assertTrue((self.output / 'scripts/new.py').is_file())

    def test_refuses_modified_or_unowned_output(self):
        self.output.mkdir(parents=True)
        with self.assertRaises(module.ExportError):
            module.export_source(self.root, self.output)
        self.output.rmdir()
        module.export_source(self.root, self.output)
        (self.output / 'README.md').write_text('local user edits')
        with self.assertRaises(module.ExportError):
            module.export_source(self.root, self.output)
        self.assertEqual((self.output / 'README.md').read_text(), 'local user edits')

    def test_refuses_history_or_linked_sources(self):
        self.write('scripts/link.py', 'source').unlink()
        (self.root / 'scripts/link.py').symlink_to(self.root / '.env')
        with self.assertRaises(module.ExportError):
            module.export_source(self.root, self.output)
        (self.root / 'scripts/link.py').unlink()
        module.export_source(self.root, self.output)
        (self.output / '.git').mkdir()
        with self.assertRaises(module.ExportError):
            module.export_source(self.root, self.output)

    def test_scanner_rejects_credentials_without_echoing_them(self):
        for material in ['https://' + 'subscriptions.real-domain.io/sub?token=do-not-echo',
                         'https://' + 'user:do-not-echo@real-domain.io/sub',
                         '-----BEGIN ' + 'PRIVATE KEY-----',
                         'ghp_' + 'z' * 36]:
            self.write('scripts/unsafe.py', material)
            with self.assertRaises(module.ExportError) as result:
                module.export_source(self.root, self.output)
            self.assertNotIn('do-not-echo', str(result.exception))
            self.assertIn('scripts/unsafe.py', str(result.exception))
        self.write('scripts/unsafe.py', 'https://example.invalid/sub?token=fixture')
        module.export_source(self.root, self.output)

    def test_destination_safety(self):
        for output in [self.root, self.root.parent, self.root / 'scripts/export']:
            with self.assertRaises(module.ExportError):
                module.export_source(self.root, output)
        with self.assertRaises(module.ExportError):
            module.export_source(self.root, self.output, self.output / 'archive.tar.gz')


if __name__ == '__main__':
    unittest.main()
