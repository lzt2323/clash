#!/usr/bin/env python3
"""Offline release contracts with synthetic runtimes, never executing binaries."""
import gzip
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('clash_builder', ROOT / 'packaging/build_release.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
metadata_spec = importlib.util.spec_from_file_location('release_metadata', ROOT / 'packaging/release_metadata.py')
release_metadata = importlib.util.module_from_spec(metadata_spec)
metadata_spec.loader.exec_module(release_metadata)


def tar_fixture(path, members):
    with tarfile.open(path, 'w:gz') as tf:
        for name, content, kind in members:
            info = tarfile.TarInfo(name)
            if kind == 'symlink':
                info.type, info.linkname = tarfile.SYMTYPE, content
                tf.addfile(info)
            elif kind == 'hardlink':
                info.type, info.linkname = tarfile.LNKTYPE, content
                tf.addfile(info)
            elif kind == 'device':
                info.type = tarfile.CHRTYPE
                tf.addfile(info)
            else:
                content = content.encode() if isinstance(content, str) else content
                info.size, info.mode = len(content), 0o755 if kind == 'executable' else 0o644
                tf.addfile(info, io.BytesIO(content))


class PackageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='clash-package-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / 'source'
        self.root.mkdir()
        self.output, self.cache = self.directory / 'output', self.directory / 'cache'
        self.output.mkdir()
        self.cache.mkdir()
        for name in ['clash', 'start.sh', 'restart.sh', 'shutdown.sh', 'README.md', 'UPGRADE.md',
                     'LICENSE', 'install.sh', 'scripts/uninstall.py',
                     'scripts/shell_integration.sh', 'scripts/terminal_mvp.py',
                     'temp/mihomo_config.template.yaml', 'temp/templete_config.yaml']:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('secret: "FIXED_BAD_PASSWORD"\n' if name.startswith('temp/') else 'public source')
        # Private data must never enter the package, even when present in source.
        self.root.joinpath('.env').write_text('DO_NOT_PACKAGE_SUBSCRIPTION_TOKEN')
        self.root.joinpath('runtime/mvp').mkdir(parents=True)
        self.root.joinpath('runtime/mvp/state.json').write_text('DO_NOT_PACKAGE_SUBSCRIPTION_TOKEN')
        self.py = self.cache / 'python.tar.gz'
        tar_fixture(self.py, [
            ('python/bin/python3.12', b'ELF synthetic private Python', 'executable'),
            ('python/bin/python3', 'python3.12', 'symlink'),
            ('python/lib/python3.12/curses/__init__.py', 'public curses package', 'file'),
            ('python/lib/python3.12/LICENSE.txt', 'CPYTHON_LICENSE', 'file'),
            ('python/lib/python3.12/site-packages/pip/LICENSE.txt', 'PIP_LICENSE', 'file'),
        ])
        self.licenses = self.cache / 'licenses.tar.gz'
        license_names = ['LICENSE', 'LICENSE.cpython.txt', 'LICENSE.libffi.txt',
                         'LICENSE.ncurses.txt', 'LICENSE.openssl-3.txt', 'LICENSE.zlib.txt',
                         'LICENSE.sqlite.txt', 'LICENSE.bzip2.txt', 'LICENSE.liblzma.txt',
                         'LICENSE.tcl.txt', 'python-licenses.rst']
        tar_fixture(self.licenses, [('upstream-release/' + name, 'ORIGINAL_' + name, 'file')
                                   for name in license_names])
        self.core = self.cache / 'core.gz'
        self.core.write_bytes(gzip.compress(b'ELF synthetic core'))
        self.lock = {'python': {'version': '3.12.14', 'release': '20260924',
                               'assets': {arch: {'name': 'python.tar.gz', 'sha256': builder.digest(self.py)}
                                          for arch in ('amd64', 'arm64')},
                               'licenses': {'name': 'licenses.tar.gz', 'url': 'https://upstream.invalid/licenses',
                                            'sha256': builder.digest(self.licenses)}},
                     'mihomo': {'version': 'v1.19.29'}}
        self.root.joinpath('packaging').mkdir()
        self.root.joinpath('packaging/runtime-lock.json').write_text(json.dumps(self.lock))
        self.root.joinpath('checksums').mkdir()
        self.root.joinpath('checksums/mihomo-v1.19.29.sha256').write_text('\n'.join(
            builder.digest(self.core) + '  mihomo-linux-' + arch + '-v1.19.29.gz'
            for arch in ('amd64', 'arm64')))
        notices = self.root / 'packaging/third-party'
        notices.mkdir()
        notices.joinpath('mihomo-v1.19.29-NOTICES.txt').write_text('ORIGINAL_MODULE_COPYRIGHT')
        notices.joinpath('mihomo-v1.19.29-index.json').write_text(json.dumps({
            'version': 'v1.19.29', 'notice_sha256': builder.digest(notices / 'mihomo-v1.19.29-NOTICES.txt'),
            'binaries': [{'name': 'mihomo-linux-' + arch + '-v1.19.29.gz', 'sha256': builder.digest(self.core)}
                         for arch in ('amd64', 'arm64')]}))

    def fake_download(self, url, expected, destination):
        path = self.licenses if 'licenses' in url else self.core if 'mihomo' in url else self.py
        self.assertEqual(builder.digest(path), expected)
        return path

    def build(self, arch='amd64'):
        with patch.object(builder, 'ROOT', self.root), patch.object(builder.sys, 'platform', 'linux'), \
                patch.object(builder, 'download', self.fake_download), \
                patch.object(builder.subprocess, 'run'):
            builder.build(arch, 'v0.1.0', self.cache, self.output)
        return self.output / ('clash-linux-' + arch + '.tar.gz')

    def test_manifest_matches_all_regular_archive_members_without_self_reference(self):
        archive = self.build()
        with tarfile.open(archive) as tf:
            members = tf.getmembers()
            self.assertTrue(all(member.isfile() for member in members))
            self.assertTrue(all(member.name.startswith('clash-linux/') for member in members))
            self.assertTrue(all('..' not in Path(member.name).parts for member in members))
            metadata = json.load(tf.extractfile('clash-linux/BUILD.json'))
            self.assertEqual(metadata['data_format'], 1)
            self.assertEqual(metadata['min_upgrade_version'], 'v0.1.0')
            expected = {member.name[len('clash-linux/'):] for member in members} - {'BUILD.json'}
            self.assertEqual(set(metadata['files']), expected)
            self.assertNotIn('BUILD.json', metadata['files'])
            self.assertEqual(tf.extractfile('clash-linux/python/bin/python3').read(), b'ELF synthetic private Python')
            self.assertTrue(tf.getmember('clash-linux/bin/mihomo').mode & 0o111)

    def test_private_data_fixed_secret_and_license_completeness(self):
        archive = self.build()
        with tarfile.open(archive) as tf:
            names = tf.getnames()
            self.assertFalse(any('/runtime/' in name or name.endswith('/.env') for name in names))
            for name in names:
                data = tf.extractfile(name).read()
                self.assertNotIn(b'DO_NOT_PACKAGE_SUBSCRIPTION_TOKEN', data)
                self.assertNotIn(b'FIXED_BAD_PASSWORD', data)
            self.assertEqual(tf.extractfile('clash-linux/python/licenses/LICENSE.openssl-3.txt').read(),
                             b'ORIGINAL_LICENSE.openssl-3.txt')
            self.assertEqual(tf.extractfile('clash-linux/python/lib/python3.12/site-packages/pip/LICENSE.txt').read(),
                             b'PIP_LICENSE')
            self.assertIn('clash-linux/licenses/mihomo/LICENSE.txt', names)
            self.assertIn('clash-linux/python/licenses/LICENSE.ncurses.txt', names)
            self.assertEqual(tf.extractfile('clash-linux/packaging/third-party/mihomo-v1.19.29-NOTICES.txt').read(),
                             b'ORIGINAL_MODULE_COPYRIGHT')

    def test_two_architectures_have_stable_checksums_and_complete_manifest(self):
        amd64 = self.build()
        first = builder.digest(amd64)
        self.assertEqual(builder.digest(self.build()), first)
        arm64 = self.build('arm64')
        lines = (self.output / 'SHA256SUMS').read_text().splitlines()
        self.assertEqual(set(lines), {builder.digest(path) + '  ' + path.name for path in (amd64, arm64)})
        self.assertEqual((self.output / (amd64.name + '.sha256')).read_text().strip(), first + '  ' + amd64.name)

    def test_latest_metadata_matches_both_architectures_and_refuses_corruption(self):
        self.build()
        self.build('arm64')
        result = release_metadata.metadata(self.output, 'v0.1.0')
        self.assertEqual(result['version'], 'v0.1.0')
        self.assertEqual(result['data_format'], 1)
        self.assertEqual(set(result['assets']), {'amd64', 'arm64'})
        for arch, asset in result['assets'].items():
            path = self.output / ('clash-linux-' + arch + '.tar.gz')
            self.assertEqual(asset['sha256'], builder.digest(path))
            self.assertEqual(asset['size'], path.stat().st_size)
        with self.assertRaisesRegex(ValueError, 'version/architecture'):
            release_metadata.metadata(self.output, 'v0.2.0')
        (self.output / 'clash-linux-arm64.tar.gz').write_bytes(b'broken')
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            release_metadata.metadata(self.output, 'v0.1.0')

    def test_real_bundle_manifest_is_accepted_by_uninstaller(self):
        archive = self.build()
        stage = self.directory / 'installed'
        stage.mkdir()
        with tarfile.open(archive) as tf:
            tf.extractall(stage)
        prefix = stage / 'clash-linux'
        prefix.joinpath('.clash-install.json').write_text(json.dumps(
            {'format': 1, 'version': 'v0.1.0', 'prefix': str(prefix)}))
        uninstall_spec = importlib.util.spec_from_file_location('package_uninstall', ROOT / 'scripts/uninstall.py')
        uninstaller = importlib.util.module_from_spec(uninstall_spec)
        uninstall_spec.loader.exec_module(uninstaller)
        validated, marker, paths = uninstaller._validate(prefix)
        self.assertEqual(validated, prefix)
        self.assertIn(prefix / 'BUILD.json', paths)
        self.assertEqual(marker['version'], 'v0.1.0')

    def test_non_linux_build_is_rejected_before_download(self):
        with patch.object(builder.sys, 'platform', 'darwin'), patch.object(builder, 'download') as download:
            with self.assertRaisesRegex(RuntimeError, 'Linux'):
                builder.build('amd64', 'v0.1.0', self.cache, self.output)
            download.assert_not_called()

    def test_bad_cache_digest_never_satisfies_pinned_download(self):
        path = self.cache / 'invalid-cache'
        path.write_bytes(b'tampered')
        with patch.object(builder.subprocess, 'run', side_effect=subprocess_error()):
            with self.assertRaises(Exception):
                builder.download('https://upstream.invalid', '0' * 64, path)

    def test_archive_traversal_devices_and_link_ancestors_are_rejected(self):
        cases = [('../outside', 'bad', 'file'), ('/outside', 'bad', 'file'),
                 ('python/../../outside', 'bad', 'file'), ('python/device', '', 'device')]
        fixtures = [[item] for item in cases]
        fixtures += [[('python/a', '../../outside', 'symlink')],
                     [('python/link', 'inside', 'symlink'), ('python/link/file', 'bad', 'file')],
                     [('python/a', 'ok', 'file'), ('python/a', 'duplicate', 'file')]]
        for index, members in enumerate(fixtures):
            with self.subTest(members=members):
                archive = self.cache / ('bad-%d.tar.gz' % index)
                tar_fixture(archive, members)
                target = self.directory / ('extract-%d' % index)
                target.mkdir()
                with self.assertRaises((RuntimeError, ValueError)):
                    builder.extract_python(archive, target)
        self.assertFalse((self.directory / 'outside').exists())

    def test_missing_dependency_licenses_and_curses_fail_build_validation(self):
        incomplete = self.cache / 'incomplete-licenses.tar.gz'
        tar_fixture(incomplete, [('upstream/LICENSE', 'license', 'file')])
        stage = self.directory / 'stage'
        stage.mkdir()
        with self.assertRaisesRegex(RuntimeError, 'license'):
            builder.extract_licenses(incomplete, stage)
        with self.assertRaisesRegex(RuntimeError, 'Python executable'):
            builder.validate_runtime(stage)

    def test_native_runtime_smoke_supports_builtin_curses_and_fails_closed(self):
        stage = self.directory / 'native-smoke'
        stage.mkdir()
        builder.extract_python(self.py, stage)
        with patch.object(builder.sys, 'platform', 'linux'), \
                patch.object(builder.platform, 'machine', return_value='x86_64'), \
                patch.object(builder.subprocess, 'run') as run:
            builder.validate_runtime(stage, 'amd64')
            arguments = run.call_args[0][0]
            self.assertEqual(arguments[1:4], ['-E', '-s', '-B'])
            self.assertIn('import curses, _curses, fcntl, ssl', arguments[-1])
            self.assertTrue(run.call_args[1]['check'])
            run.side_effect = subprocess_error()
            with self.assertRaises(Exception):
                builder.validate_runtime(stage, 'amd64')


def subprocess_error():
    import subprocess
    return subprocess.CalledProcessError(1, ['curl'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
