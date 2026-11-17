from datetime import datetime, timedelta, timezone

import pytest

from ops.render_db_rotation.dry_run import DiscoveryError, _safe_lines, select_targets


WORKSPACE = {"id": "tea-123", "name": "LearnLog"}
DATABASE = {
    "id": "dpg-123",
    "name": "learn-log",
    "owner": {"id": "tea-123"},
}
SERVICE = {
    "id": "srv-123",
    "name": "learn-log",
    "ownerId": "tea-123",
}


def test_select_targets_returns_resources_in_same_workspace():
    result = select_targets(
        [WORKSPACE],
        [DATABASE],
        [SERVICE],
        configured_workspace_id=None,
    )

    assert result == (WORKSPACE, DATABASE, SERVICE)


def test_select_targets_rejects_cross_workspace_resources():
    other_service = {**SERVICE, "ownerId": "tea-other"}

    with pytest.raises(DiscoveryError, match="서로 다른 워크스페이스"):
        select_targets(
            [WORKSPACE],
            [DATABASE],
            [other_service],
            configured_workspace_id=None,
        )


def test_select_targets_rejects_ambiguous_database():
    with pytest.raises(DiscoveryError, match="발견 개수: 2"):
        select_targets(
            [WORKSPACE],
            [DATABASE, {**DATABASE, "id": "dpg-456"}],
            [SERVICE],
            configured_workspace_id=None,
        )


def test_safe_lines_reports_not_due_before_threshold():
    database = {
        **DATABASE,
        "expiresAt": (datetime.now(timezone.utc) + timedelta(days=20)).isoformat(),
    }

    lines = _safe_lines(WORKSPACE, database, SERVICE, threshold_days=14)

    assert any("아직 교체하지 않음" in line for line in lines)


def test_safe_lines_reports_due_inside_threshold():
    database = {
        **DATABASE,
        "expiresAt": (datetime.now(timezone.utc) + timedelta(days=5)).isoformat(),
    }

    lines = _safe_lines(WORKSPACE, database, SERVICE, threshold_days=14)

    assert any("교체 대상" in line for line in lines)
