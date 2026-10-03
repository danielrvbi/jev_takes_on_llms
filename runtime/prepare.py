"""Build the pinned private Go server against the matching installed native payload.

Run with `uv run --no-sync python runtime/prepare.py`. Nothing is installed globally.
"""
import difflib
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / '.runtime'
COMMIT = 'cc4069396f3ad2c370c53eed2e4a42ac13adab84'
VERSION = '0.35.0'
GO_VERSION = 'go1.26.0'
GO_SHA256 = 'b1640525dfe68f066d56f200bef7bf4dce955a1a893bd061de6754c211431023'


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def download(url, target):
    with urllib.request.urlopen(url) as source, target.open('wb') as dest:
        shutil.copyfileobj(source, dest)


def main():
    if (platform.system(), platform.machine()) != ('Darwin', 'arm64'):
        raise SystemExit('This payload reuse recipe requires macOS arm64 and Ollama 0.35.0.')
    PRIVATE.mkdir(exist_ok=True)
    installed = Path(shutil.which('ollama') or '').resolve()
    observed = subprocess.run([str(installed), '--version'], capture_output=True, text=True, check=True)
    if f'ollama version is {VERSION}' not in observed.stdout + observed.stderr:
        raise SystemExit('Installed native payload must match Ollama 0.35.0.')
    archive = PRIVATE / 'go.tar.gz'
    if not archive.exists() or digest(archive) != GO_SHA256:
        download(f'https://go.dev/dl/{GO_VERSION}.darwin-arm64.tar.gz', archive)
    if digest(archive) != GO_SHA256:
        raise SystemExit('Go archive checksum mismatch')
    if not (PRIVATE / 'go/bin/go').exists():
        with tarfile.open(archive) as t:
            t.extractall(PRIVATE, filter='data')
    source = PRIVATE / 'source'
    # Always restore the pinned source before applying the single checked-in patch.
    tarball = PRIVATE / 'source.tar.gz'
    if not tarball.exists():
        download(f'https://api.github.com/repos/ollama/ollama/tarball/{COMMIT}', tarball)
    with tarfile.open(tarball) as t:
        prefix = t.getnames()[0].split('/')[0]
        if not prefix.endswith(COMMIT[:7]):
            raise SystemExit('Source archive does not identify the pinned commit')
        if source.exists():
            shutil.rmtree(source)
        t.extractall(PRIVATE, filter='data')
        (PRIVATE / prefix).rename(source)
    subprocess.run(['git', 'apply', str(ROOT / 'runtime/no-cache.patch')], cwd=source, check=True)
    shutil.copy2(ROOT / 'runtime/no_cache_test.go', source / 'llm/experiment_no_cache_test.go')
    binary = PRIVATE / 'bin/ollama'
    binary.parent.mkdir(exist_ok=True)
    native = PRIVATE / 'bin/lib/ollama'
    native.mkdir(parents=True, exist_ok=True)
    native_files = {}
    for p in installed.parent.iterdir():
        if p.is_file() and (p.name.startswith('lib') or p.name == 'llama-server' or 'LICENSE' in p.name or p.name.endswith('_NOTICE')):
            dest = native / p.name
            shutil.copy2(p, dest, follow_symlinks=True)
            native_files[str(dest.relative_to(PRIVATE))] = digest(dest)
    env = {**os.environ, 'GOTOOLCHAIN': 'local', 'GOPATH': str(PRIVATE / 'gopath'),
           'GOCACHE': str(PRIVATE / 'go-cache')}
    go = str(PRIVATE / 'go/bin/go')
    subprocess.run([go, 'test', './llm', '-run', '^TestExperimentNoCache', '-count=1'], cwd=source, env=env, check=True)
    subprocess.run([go, 'build', '-trimpath', '-ldflags', '-X github.com/ollama/ollama/version.Version=0.35.0', '-o', str(binary), '.'], cwd=source, env=env, check=True)
    manifest = {'version': VERSION, 'commit': COMMIT, 'go_version': GO_VERSION,
                'go_archive_sha256': GO_SHA256, 'source_archive_sha256': digest(tarball),
                'patch_sha256': digest(ROOT / 'runtime/no-cache.patch'),
                'runtime_test_sha256': digest(ROOT / 'runtime/no_cache_test.go'),
                'binary': str(binary.relative_to(PRIVATE)), 'binary_sha256': digest(binary),
                'installed_payload_source': str(installed.parent), 'native_files': native_files}
    (PRIVATE / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    print('Verified private runtime:', binary, flush=True)


if __name__ == '__main__':
    main()
