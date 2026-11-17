"""Render Free PostgreSQL을 만료 전에 이름 기반으로 교체한다."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .backup_current import BackupError, backup_current_database
from .bootstrap_from_local import (
    CHECK_QUERY,
    POSTGRES_IMAGE,
    BootstrapError,
    _restore_and_count,
    _run,
    _wait_until_available,
    build_create_settings,
)
from .render_api import RenderApiClient, RenderApiError


class RotationError(RuntimeError):
    """DB 회전을 안전하게 계속할 수 없을 때 발생한다."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_manifest(path: Path, dump_path: Path) -> dict[str, Any] | None:
    if not path.is_file() or not dump_path.is_file():
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        raise RotationError("회전 백업 manifest를 읽을 수 없습니다.") from error
    if manifest.get("version") != 1:
        raise RotationError("지원하지 않는 회전 백업 manifest입니다.")
    if manifest.get("dump_sha256") != _sha256(dump_path):
        raise RotationError("회전 백업 dump의 체크섬이 일치하지 않습니다.")
    if not manifest.get("source_db_id") or not manifest.get("create_settings"):
        raise RotationError("회전 백업 manifest에 필수 정보가 없습니다.")
    return manifest


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _remote_counts(client: RenderApiClient, database_id: str) -> list[str]:
    info = client.get_postgres_connection_info(database_id)
    external_url = info.get("externalConnectionString")
    if not external_url:
        raise RotationError("DB External URL을 받지 못했습니다.")
    env = {**os.environ, "DATABASE_URL": external_url, "CHECK_QUERY": CHECK_QUERY}
    output = _run(
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


def _expires_within(database: dict[str, Any], days: int) -> bool:
    value = database.get("expiresAt")
    if not value:
        raise RotationError("Free DB의 expiresAt 값이 없습니다.")
    expires_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    remaining = expires_at - datetime.now(timezone.utc)
    print(f"DB 만료까지 남은 시간: {remaining}")
    return remaining <= timedelta(days=days)


def _wait_for_deploy(
    client: RenderApiClient,
    service_id: str,
    deploy_id: str,
    *,
    timeout_seconds: int = 900,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_status = None
    while time.monotonic() < deadline:
        deploy = client.get_deploy(service_id, deploy_id)
        status = deploy.get("status")
        if status != last_status:
            print(f"웹 배포 상태: {status}")
            last_status = status
        if status == "live":
            return
        if status in {"build_failed", "update_failed", "pre_deploy_failed", "canceled"}:
            raise RotationError(f"웹 배포 실패 상태: {status}")
        time.sleep(10)
    raise RotationError("웹서비스 배포가 제한 시간 안에 완료되지 않았습니다.")


def _check_web(service: dict[str, Any]) -> None:
    details = service.get("serviceDetails")
    url = details.get("url") if isinstance(details, dict) else None
    if not url:
        raise RotationError("웹서비스 URL을 찾지 못했습니다.")
    try:
        with urlopen(  # noqa: S310
            Request(url, headers={"User-Agent": "LearnLog-DB-Rotation"}),
            timeout=120,
        ) as response:
            if response.status >= 500:
                raise RotationError(f"웹서비스 응답 오류: HTTP {response.status}")
    except HTTPError as error:
        if error.code >= 500:
            raise RotationError(f"웹서비스 응답 오류: HTTP {error.code}") from error
    except URLError as error:
        raise RotationError("웹서비스에 연결할 수 없습니다.") from error
    print("웹서비스 HTTP 응답 확인 완료")


def _find_service_and_databases(
    client: RenderApiClient,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    database_name = os.getenv("RENDER_DATABASE_NAME", "learn-log")
    service_name = os.getenv("RENDER_WEB_SERVICE_NAME", "learn-log")
    configured_workspace_id = os.getenv("RENDER_WORKSPACE_ID") or None
    services = client.list_web_services(
        service_name, workspace_id=configured_workspace_id
    )
    if len(services) != 1:
        raise RotationError(
            f"대상 웹서비스를 하나로 확정할 수 없습니다. 발견 개수: {len(services)}"
        )
    service = services[0]
    workspace_id = configured_workspace_id or service.get("ownerId")
    if not workspace_id:
        raise RotationError("대상 워크스페이스 ID를 확인할 수 없습니다.")
    databases = client.list_postgres(database_name, workspace_id=workspace_id)
    if len(databases) > 1:
        raise RotationError("동일한 이름의 DB가 여러 개라서 자동 회전을 중단합니다.")
    return service, databases


def _create_manifest(
    client: RenderApiClient,
    database: dict[str, Any],
    dump_path: Path,
    manifest_path: Path,
) -> dict[str, Any]:
    backup_current_database(dump_path)
    manifest = {
        "version": 1,
        "source_db_id": database["id"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dump_sha256": _sha256(dump_path),
        "row_counts": _remote_counts(client, database["id"]),
        "create_settings": build_create_settings(database),
        "completed_db_id": None,
    }
    _write_manifest(manifest_path, manifest)
    print("회전 직전 백업과 manifest 생성 완료")
    return manifest


def main() -> int:
    rotation_dir = Path(os.getenv("ROTATION_DIR", "rotation_backup"))
    dump_path = rotation_dir / "learnlog.dump"
    manifest_path = rotation_dir / "manifest.json"
    threshold_days = int(os.getenv("ROTATION_THRESHOLD_DAYS", "14"))

    try:
        client = RenderApiClient(os.getenv("RENDER_API_KEY", ""), timeout=60)
        service, databases = _find_service_and_databases(client)
        manifest = _load_manifest(manifest_path, dump_path)
        database = databases[0] if databases else None

        if database and manifest and manifest.get("completed_db_id") == database["id"]:
            dump_path.unlink(missing_ok=True)
            manifest_path.unlink(missing_ok=True)
            manifest = None

        resuming = bool(
            manifest
            and (database is None or manifest.get("source_db_id") != database.get("id"))
        )

        if not resuming:
            if database is None:
                raise RotationError("현재 DB가 없고 복구에 사용할 회전 백업도 없습니다.")
            if database.get("status") != "available":
                raise RotationError(
                    f"현재 DB가 자동 회전 가능한 상태가 아닙니다: {database.get('status')}"
                )
            if not _expires_within(database, threshold_days):
                print("아직 교체 시점이 아니므로 변경 없이 종료합니다.")
                return 0
            manifest = _create_manifest(client, database, dump_path, manifest_path)
            client.delete_postgres(database["id"])
            print(f"기존 DB 삭제 완료: {database['id']}")
            database = None

        if manifest is None:
            raise RotationError("복원에 사용할 회전 백업 manifest가 없습니다.")

        if database is None:
            database = client.create_postgres(manifest["create_settings"])
            print(f"새 DB 생성 요청 완료: {database.get('id', '-')}")
        database_id = database.get("id")
        if not database_id:
            raise RotationError("새 DB ID를 확인할 수 없습니다.")

        if database.get("status") != "available":
            database = _wait_until_available(client, database_id)

        try:
            restored_counts = _remote_counts(client, database_id)
        except BootstrapError:
            restored_counts = []
        expected_counts = manifest.get("row_counts", [])
        if restored_counts != expected_counts:
            info = client.get_postgres_connection_info(database_id)
            external_url = info.get("externalConnectionString")
            if not external_url:
                raise RotationError("새 DB External URL을 받지 못했습니다.")
            restored_counts = _restore_and_count(external_url, dump_path)
        if restored_counts != expected_counts:
            raise RotationError("복원 후 핵심 테이블 행 수가 백업 manifest와 다릅니다.")
        print("새 DB 복원 및 행 수 검증 완료")

        info = client.get_postgres_connection_info(database_id)
        internal_url = info.get("internalConnectionString")
        if not internal_url:
            raise RotationError("새 DB Internal URL을 받지 못했습니다.")
        client.update_service_env_var(service["id"], "DATABASE_URL", internal_url)
        deploy = client.trigger_deploy(service["id"])
        deploy_id = deploy.get("id")
        if not deploy_id:
            raise RotationError("웹서비스 배포 ID를 받지 못했습니다.")
        _wait_for_deploy(client, service["id"], deploy_id)
        _check_web(service)

        manifest["completed_db_id"] = database_id
        manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
        _write_manifest(manifest_path, manifest)
        print(f"Render DB 회전 완료: {database_id}")
    except (
        ValueError,
        RenderApiError,
        BackupError,
        BootstrapError,
        RotationError,
    ) as error:
        print(f"Render DB 회전 실패: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
