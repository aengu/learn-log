"""현재 Render DB를 connection string 노출 없이 백업한다."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .dry_run import DiscoveryError, select_targets
from .render_api import RenderApiClient, RenderApiError


POSTGRES_IMAGE = "pgvector/pgvector:pg18"


class BackupError(RuntimeError):
    """안전한 Render DB 백업을 만들 수 없을 때 발생한다."""


def _run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    result = subprocess.run(
        command,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        raise BackupError(f"명령 실행에 실패했습니다: {command[0]}")


def backup_database(
    client: RenderApiClient, database: dict[str, object], output_path: Path
) -> None:
    if database.get("status") != "available":
        raise BackupError(f"DB가 백업 가능한 상태가 아닙니다: {database.get('status')}")

    database_id = database.get("id")
    if not isinstance(database_id, str):
        raise BackupError("DB ID를 확인할 수 없습니다.")
    connection_info = client.get_postgres_connection_info(database_id)
    external_url = connection_info.get("externalConnectionString")
    if not external_url:
        raise BackupError("Render에서 External Database URL을 받지 못했습니다.")

    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "DATABASE_URL": external_url,
        "BACKUP_FILENAME": output_path.name,
    }
    _run(
        [
            "docker",
            "run",
            "--rm",
            "--env",
            "DATABASE_URL",
            "--env",
            "BACKUP_FILENAME",
            "--volume",
            f"{output_path.parent}:/backup",
            POSTGRES_IMAGE,
            "sh",
            "-ceu",
            'pg_dump --no-owner --no-privileges --format=custom --file="/backup/$BACKUP_FILENAME" "$DATABASE_URL"',
        ],
        env=env,
    )
    _run(
        [
            "docker",
            "run",
            "--rm",
            "--volume",
            f"{output_path}:/backup/input.dump:ro",
            POSTGRES_IMAGE,
            "pg_restore",
            "--list",
            "/backup/input.dump",
        ]
    )
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise BackupError("백업 파일이 생성되지 않았습니다.")


def backup_current_database(output_path: Path) -> None:
    client = RenderApiClient(os.getenv("RENDER_API_KEY", ""), timeout=60)
    database_name = os.getenv("RENDER_DATABASE_NAME", "learn-log")
    service_name = os.getenv("RENDER_WEB_SERVICE_NAME", "learn-log")
    workspace_id = os.getenv("RENDER_WORKSPACE_ID") or None

    services = client.list_web_services(service_name, workspace_id=workspace_id)
    if len(services) != 1:
        raise DiscoveryError(
            f"대상 웹서비스를 하나로 확정할 수 없습니다. 발견 개수: {len(services)}"
        )
    owner = services[0].get("owner")
    service_owner_id = (
        owner.get("id") if isinstance(owner, dict) else services[0].get("ownerId")
    )
    effective_workspace_id = workspace_id or service_owner_id
    if not isinstance(effective_workspace_id, str):
        raise DiscoveryError("웹서비스의 워크스페이스를 확인할 수 없습니다.")

    workspaces = client.list_workspaces()
    databases = client.list_postgres(database_name, workspace_id=effective_workspace_id)
    _, database, _ = select_targets(
        workspaces,
        databases,
        services,
        configured_workspace_id=effective_workspace_id,
    )
    backup_database(client, database, output_path)


def main() -> int:
    output = Path(os.getenv("BACKUP_OUTPUT", "learnlog_backup.dump"))
    try:
        backup_current_database(output)
    except (ValueError, RenderApiError, DiscoveryError, BackupError) as error:
        print(f"Render DB 백업 실패: {error}", file=sys.stderr)
        return 1
    print(f"Render DB 백업 및 검증 완료: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
