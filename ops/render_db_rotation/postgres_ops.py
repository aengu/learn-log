"""DB 회전에서 공통으로 사용하는 PostgreSQL 작업."""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from .render_api import RenderApiClient


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
MARKER_TABLE = "public._learnlog_rotation_state"


class DatabaseOperationError(RuntimeError):
    """PostgreSQL 작업을 안전하게 완료할 수 없을 때 발생한다."""


def run(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: int = 300,
) -> str:
    result = subprocess.run(
        command,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise DatabaseOperationError(f"명령 실행에 실패했습니다: {command[0]}")
    return result.stdout.strip()


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
        raise DatabaseOperationError(f"DB 생성 설정이 부족합니다: {', '.join(missing)}")

    settings = dict(required)
    if database.get("environmentId"):
        settings["environmentId"] = database["environmentId"]
    if isinstance(database.get("ipAllowList"), list):
        settings["ipAllowList"] = database["ipAllowList"]
    return settings


def restore_and_count(connection_url: str, backup_path: Path) -> list[str]:
    env = {**os.environ, "DATABASE_URL": connection_url, "CHECK_QUERY": CHECK_QUERY}
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
    run(
        [
            *common,
            "sh",
            "-ceu",
            'pg_restore --clean --if-exists --no-owner --no-privileges --dbname="$DATABASE_URL" /backup/input.dump',
        ],
        env=env,
    )
    output = run(
        [*common, "sh", "-ceu", 'psql "$DATABASE_URL" -Atc "$CHECK_QUERY"'],
        env=env,
    )
    return output.splitlines()


def remote_counts(connection_url: str) -> list[str]:
    env = {**os.environ, "DATABASE_URL": connection_url, "CHECK_QUERY": CHECK_QUERY}
    output = run(
        [
            "docker",
            "run",
            "--rm",
            "--env",
            "DATABASE_URL",
            "--env",
            "CHECK_QUERY",
            POSTGRES_IMAGE,
            "sh",
            "-ceu",
            'psql "$DATABASE_URL" -Atc "$CHECK_QUERY"',
        ],
        env=env,
    )
    return output.splitlines()


def counts_from_dump(backup_path: Path) -> list[str]:
    if not backup_path.is_file() or backup_path.stat().st_size == 0:
        raise DatabaseOperationError("행 수를 검증할 dump 파일이 없습니다.")

    container_name = f"learnlog-rotation-check-{uuid.uuid4().hex[:12]}"
    password = uuid.uuid4().hex
    try:
        run(
            [
                "docker",
                "run",
                "--detach",
                "--rm",
                "--name",
                container_name,
                "--env",
                f"POSTGRES_PASSWORD={password}",
                "--env",
                "POSTGRES_DB=learnlog",
                "--volume",
                f"{backup_path.resolve()}:/backup/input.dump:ro",
                POSTGRES_IMAGE,
            ]
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            result = subprocess.run(
                ["docker", "exec", container_name, "pg_isready", "-U", "postgres"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                break
            time.sleep(2)
        else:
            raise DatabaseOperationError("임시 PostgreSQL이 준비되지 않았습니다.")

        run(
            [
                "docker",
                "exec",
                container_name,
                "pg_restore",
                "--no-owner",
                "--no-privileges",
                "--username",
                "postgres",
                "--dbname",
                "learnlog",
                "/backup/input.dump",
            ]
        )
        output = run(
            [
                "docker",
                "exec",
                container_name,
                "psql",
                "--username",
                "postgres",
                "--dbname",
                "learnlog",
                "--tuples-only",
                "--no-align",
                "--command",
                CHECK_QUERY,
            ]
        )
        return output.splitlines()
    finally:
        subprocess.run(
            ["docker", "rm", "--force", container_name],
            capture_output=True,
            text=True,
        )


def read_rotation_marker(connection_url: str) -> str | None:
    env = {**os.environ, "DATABASE_URL": connection_url}
    exists = run(
        [
            "docker",
            "run",
            "--rm",
            "--env",
            "DATABASE_URL",
            POSTGRES_IMAGE,
            "sh",
            "-ceu",
            'psql "$DATABASE_URL" -Atc "SELECT to_regclass(\'public._learnlog_rotation_state\') IS NOT NULL"',
        ],
        env=env,
    )
    if exists != "t":
        return None
    value = run(
        [
            "docker",
            "run",
            "--rm",
            "--env",
            "DATABASE_URL",
            POSTGRES_IMAGE,
            "sh",
            "-ceu",
            f'psql "$DATABASE_URL" -Atc "SELECT rotation_id FROM {MARKER_TABLE} WHERE singleton = TRUE"',
        ],
        env=env,
    )
    return value or None


def write_rotation_marker(connection_url: str, rotation_id: str) -> None:
    env = {
        **os.environ,
        "DATABASE_URL": connection_url,
        "ROTATION_ID": rotation_id,
    }
    sql = f"""
CREATE TABLE IF NOT EXISTS {MARKER_TABLE} (
    singleton boolean PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    rotation_id text NOT NULL,
    restored_at timestamptz NOT NULL DEFAULT NOW()
);
INSERT INTO {MARKER_TABLE} (singleton, rotation_id, restored_at)
VALUES (TRUE, :'rotation_id', NOW())
ON CONFLICT (singleton) DO UPDATE
SET rotation_id = EXCLUDED.rotation_id, restored_at = EXCLUDED.restored_at;
"""
    run(
        [
            "docker",
            "run",
            "--rm",
            "--env",
            "DATABASE_URL",
            "--env",
            "ROTATION_ID",
            "--env",
            "MARKER_SQL",
            POSTGRES_IMAGE,
            "sh",
            "-ceu",
            'psql "$DATABASE_URL" --set=rotation_id="$ROTATION_ID" --command "$MARKER_SQL"',
        ],
        env={**env, "MARKER_SQL": sql},
    )


def wait_until_available(
    client: RenderApiClient, postgres_id: str, *, timeout_seconds: int = 900
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_status = None
    while time.monotonic() < deadline:
        database = client.get_postgres(postgres_id)
        status = database.get("status")
        if status != last_status:
            print(f"새 DB 상태: {status}")
            last_status = status
        if status == "available":
            return database
        if status in {"unavailable", "recovery_failed"}:
            raise DatabaseOperationError(f"새 DB 생성 실패 상태: {status}")
        time.sleep(10)
    raise DatabaseOperationError(
        "새 DB가 제한 시간 안에 available 상태가 되지 않았습니다."
    )
