#!/usr/bin/env python3
"""Collect versioned module notices without executing the Mihomo binary.

Module zip contents are verified against h1 hashes embedded by Go in the binary.
The hashing format follows https://pkg.go.dev/golang.org/x/mod/sumdb/dirhash.
"""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import urllib.parse
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def varint(data, position):
    value = shift = 0
    while shift < 64:
        byte = data[position]
        position += 1
        value |= (byte & 127) << shift
        if not byte & 128:
            return value, position
        shift += 7
    raise ValueError('Invalid build information length')


def build_info(binary):
    position = binary.find(b'\xff Go buildinf:')
    if position < 0 or not binary[position + 15] & 2:
        raise ValueError('Expected modern inline Go build information')
    position += 32
    length, position = varint(binary, position)
    go = binary[position:position + length].decode()
    position += length
    length, position = varint(binary, position)
    metadata = binary[position:position + length][16:-16].decode()
    modules = []
    for line in metadata.splitlines():
        fields = line.split('\t')
        if fields[0] == 'dep':
            modules.append(dict(module=fields[1], version=fields[2], h1=fields[3] if len(fields) > 3 else ''))
        elif fields[0] == '=>':
            previous = modules[-1]
            modules[-1] = dict(module=fields[1], version=fields[2], h1=fields[3],
                               replaces=previous['module'])
    return go, metadata, modules


def proxy_escape(value):
    return urllib.parse.quote(''.join('!' + c.lower() if c.isupper() else c for c in value), safe='/!')


