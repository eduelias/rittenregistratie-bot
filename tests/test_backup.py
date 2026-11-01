"""Backups: everything outside git, verified, restorable, pruned."""
import json
import os
import tarfile
import time

import pytest

from rittenregistratie import backup


@pytest.fixture
def tree(tmp_path):
    data, cfg = tmp_path / "app" / "data", tmp_path / "app" / "config"
    (data / "exports").mkdir(parents=True)
    cfg.mkdir()
    (data / "raw-ledger-golf.jsonl").write_text('{"end_odo": 1}\n{"end_odo": 2}\n')
    (data / "signups.json").write_text('{"a": {}}')
    (data / "exports" / "trips-golf-2026.xlsx").write_bytes(b"xlsx")
    (data / "half-written.tmp").write_text("x")
    (cfg / "cars.yaml").write_text("golf: {phones: ['316']}\n")
    (tmp_path / "app" / ".env").write_text("RIT_WHATSAPP_TOKEN=secret\n")
    return data, cfg, tmp_path


def test_create_verify_restore(tree):
    data, cfg, tmp = tree
    archive = backup.create(data, cfg, tmp / "out")
    assert oct(archive.stat().st_mode & 0o777) == "0o600"
    with tarfile.open(archive) as tar:
        names = set(tar.getnames())
    assert {"data/raw-ledger-golf.jsonl", "data/signups.json", "config/cars.yaml",
            "data/exports/trips-golf-2026.xlsx", "MANIFEST.json"} <= names
    assert not any(".env" in n or n.endswith(".tmp") for n in names)
    assert backup.verify(archive) == {"files": 4, "ledgers": 1, "ledger_rows": 2, "cars_yaml": True}
    to = backup.restore(archive, tmp / "restored")
    assert (to / "data" / "raw-ledger-golf.jsonl").read_text() == (data / "raw-ledger-golf.jsonl").read_text()
    with pytest.raises(ValueError):
        backup.restore(archive, tmp / "restored")        # never over existing files


def test_verify_catches_a_damaged_archive(tree):
    data, cfg, tmp = tree
    archive = backup.create(data, cfg, tmp / "out")
    root = backup.restore(archive, tmp / "r")
    (root / "data" / "signups.json").write_text('{"tampered": true}')
    bad = tmp / "bad.tar.gz"
    with tarfile.open(bad, "w:gz") as tar:
        for p in root.rglob("*"):
            if p.is_file():
                tar.add(p, arcname=p.relative_to(root).as_posix())
    with pytest.raises(ValueError, match="checksum"):
        backup.verify(bad)


def test_prune_keeps_recent_and_ignores_other_files(tmp_path):
    old = tmp_path / "rittenregistratie-20250101-000000.tar.gz"
    new = tmp_path / "rittenregistratie-20261101-000000.tar.gz"
    other = tmp_path / "pauta-20250101.tar.gz"
    for p in (old, new, other):
        p.write_bytes(b"x")
    long_ago = time.time() - 40 * 86400
    os.utime(old, (long_ago, long_ago))
    os.utime(other, (long_ago, long_ago))
    assert backup.prune(tmp_path, 30) == [old]
    assert new.exists() and other.exists()


def test_cli_create_copies_and_verifies(tree, monkeypatch, capsys):
    data, cfg, tmp = tree
    import rittenregistratie.config as config
    from rittenregistratie.config import Settings
    monkeypatch.setattr(config, "get_settings", lambda: Settings(data_dir=data, config_dir=cfg))
    assert backup.main(["create", "--dir", str(tmp / "local"), "--copy-to", str(tmp / "nas")]) == 0
    (copy,) = list((tmp / "nas").glob("rittenregistratie-*.tar.gz"))
    assert backup.verify(copy)["ledger_rows"] == 2
    assert "copied to" in capsys.readouterr().out
