"""Render의 LearnLog 리소스를 변경 없이 확인한다."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .render_api import RenderApiClient, RenderApiError


class DiscoveryError(RuntimeError):
    """대상 리소스를 안전하게 하나로 확정할 수 없을 때 발생한다."""


def _postgres_workspace_id(postgres: dict[str, Any]) -> str | None:
    owner = postgres.get("owner")
    return owner.get("id") if isinstance(owner, dict) else None


def select_targets(
    workspaces: list[dict[str, Any]],
    databases: list[dict[str, Any]],
    services: list[dict[str, Any]],
    *,
    configured_workspace_id: str | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """DB와 웹서비스가 같은 워크스페이스의 유일한 대상인지 검증한다."""
    if configured_workspace_id:
        workspaces = [
            item for item in workspaces if item.get("id") == configured_workspace_id
        ]
        databases = [
            item
            for item in databases
            if _postgres_workspace_id(item) == configured_workspace_id
        ]
        services = [
            item for item in services if item.get("ownerId") == configured_workspace_id
        ]

    if len(databases) != 1:
        raise DiscoveryError(
            f"대상 PostgreSQL을 하나로 확정할 수 없습니다. 발견 개수: {len(databases)}"
        )
    if len(services) != 1:
        raise DiscoveryError(
            f"대상 웹서비스를 하나로 확정할 수 없습니다. 발견 개수: {len(services)}"
        )

    database = databases[0]
    service = services[0]
    database_workspace_id = _postgres_workspace_id(database)
    service_workspace_id = service.get("ownerId")
    if not database_workspace_id or database_workspace_id != service_workspace_id:
        raise DiscoveryError("DB와 웹서비스가 서로 다른 워크스페이스에 있습니다.")

    matching_workspaces = [
        item for item in workspaces if item.get("id") == database_workspace_id
    ]
    if len(matching_workspaces) != 1:
        raise DiscoveryError("대상 워크스페이스를 하나로 확정할 수 없습니다.")

    return matching_workspaces[0], database, service


def _safe_lines(
    workspace: dict[str, Any],
    database: dict[str, Any],
    service: dict[str, Any],
    *,
    threshold_days: int,
) -> list[str]:
    expires_at_value = database.get("expiresAt")
    if not expires_at_value:
        raise DiscoveryError("Free DB의 expiresAt 값이 없습니다.")
    expires_at = datetime.fromisoformat(expires_at_value.replace("Z", "+00:00"))
    remaining = expires_at - datetime.now(timezone.utc)
    rotation_due = remaining <= timedelta(days=threshold_days)

    return [
        "## Render DB 갱신 사전 점검",
        "",
        "이 작업은 조회 전용이며 어떤 리소스도 변경하지 않았습니다.",
        "",
        "| 구분 | 값 |",
        "| --- | --- |",
        f"| 워크스페이스 | {workspace.get('name', '-')} (`{workspace.get('id', '-')}`) |",
        f"| PostgreSQL | {database.get('name', '-')} (`{database.get('id', '-')}`) |",
        f"| DB 플랜 | {database.get('plan', '-')} |",
        f"| DB 상태 | {database.get('status', '-')} |",
        f"| DB 리전 | {database.get('region', '-')} |",
        f"| PostgreSQL 버전 | {database.get('version', '-')} |",
        f"| 만료 예정 | {expires_at_value} |",
        f"| 교체 기준 | 만료 {threshold_days}일 전부터 |",
        f"| 현재 판단 | {'교체 대상' if rotation_due else '아직 교체하지 않음'} |",
        f"| 웹서비스 | {service.get('name', '-')} (`{service.get('id', '-')}`) |",
        f"| 웹서비스 상태 | {service.get('suspended', '-')} |",
        "",
        "접속 문자열과 비밀번호는 조회하거나 출력하지 않았습니다.",
    ]


def _append_github_summary(lines: list[str]) -> None:
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with Path(summary_path).open("a", encoding="utf-8") as summary:
        summary.write("\n".join(lines) + "\n")


def main() -> int:
    api_key = os.getenv("RENDER_API_KEY", "")
    database_name = os.getenv("RENDER_DATABASE_NAME", "learn-log")
    service_name = os.getenv("RENDER_WEB_SERVICE_NAME", "learn-log")
    workspace_id = os.getenv("RENDER_WORKSPACE_ID") or None
    threshold_days = int(os.getenv("ROTATION_THRESHOLD_DAYS", "14"))

    try:
        client = RenderApiClient(api_key)
        workspaces = client.list_workspaces()
        databases = client.list_postgres(database_name, workspace_id=workspace_id)
        services = client.list_web_services(service_name, workspace_id=workspace_id)
        workspace, database, service = select_targets(
            workspaces,
            databases,
            services,
            configured_workspace_id=workspace_id,
        )
    except (ValueError, RenderApiError, DiscoveryError) as error:
        print(f"사전 점검 실패: {error}", file=sys.stderr)
        return 1

    try:
        lines = _safe_lines(
            workspace,
            database,
            service,
            threshold_days=threshold_days,
        )
    except (ValueError, DiscoveryError) as error:
        print(f"사전 점검 실패: {error}", file=sys.stderr)
        return 1
    print("\n".join(lines))
    _append_github_summary(lines)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
