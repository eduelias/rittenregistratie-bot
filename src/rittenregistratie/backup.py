"""Backups of everything that is not in git: ``data/`` and ``config/``.

    python -m rittenregistratie.backup create --dir ~/backups/rittenregistratie \\
        --keep-days 30 --copy-to /media/nas/backups/rittenregistratie
    python -m rittenregistratie.backup verify <archive>
    python -m rittenregistratie.backup restore <archive> --to <dir>

An archive is ``rittenregistratie-<UTC timestamp>.tar.gz`` holding ``data/``,
``config/`` and ``MANIFEST.json`` (sha256 and size of every file). ``.env`` is
never included: it is rendered from 1Password. ``verify`` unpacks into a
temporary directory and checks every file against the manifest, every ledger
line and every JSON store; ``restore`` unpacks to a directory you name (point
``RIT_DATA_DIR`` / ``RIT_CONFIG_DIR`` at it, or copy back by hand). Archives
older than ``--keep-days`` are removed from both places, so data deleted under
the retention rules is gone from the backups after that many days too.

Run daily by ``deploy/rittenregistratie-backup.timer``.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

PREFIX = "rittenregistratie-"
SUFFIX = ".tar.gz"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _files(root: Path, name: str) -> List[Path]:
    base = root / name
    if not base.exists():
        return []
    return sorted(p for p in base.rglob("*") if p.is_file() and not p.name.endswith(".tmp"))


def create(data_dir: Path, config_dir: Path, out_dir: Path,
           now: Optional[datetime] = None) -> Path:
    """Write one archive of ``data_dir`` and ``config_dir`` into ``out_dir``."""
    now = now or datetime.now(timezone.utc)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{PREFIX}{now:%Y%m%d-%H%M%S}{SUFFIX}"
    manifest: Dict[str, dict] = {}
    tmp = target.with_suffix(".partial")
    with tarfile.open(tmp, "w:gz") as tar:
        for arc_root, src in (("data", data_dir), ("config", config_dir)):
            for path in _files(src.parent, src.name):
                arcname = f"{arc_root}/{path.relative_to(src).as_posix()}"
                manifest[arcname] = {"sha256": _sha256(path), "size": path.stat().st_size}
                tar.add(path, arcname=arcname, recursive=False)
        blob = json.dumps({"created": now.isoformat(timespec="seconds"), "files": manifest},
                          indent=2, sort_keys=True).encode()
        info = tarfile.TarInfo("MANIFEST.json")
        info.size, info.mtime = len(blob), int(time.time())
        tar.addfile(info, io.BytesIO(blob))
    os.chmod(tmp, 0o600)
    tmp.replace(target)
    return target


def _safe_members(tar: tarfile.TarFile):
    for m in tar.getmembers():
        p = Path(m.name)
        if p.is_absolute() or ".." in p.parts or not (m.isfile() or m.isdir()):
            raise ValueError(f"refusing archive member {m.name!r}")
        yield m


def restore(archive: Path, to: Path) -> Path:
    """Unpack ``archive`` into ``to`` (created; must be empty if it exists)."""
    to.mkdir(parents=True, exist_ok=True)
    if any(to.iterdir()):
        raise ValueError(f"{to} is not empty")
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(to, members=list(_safe_members(tar)))
    return to


def verify(archive: Path) -> dict:
    """Unpack into a temporary directory and check it. Returns a summary;
    raises ValueError on the first problem."""
    with tempfile.TemporaryDirectory() as tmp:
        root = restore(archive, Path(tmp) / "r")
        manifest = json.loads((root / "MANIFEST.json").read_text())["files"]
        for name, meta in manifest.items():
            path = root / name
            if not path.is_file():
                raise ValueError(f"{name} missing")
            if _sha256(path) != meta["sha256"]:
                raise ValueError(f"{name} checksum differs")
        ledgers = sorted((root / "data").glob("raw-ledger-*.jsonl"))
        rows = 0
        for ledger in ledgers:
            for n, line in enumerate(ledger.read_text(encoding="utf-8").splitlines(), 1):
                if line.strip():
                    json.loads(line)
                    rows += 1
        for store in (root / "data").glob("*.json"):
            json.loads(store.read_text(encoding="utf-8") or "{}")
        return {"files": len(manifest), "ledgers": len(ledgers), "ledger_rows": rows,
                "cars_yaml": (root / "config" / "cars.yaml").is_file()}


def prune(directory: Path, keep_days: int, now: Optional[float] = None) -> List[Path]:
    now = now or time.time()
    gone = []
    for path in sorted(directory.glob(f"{PREFIX}*{SUFFIX}")):
        if now - path.stat().st_mtime > keep_days * 86400:
            path.unlink()
            gone.append(path)
    return gone


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Back up data/ and config/.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("--dir", required=True, type=Path)
    c.add_argument("--keep-days", type=int, default=30)
    c.add_argument("--copy-to", type=Path)
    v = sub.add_parser("verify")
    v.add_argument("archive", type=Path)
    r = sub.add_parser("restore")
    r.add_argument("archive", type=Path)
    r.add_argument("--to", required=True, type=Path)
    args = ap.parse_args(argv)

    if args.cmd == "create":
        from .config import get_settings
        s = get_settings()
        archive = create(s.data_dir.resolve(), s.config_dir.resolve(), args.dir.expanduser())
        summary = verify(archive)
        print(f"{archive} ({archive.stat().st_size} bytes): {summary}")
        prune(args.dir.expanduser(), args.keep_days)
        if args.copy_to:
            dest = args.copy_to.expanduser()
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(archive, dest / archive.name)
            verify(dest / archive.name)
            prune(dest, args.keep_days)
            print(f"copied to {dest / archive.name}")
        return 0
    if args.cmd == "verify":
        print(json.dumps(verify(args.archive)))
        return 0
    restore(args.archive, args.to)
    print(f"restored into {args.to}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