def fetch(url, destination):
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + '.partial')
        subprocess.run(['curl', '--fail', '--location', '--silent', '--show-error',
                        '--retry', '2', '--connect-timeout', '15', '--max-time', '120',
                        '--proto', '=https', '--proto-redir', '=https', url, '-o', str(temporary)],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        temporary.replace(destination)
    return destination.read_bytes()


def module_notices(module, cache):
    path, version = module['module'], module['version']
    url = 'https://proxy.golang.org/%s/@v/%s.zip' % (proxy_escape(path), proxy_escape(version))
    result = dict(module, url=url, notices=[])
    try:
        data = fetch(url, cache / (sha((path + '@' + version).encode()) + '.zip'))
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            if len(set(names)) != len(names) or any('\n' in name for name in names):
                raise ValueError('Invalid module archive paths')
            summary = hashlib.sha256()
            for name in sorted(names):
                summary.update((sha(archive.read(name)) + '  ' + name + '\n').encode())
            actual = 'h1:' + base64.b64encode(summary.digest()).decode()
            if actual != module['h1']:
                raise ValueError('Module content differs from embedded Go h1 hash')
            prefix = path + '@' + version + '/'
            for name in sorted(names):
                if name.endswith('/') or not name.startswith(prefix):
                    continue
                relative = name[len(prefix):]
                parts = PurePosixPath(relative).parts
                if any(re.match(r'(?i)^(licen[cs]e|notice|copying|copyright|authors)([._-].*)?$', part)
                       or part.lower() in ('licenses', 'licences', 'notices') for part in parts):
                    text = archive.read(name)
                    result['notices'].append({'path': relative, 'sha256': sha(text),
                                              'text': text.decode('utf-8', 'replace')})
            if not result['notices']:
                result['error'] = 'No standalone LICENSE/NOTICE/COPYING/COPYRIGHT text in verified module'
    except Exception as error:
        result['error'] = str(error)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', type=Path, required=True, help='Pinned official compressed Mihomo binary')
    parser.add_argument('--additional-binary', type=Path, action='append', default=[])
    parser.add_argument('--version', default='v1.19.29')
    parser.add_argument('--source', type=Path, required=True, help='Official Mihomo source tar.gz for this tag')
    parser.add_argument('--cache', type=Path, default=ROOT / 'runtime/notice-cache')
    parser.add_argument('--output', type=Path, default=ROOT / 'packaging/third-party')
    args = parser.parse_args()
    hashes = dict(line.split()[::-1] for line in (ROOT / 'checksums' / ('mihomo-%s.sha256' % args.version)).read_text().splitlines() if line.strip())
    compressed = args.binary.read_bytes()
    if sha(compressed) != hashes[args.binary.name]:
        raise RuntimeError('Official core archive checksum mismatch')
    go, metadata, modules = build_info(gzip.decompress(compressed))
    binaries = [{'name': args.binary.name, 'sha256': sha(compressed), 'go': go}]
    signatures = {(m['module'], m['version'], m['h1']) for m in modules}
    for additional in args.additional_binary:
        data = additional.read_bytes()
        if sha(data) != hashes[additional.name]:
            raise RuntimeError('Additional official core checksum mismatch')
        other_go, _metadata, other_modules = build_info(gzip.decompress(data))
        for module in other_modules:
            signature = (module['module'], module['version'], module['h1'])
            if signature not in signatures:
                signatures.add(signature)
                modules.append(module)
        binaries.append({'name': additional.name, 'sha256': sha(data), 'go': other_go})
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda module: module_notices(module, args.cache), modules))
    sections = ['Mihomo %s: third-party notices from versioned module archives.\n'
                'Core: %s\nCore SHA-256: %s\nGo toolchain: %s\n'
                'Module archives are verified using the exact Go h1 values embedded in this official binary.\n'
                'Nested original notices are retained; some refer to optional or test code.\n' %
                (args.version, args.binary.name, sha(compressed), go)]
    for result in results:
        for notice in result['notices']:
            sections.append('\n' + '=' * 78 + '\nModule: %s@%s\nArchive: %s\n'
                            'Original file: %s\nSHA-256: %s\n\n%s\n' %
                            (result['module'], result['version'], result['url'], notice['path'],
                             notice['sha256'], notice['text']))
    embedded = []
    with tarfile.open(args.source) as archive:
        for member in archive.getmembers():
            if member.isfile() and PurePosixPath(member.name).name.upper().startswith(('LICENSE', 'NOTICE', 'COPYING', 'COPYRIGHT')):
                content = archive.extractfile(member).read()
                embedded.append({'path': member.name, 'sha256': sha(content)})
                sections.append('\n' + '=' * 78 + '\nMihomo source: %s\n\n%s\n' %
                                (member.name, content.decode('utf-8', 'replace')))
    go_url = 'https://raw.githubusercontent.com/golang/go/%s/LICENSE' % go
    go_license = fetch(go_url, args.cache / (go + '-LICENSE.txt'))
    sections.append('\n' + '=' * 78 + '\nGo standard library/runtime: %s\nSource: %s\n\n%s\n' %
                    (go, go_url, go_license.decode()))
    # Mihomo's copied faketcp directory explicitly names tcpraw as its origin but
    # retains only that attribution line, not tcpraw's original MIT terms.
    tcpraw_url = 'https://raw.githubusercontent.com/xtaci/tcpraw/cbf96359f63c20cfd3b27a9b3927d1ebc6f4182d/LICENSE'
    tcpraw = fetch(tcpraw_url, args.cache / 'xtaci-tcpraw-cbf96359-LICENSE.txt')
    sections.append('\n' + '=' * 78 + '\nCopied faketcp origin: xtaci/tcpraw\nSource: %s\n\n%s\n' %
                    (tcpraw_url, tcpraw.decode()))
    args.output.mkdir(parents=True, exist_ok=True)
    name = 'mihomo-%s' % args.version
    notice_file = args.output / (name + '-NOTICES.txt')
    notice_file.write_text('\n'.join(sections), encoding='utf-8')
    for result in results:
        for notice in result['notices']:
            notice.pop('text')
    report = {'format': 1, 'version': args.version, 'binary': args.binary.name,
              'binary_sha256': sha(compressed), 'go': go, 'build_info': metadata,
              'binaries': binaries,
              'source_sha256': sha(args.source.read_bytes()), 'source_notices': embedded,
              'go_license_url': go_url, 'go_license_sha256': sha(go_license),
              'copied_code_notices': [{'origin': 'xtaci/tcpraw', 'url': tcpraw_url, 'sha256': sha(tcpraw)}],
              'modules': results, 'notice_sha256': sha(notice_file.read_bytes())}
    (args.output / (name + '-index.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    errors = [result for result in results if result.get('error')]
    print('%d modules, %d original notices, %d unresolved modules' %
          (len(results), sum(len(result['notices']) for result in results), len(errors)), flush=True)
    for result in errors:
        print(result['module'] + '@' + result['version'] + ': ' + result['error'], flush=True)
    return bool(errors)


if __name__ == '__main__':
    raise SystemExit(main())
