import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from ops.render_db_rotation.scheduled_rotation import (
    PHASE_PREPARED,
    RotationError,
    _expires_within,
    _load_manifest,
    _safe_replacement,
    _validate_manifest_scope,
)


def _manifest(dump: bytes) -> dict:
    return {
        "version": 2,
        "rotation_id": "rotation-1",
        "workspace_id": "tea-workspace",
        "service_id": "srv-learnlog",
        "source_db_id": "dpg-source",
        "replacement_db_id": None,
        "phase": PHASE_PREPARED,
        "created_at": "2026-11-24T00:00:00+00:00",
        "create_settings": {"name": "learn-log", "ownerId": "tea-workspace"},
        "dump_sha256": hashlib.sha256(dump).hexdigest(),
    }


def test_expires_within_uses_threshold():
    soon = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    later = (datetime.now(timezone.utc) + timedelta(days=20)).isoformat()

    assert _expires_within({"expiresAt": soon}, 14) is True
    assert _expires_within({"expiresAt": later}, 14) is False


def test_load_manifest_validates_dump_checksum(tmp_path):
    dump = b"valid dump"
    dump_path = tmp_path / "learnlog.dump"
    dump_path.write_bytes(dump)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest(dump)), encoding="utf-8")

    manifest = _load_manifest(manifest_path, dump_path)

    assert manifest["source_db_id"] == "dpg-source"


def test_load_manifest_rejects_changed_dump(tmp_path):
    dump_path = tmp_path / "learnlog.dump"
    dump_path.write_bytes(b"changed dump")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest(b"other dump")), encoding="utf-8")

    with pytest.raises(RotationError, match="체크섬"):
        _load_manifest(manifest_path, dump_path)


def test_validate_manifest_scope_rejects_other_service():
    manifest = _manifest(b"dump")

    with pytest.raises(RotationError, match="웹서비스"):
        _validate_manifest_scope(
            manifest,
            {"id": "srv-other", "ownerId": "tea-workspace"},
            "tea-workspace",
        )


def test_safe_replacement_requires_scope_name_and_creation_time():
    manifest = _manifest(b"dump")
    valid = {
        "id": "dpg-new",
        "name": "learn-log",
        "owner": {"id": "tea-workspace"},
        "createdAt": "2026-11-24T00:01:00Z",
    }
    manifest["create_settings"] = {
        "name": "learn-log",
        "databaseName": None,
        "databaseUser": None,
        "region": None,
        "version": None,
        "ownerId": "tea-workspace",
    }

    assert _safe_replacement(valid, manifest, "tea-workspace") is True
    wrong_owner = {**valid, "owner": {"id": "other"}}
    assert _safe_replacement(wrong_owner, manifest, "tea-workspace") is False
