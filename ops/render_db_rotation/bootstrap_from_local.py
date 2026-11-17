"""만료된 Render DB를 새 Free DB로 교체하고 로컬 dump를 복원한다."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .dry_run import DiscoveryError, select_targets
from .render_api import RenderApiClient, RenderApiError


CHECK_QUERY = """
SELECT 'learning_logs=' || COUNT(*) FROM search_learninglog
UNION ALL SELECT 'daily_journals=' || COUNT(*) FROM search_dailyjournal
UNION ALL SELECT 'exercises=' || COUNT(*) FROM search_exercise
UNION ALL SELECT 'exercise_attempts=' || COUNT(*) FROM search_exerciseattempt
UNION ALL SELECT 'mate_attempts=' || COUNT(*) FROM search_mateattempt
UNION ALL SELECT 'concepts=' || COUNT(*) FROM practice_concept
ORDER BY 1;
"""
POSTGRES_IMAGE = "pgvector/pgvector:pg18"


class BootstrapError(RuntimeError):
    """안전한 DB 교체를 완료할 수 없을 때 발생한다."""


def build_create_settings(database: dict[str, Any]) -> dict[str, Any]:
    owner = database.get("owner")
    owner_id = owner.get("id") if isinstance(owner, dict) else None
    required = {
        "name": database.get("name"),
        "databaseName": database.get("databaseName"),
        "databaseUser": database.get("databaseUser"),
        "ownerId": owner_id,
        "plan": "free",
        "region": database.get("region"),
        "version": database.get("version"),
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise BootstrapError(f"DB 생성 설정이 부족합니다: {', '.join(missing)}")

    settings = dict(required)
    if database.get("environmentId"):
        settings["environmentId"] = database["environmentId"]
    if isinstance(database.get("ipAllowList"), list):
        settings["ipAllowList"] = database["ipAllowList"]
    return settings


def _run(command: list[str], *, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        command,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise BootstrapError(f"명령 실행에 실패했습니다: {command[0]}")
    return result.stdout.strip()


def _validate_backup(backup_path: Path) -> None:
    if not backup_path.is_file() or backup_path.stat().st_size == 0:
        raise BootstrapError("검증할 로컬 DB 백업 파일이 없습니다.")
    container_path = "/backup/input.dump"
    _run(
        [
            "docker",
            "run",
            "--rm",
            "--volume",
            f"{backup_path.resolve()}:{container_path}:ro",
            POSTGRES_IMAGE,
            "pg_restore",
            "--list",
            container_path,
        ]
    )


def _local_counts() -> list[str]:
    output = _run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "db",
            "psql",
            "-U",
            "learnlog_user",
            "-d",
            "learnlog",
            "-Atc",
            CHECK_QUERY,
        ]
    )
    return output.splitlines()


def _restore_and_count(connection_url: str, backup_path: Path) -> list[str]:
    env = os.environ.copy()
    env["DATABASE_URL"] = connection_url
    container_path = "/backup/input.dump"
    common = [
        "docker",
        "run",
        "--rm",
        "--env",
        "DATABASE_URL",
        "--env",
        "CHECK_QUERY",
        "--volume",
        f"{backup_path.resolve()}:{container_path}:ro",
        POSTGRES_IMAGE,
    ]
    _run(
        [
            *common,
            "sh",
            "-ceu",
            'pg_restore --clean --if-exists --no-owner --no-privileges --dbname="$DATABASE_URL" /backup/input.dump',
        ],
        env={**env, "CHECK_QUERY": CHECK_QUERY},
    )
    output = _run(
        [*common, "sh", "-ceu", 'psql "$DATABASE_URL" -Atc "$CHECK_QUERY"'],
        env={**env, "CHECK_QUERY": CHECK_QUERY},
    )
    return output.splitlines()


def _wait_until_available(
    client: RenderApiClient, postgres_id: str, *, timeout_seconds: int = 900
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        database = client.get_postgres(postgres_id)
        status = database.get("status")
        print(f"새 DB 상태: {status}")
        if status == "available":
            return database
        if status in {"unavailable", "recovery_failed"}:
            raise BootstrapError(f"새 DB 생성 실패 상태: {status}")
        time.sleep(10)
    raise BootstrapError("새 DB가 제한 시간 안에 available 상태가 되지 않았습니다.")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--confirm-delete-id")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    database_name = os.getenv("RENDER_DATABASE_NAME", "learn-log")
    service_name = os.getenv("RENDER_WEB_SERVICE_NAME", "learn-log")

    try:
        client = RenderApiClient(os.getenv("RENDER_API_KEY", ""), timeout=60)
        workspaces = client.list_workspaces()
        databases = client.list_postgres(database_name)
        services = client.list_web_services(service_name)
        _, old_database, service = select_targets(
            workspaces,
            databases,
            services,
            configured_workspace_id=None,
        )
        if not args.dry_run and old_database.get("id") != args.confirm_delete_id:
            raise BootstrapError("삭제 확인 DB ID가 현재 대상과 일치하지 않습니다.")
        if old_database.get("plan") != "free":
            raise BootstrapError("Free 플랜 DB만 이 명령으로 교체할 수 있습니다.")
        if old_database.get("status") != "suspended":
            raise BootstrapError("이번 부트스트랩은 만료되어 정지된 DB만 교체합니다.")

        settings = build_create_settings(old_database)
        _validate_backup(args.backup)
        expected_counts = _local_counts()
        print("로컬 백업과 핵심 데이터 검증 완료")
        if args.dry_run:
            print(f"삭제 예정 DB: {old_database['id']}")
            print(f"연결 예정 웹서비스: {service['id']}")
            print("dry-run 완료: Render 리소스를 변경하지 않았습니다.")
            return 0

        client.delete_postgres(old_database["id"])
        print("만료된 DB 삭제 완료")

        created = client.create_postgres(settings)
        new_id = created.get("id")
        if not new_id:
            raise BootstrapError("새 DB ID를 받지 못했습니다.")
        print(f"새 DB 생성 요청 완료: {new_id}")
        _wait_until_available(client, new_id)

        connection_info = client.get_postgres_connection_info(new_id)
        external_url = connection_info.get("externalConnectionString")
        internal_url = connection_info.get("internalConnectionString")
        if not external_url or not internal_url:
            raise BootstrapError("새 DB 접속정보를 받지 못했습니다.")

        actual_counts = _restore_and_count(external_url, args.backup)
        if actual_counts != expected_counts:
            raise BootstrapError("복원 후 핵심 테이블 행 수가 로컬과 일치하지 않습니다.")
        print("새 DB 복원 및 행 수 검증 완료")

        client.update_service_env_var(service["id"], "DATABASE_URL", internal_url)
        deploy = client.trigger_deploy(service["id"])
        print(f"웹서비스 DATABASE_URL 갱신 및 배포 요청 완료: {deploy.get('id', '-')}")
    except (ValueError, RenderApiError, DiscoveryError, BootstrapError) as error:
        print(f"부트스트랩 실패: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
