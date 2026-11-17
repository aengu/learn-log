import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from ops.render_db_rotation.scheduled_rotation import (
    RotationError,
    _expires_within,
    _load_manifest,
)


def test_expires_within_uses_threshold():
    soon = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    later = (datetime.now(timezone.utc) + timedelta(days=20)).isoformat()

    assert _expires_within({"expiresAt": soon}, 14) is True
    assert _expires_within({"expiresAt": later}, 14) is False


def test_load_manifest_validates_dump_checksum(tmp_path):
    dump_path = tmp_path / "learnlog.dump"
    dump_path.write_bytes(b"valid dump")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "version": 1,
                "source_db_id": "dpg-source",
                "create_settings": {"name": "learn-log"},
                "dump_sha256": hashlib.sha256(b"valid dump").hexdigest(),
            }
        ),
        encoding="utf-8",
    )

    manifest = _load_manifest(manifest_path, dump_path)

    assert manifest["source_db_id"] == "dpg-source"


def test_load_manifest_rejects_changed_dump(tmp_path):
    dump_path = tmp_path / "learnlog.dump"
    dump_path.write_bytes(b"changed dump")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "version": 1,
                "source_db_id": "dpg-source",
                "create_settings": {"name": "learn-log"},
                "dump_sha256": hashlib.sha256(b"other dump").hexdigest(),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RotationError, match="체크섬"):
        _load_manifest(manifest_path, dump_path)
