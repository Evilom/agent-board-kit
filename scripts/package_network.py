"""Build a portable source package and publish verified copies to configured SMB shares."""
import argparse
import ast
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_state():
    try:
        commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, stderr=subprocess.DEVNULL, text=True).strip()
        dirty = bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip())
        timestamp = int(subprocess.check_output(['git', 'show', '-s', '--format=%ct', 'HEAD'], cwd=ROOT, text=True))
        return {'commit': commit, 'dirty': dirty, 'timestamp': timestamp}
    except (OSError, subprocess.CalledProcessError):
        return {'commit': None, 'dirty': True, 'timestamp': 315532800}


def publish_package(archive, manifest, shares):
    if not shares:
        raise ValueError('Configure distribution.smb_shares in the existing local config before publishing')
    if not manifest.get('source_commit') or manifest.get('source_dirty'):
        raise ValueError('Commit source changes before publishing an immutable release')
    if sha256(archive) != manifest['sha256']:
        raise ValueError('Local archive SHA256 does not match its manifest')
    release_name = manifest['version'] + '-' + manifest['source_commit'][:12]
    published = []
    for share in shares:
        root = Path(share['path'])
        if not root.is_dir() or (share.get('mount') and not os.path.ismount(share['mount'])):
            raise ValueError('SMB destination is not available: ' + share['name'])
        release = root / release_name
        if release.exists():
            saved = json.loads((release / 'manifest.json').read_text(encoding='utf-8'))
            if saved != manifest or sha256(release / manifest['file']) != manifest['sha256']:
                raise ValueError('Existing release differs; refusing to overwrite: ' + release_name)
        else:
            with tempfile.TemporaryDirectory(prefix='.upload-', dir=root) as staging:
                staged = Path(staging)
                staged.chmod(0o755)
                shutil.copyfile(archive, staged / manifest['file'])
                if sha256(staged / manifest['file']) != manifest['sha256']:
                    raise ValueError('Shared archive readback failed: ' + share['name'])
                (staged / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
                os.rename(staged, release)
        if sha256(release / manifest['file']) != manifest['sha256']:
            raise ValueError('Published archive readback failed: ' + share['name'])
        latest = dict(manifest, directory=release_name)
        fd, temporary = tempfile.mkstemp(prefix='.latest-', dir=root)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(latest, stream, ensure_ascii=False, indent=2); stream.write('\n')
                stream.flush(); os.fsync(stream.fileno())
            os.chmod(temporary, 0o644)
            os.replace(temporary, root / 'latest.json')
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        published.append({'name': share['name'], 'directory': str(release),
                          'uri': share.get('uri', '').rstrip('/') + '/' + release_name + '/' + manifest['file'],
                          'sha256': manifest['sha256']})
    return published


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publish', action='store_true', help='Publish to SMB destinations from the central local config')
    parser.add_argument('--config', default=str(ROOT / '.runtime/config.json'))
    args = parser.parse_args()
    source = source_state()
    if args.publish and (source['dirty'] or not source['commit']):
        raise ValueError('Commit source changes before publishing')
    version_line = next(line for line in (ROOT / 'board_network/__init__.py').read_text(encoding='utf-8').splitlines() if line.startswith('VERSION = '))
    version = ast.literal_eval(version_line.split('=', 1)[1].strip())
    files = [ROOT / name for name in (
        ".gitignore", "agent_board.py", "install.py", "schema.json", "LICENSE", "README.md", "README.zh-CN.md",
        "SKILL.md", "AGENTS.snippet.md", "test_agent_board_kit.py", "test_board_network.py", "test_collaboration.py", "test_network_runtime.py", "test_coordination.py", "test_ecosystem.py",
        "agents/openai.yaml", "scripts/server.sh", "scripts/client.sh", "scripts/client.ps1", "scripts/open.sh", "scripts/open.ps1", "scripts/update.sh", "scripts/update.ps1", "scripts/smoke_network.py", "scripts/package_network.py")]
    for folder, pattern in (("board_network", "*.py"), ("board_network/static", "*"), ("docs", "*.md"), ("examples/network", "*.json")):
        files.extend(sorted((ROOT / folder).glob(pattern)))

    out = ROOT / 'dist'; out.mkdir(exist_ok=True)
    archive = out / ('agent-board-network-' + version + '.zip')
    timestamp = time.gmtime(max(source['timestamp'], 315532800))[:6]
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as zipped:
        for path in files:
            info = zipfile.ZipInfo('agent-board-kit/' + path.relative_to(ROOT).as_posix(), timestamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (path.stat().st_mode & 0o777) << 16
            zipped.writestr(info, path.read_bytes())
    with zipfile.ZipFile(archive) as zipped:
        if zipped.testzip() is not None:
            raise ValueError('Package CRC check failed')
    manifest = {'file': archive.name, 'version': version, 'sha256': sha256(archive), 'files': len(files),
                'source_commit': source['commit'], 'source_dirty': source['dirty']}
    (out / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    result = dict(manifest)
    if args.publish:
        if source_state() != source:
            raise ValueError('Source changed while packaging; no SMB release was published')
        config = json.loads(Path(args.config).read_text(encoding='utf-8'))
        result['published'] = publish_package(archive, manifest, config.get('distribution', {}).get('smb_shares', []))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
