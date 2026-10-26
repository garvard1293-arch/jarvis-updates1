from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import requests


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    if len(sys.argv) != 5:
        return 2
    app_dir = Path(sys.argv[1]).resolve()
    download_url = sys.argv[2]
    expected_hash = sys.argv[3].lower().strip()
    old_pid = int(sys.argv[4])
    time.sleep(2.5)
    # The main process has already closed. The delay also helps Windows release files.
    with tempfile.TemporaryDirectory(prefix="jarvis-update-") as temp_name:
        temp_dir = Path(temp_name)
        archive = temp_dir / "update.zip"
        response = requests.get(download_url, timeout=120)
        response.raise_for_status()
        archive.write_bytes(response.content)
        if sha256(archive).lower() != expected_hash:
            raise RuntimeError("Контрольная сумма обновления не совпадает.")
        extracted = temp_dir / "extracted"
        with zipfile.ZipFile(archive) as package:
            package.extractall(extracted)
        roots = [item for item in extracted.iterdir() if item.is_dir()]
        source = roots[0] if len(roots) == 1 else extracted
        backup = app_dir.with_name(app_dir.name + ".backup")
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        shutil.copytree(app_dir, backup)
        shutil.copytree(source, app_dir, dirs_exist_ok=True)
        subprocess.Popen([sys.executable, str(app_dir / "jarvis_app.py")], cwd=str(app_dir))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log = Path.home() / "JarvisAssistant-update-error.txt"
        log.write_text(str(exc), encoding="utf-8")
        raise
