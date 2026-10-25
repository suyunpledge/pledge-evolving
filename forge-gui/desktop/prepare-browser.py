"""Build-time browser install. End users receive the entire browsers directory."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys

VERSION = '1.64.1'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--destination', type=Path, default=Path(__file__).resolve().parent / 'browsers')
    args = parser.parse_args()
    if importlib.metadata.version('playwright') != VERSION:
        raise RuntimeError(f'Install playwright=={VERSION} before building')
    destination = args.destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    environment = {**os.environ, 'PLAYWRIGHT_BROWSERS_PATH': str(destination)}
    subprocess.run([sys.executable, '-m', 'playwright', 'install', '--no-shell', 'chromium'],
                   env=environment, check=True)
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(destination)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as driver:
        executable = Path(driver.chromium.executable_path)
        if not executable.is_file() or not executable.is_relative_to(destination):
            raise RuntimeError('Downloaded Chromium is incomplete')
        with driver.chromium.launch(executable_path=str(executable), headless=True, chromium_sandbox=True) as browser:
            page = browser.new_page()
            page.set_content('<title>Forge Chromium smoke</title>')
            if page.title() != 'Forge Chromium smoke':
                raise RuntimeError('Browser smoke failed')
            version = browser.version
    (destination / 'forge-browser.json').write_text(json.dumps({
        'playwright': VERSION, 'chromium': version,
        'executable': str(executable.relative_to(destination))}, indent=2), encoding='utf-8')
    integrity = {}
    for folder in destination.iterdir():
        if not folder.is_dir() or folder.name.startswith(('.', '__')):
            continue
        for path in sorted(folder.rglob('*')):
            if path.is_file():
                digest = hashlib.sha256()
                with path.open('rb') as stream:
                    for block in iter(lambda: stream.read(1024*1024), b''):
                        digest.update(block)
                integrity[path.relative_to(destination).as_posix()] = digest.hexdigest()
    (destination / 'forge-browser-integrity.json').write_text(json.dumps(integrity, indent=2), encoding='utf-8')
    print(f'Bundled Chromium {version}: {destination}')


if __name__ == '__main__':
    main()
