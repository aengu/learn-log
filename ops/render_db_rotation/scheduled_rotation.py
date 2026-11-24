"""Render Free PostgreSQL을 안전하게 준비하고 교체한다."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .backup_current import BackupError, backup_database
from .postgres_ops import (
    DatabaseOperationError,
    build_create_settings,
    counts_from_dump,
    read_rotation_marker,
    remote_counts,
    restore_and_count,
    wait_until_available,
    write_rotation_marker,
)
from .render_api import RenderApiClient, RenderApiError

PHASE_PREPARED = "PREPARED"
PHASE_DB_CREATED = "DB_CREATED"
PHASE_RESTORED = "RESTORED"
PHASE_COMPLETED = "COMPLETED"
MANIFEST_VERSION = 2


class RotationError(RuntimeError):
    """DB 회전을 안전하게 계속할 수 없을 때 발생한다."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_manifest(path: Path, dump_path: Path) -> dict[str, Any] | None:
    if not path.is_file() and not dump_path.is_file():
        return None
    if not path.is_file() or not dump_path.is_file():
        raise RotationError("회전 백업 dump와 manifest가 모두 필요합니다.")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        raise RotationError("회전 백업 manifest를 읽을 수 없습니다.") from error
    if manifest.get("version") != MANIFEST_VERSION:
        raise RotationError("지원하지 않는 회전 백업 manifest입니다.")
    required = {
        "rotation_id",
        "workspace_id",
        "service_id",
        "source_db_id",
        "create_settings",
        "phase",
        "dump_sha256",
    }
    missing = sorted(key for key in required if not manifest.get(key))
    if missing:
        raise RotationError(f"회전 manifest 필수 정보가 없습니다: {', '.join(missing)}")
    if manifest["dump_sha256"] != _sha256(dump_path):
        raise RotationError("회전 백업 dump의 체크섬이 일치하지 않습니다.")
    if manifest["phase"] not in {
        PHASE_PREPARED,
        PHASE_DB_CREATED,
        PHASE_RESTORED,
        PHASE_COMPLETED,
    }:
        raise RotationError("회전 manifest의 phase가 올바르지 않습니다.")
    return manifest


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _owner_id(resource: dict[str, Any]) -> str | None:
    owner = resource.get("owner")
    value = owner.get("id") if isinstance(owner, dict) else resource.get("ownerId")
    return value if isinstance(value, str) else None


def _expires_within(database: dict[str, Any], days: int) -> bool:
    value = database.get("expiresAt")
    if not value:
        raise RotationError("Free DB의 expiresAt 값이 없습니다.")
    expires_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    remaining = expires_at - datetime.now(timezone.utc)
    print(f"DB 만료까지 남은 시간: {remaining}")
    return remaining <= timedelta(days=days)


def _find_service_and_databases(
    client: RenderApiClient,
    *,
    expected_workspace_id: str | None = None,
    expected_service_id: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    database_name = os.getenv("RENDER_DATABASE_NAME", "learn-log")
    service_name = os.getenv("RENDER_WEB_SERVICE_NAME", "learn-log")
    configured_workspace_id = os.getenv("RENDER_WORKSPACE_ID") or None
    if (
        configured_workspace_id
        and expected_workspace_id
        and configured_workspace_id != expected_workspace_id
    ):
        raise RotationError("설정된 워크스페이스가 회전 manifest와 다릅니다.")
    workspace_id = expected_workspace_id or configured_workspace_id
    services = client.list_web_services(service_name, workspace_id=workspace_id)
    if expected_service_id:
        services = [item for item in services if item.get("id") == expected_service_id]
    if len(services) != 1:
        raise RotationError(
            f"대상 웹서비스를 하나로 확정할 수 없습니다. 발견 개수: {len(services)}"
        )
    service = services[0]
    effective_workspace_id = workspace_id or _owner_id(service)
    if not effective_workspace_id:
        raise RotationError("대상 워크스페이스 ID를 확인할 수 없습니다.")
    if _owner_id(service) != effective_workspace_id:
        raise RotationError("웹서비스가 대상 워크스페이스에 속하지 않습니다.")
    databases = client.list_postgres(database_name, workspace_id=effective_workspace_id)
    if len(databases) > 1:
        raise RotationError("동일한 이름의 DB가 여러 개라서 자동 회전을 중단합니다.")
    for database in databases:
        if _owner_id(database) != effective_workspace_id:
            raise RotationError("DB가 대상 웹서비스와 다른 워크스페이스에 있습니다.")
    return service, databases, effective_workspace_id


def _validate_manifest_scope(
    manifest: dict[str, Any], service: dict[str, Any], workspace_id: str
) -> None:
    if manifest["workspace_id"] != workspace_id:
        raise RotationError("Artifact의 워크스페이스가 현재 대상과 다릅니다.")
    if manifest["service_id"] != service.get("id"):
        raise RotationError("Artifact의 웹서비스가 현재 대상과 다릅니다.")
    if manifest["create_settings"].get("ownerId") != workspace_id:
        raise RotationError("Artifact의 DB 생성 워크스페이스가 현재 대상과 다릅니다.")


def _is_suspended(service: dict[str, Any]) -> bool:
    value = service.get("suspended")
    return value is True or value == "suspended"


def _wait_for_service_state(
    client: RenderApiClient,
    service_id: str,
    *,
    suspended: bool,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        service = client.get_service(service_id)
        if _is_suspended(service) is suspended:
            return service
        time.sleep(5)
    state = "suspended" if suspended else "not_suspended"
    raise RotationError(f"웹서비스가 {state} 상태가 되지 않았습니다.")


def _suspend_service(client: RenderApiClient, service: dict[str, Any]) -> None:
    if _is_suspended(service):
        return
    service_id = str(service["id"])
    client.suspend_service(service_id)
    _wait_for_service_state(client, service_id, suspended=True)
    print("웹서비스 쓰기 차단 완료")


def _resume_service(client: RenderApiClient, service: dict[str, Any]) -> None:
    latest = client.get_service(str(service["id"]))
    if not _is_suspended(latest):
        return
    client.resume_service(str(service["id"]))
    _wait_for_service_state(client, str(service["id"]), suspended=False)
    print("웹서비스 재개 완료")


def _set_output(name: str, value: str) -> None:
    output_path = os.getenv("GITHUB_OUTPUT")
    if output_path:
        with Path(output_path).open("a", encoding="utf-8") as output:
            output.write(f"{name}={value}\n")


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


def _prepare(rotation_dir: Path) -> None:
    dump_path = rotation_dir / "learnlog.dump"
    manifest_path = rotation_dir / "manifest.json"
    threshold_days = int(os.getenv("ROTATION_THRESHOLD_DAYS", "14"))
    client = RenderApiClient(os.getenv("RENDER_API_KEY", ""), timeout=60)
    manifest = _load_manifest(manifest_path, dump_path)
    service, databases, workspace_id = _find_service_and_databases(
        client,
        expected_workspace_id=manifest.get("workspace_id") if manifest else None,
        expected_service_id=manifest.get("service_id") if manifest else None,
    )
    database = databases[0] if databases else None

    if manifest:
        _validate_manifest_scope(manifest, service, workspace_id)
        if (
            manifest.get("phase") == PHASE_COMPLETED
            and database
            and manifest.get("completed_db_id") == database.get("id")
        ):
            _resume_service(client, service)
            dump_path.unlink(missing_ok=True)
            manifest_path.unlink(missing_ok=True)
            manifest = None
        else:
            allowed_ids = {
                manifest["source_db_id"],
                manifest.get("replacement_db_id"),
            }
            if database and database.get("id") not in allowed_ids:
                raise RotationError("현재 DB가 회전 manifest의 대상과 다릅니다.")
            _suspend_service(client, service)
            _set_output("rotation_required", "true")
            print(f"중단된 회전을 이어갑니다: {manifest['phase']}")
            return

    if database is None:
        raise RotationError("현재 DB가 없고 복구에 사용할 회전 백업도 없습니다.")
    if database.get("status") != "available":
        raise RotationError(
            f"현재 DB가 자동 회전 가능한 상태가 아닙니다: {database.get('status')}"
        )
    if not _expires_within(database, threshold_days):
        _set_output("rotation_required", "false")
        print("아직 교체 시점이 아니므로 변경 없이 종료합니다.")
        return
    if _is_suspended(service):
        raise RotationError(
            "웹서비스가 이미 중지돼 있어 자동 회전을 시작하지 않습니다."
        )

    _suspend_service(client, service)
    try:
        backup_database(client, database, dump_path)
        manifest = {
            "version": MANIFEST_VERSION,
            "rotation_id": uuid.uuid4().hex,
            "workspace_id": workspace_id,
            "service_id": service["id"],
            "service_name": service.get("name"),
            "database_name": database.get("name"),
            "source_db_id": database["id"],
            "replacement_db_id": None,
            "completed_db_id": None,
            "phase": PHASE_PREPARED,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "dump_sha256": _sha256(dump_path),
            "row_counts": counts_from_dump(dump_path),
            "create_settings": build_create_settings(database),
        }
        _write_manifest(manifest_path, manifest)
    except Exception:
        _resume_service(client, service)
        raise
    _set_output("rotation_required", "true")
    print("삭제 전 백업과 manifest 준비 완료")


def _safe_replacement(
    database: dict[str, Any], manifest: dict[str, Any], workspace_id: str
) -> bool:
    if _owner_id(database) != workspace_id:
        return False
    settings = manifest["create_settings"]
    for key in ("name", "databaseName", "databaseUser", "region", "version"):
        if database.get(key) != settings.get(key):
            return False
    created_at = database.get("createdAt")
    if not isinstance(created_at, str):
        return False
    current_created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    rotation_created = datetime.fromisoformat(
        manifest["created_at"].replace("Z", "+00:00")
    )
    return current_created >= rotation_created


def _execute(rotation_dir: Path) -> None:
    dump_path = rotation_dir / "learnlog.dump"
    manifest_path = rotation_dir / "manifest.json"
    manifest = _load_manifest(manifest_path, dump_path)
    if manifest is None:
        raise RotationError("실행할 회전 백업이 없습니다.")
    client = RenderApiClient(os.getenv("RENDER_API_KEY", ""), timeout=60)
    service, databases, workspace_id = _find_service_and_databases(
        client,
        expected_workspace_id=manifest["workspace_id"],
        expected_service_id=manifest["service_id"],
    )
    _validate_manifest_scope(manifest, service, workspace_id)
    database = databases[0] if databases else None

    if database and database.get("id") == manifest["source_db_id"]:
        _suspend_service(client, service)
        client.delete_postgres(str(database["id"]))
        print(f"기존 DB 삭제 완료: {database['id']}")
        database = None

    if database is None:
        database = client.create_postgres(manifest["create_settings"])
        database_id = database.get("id")
        if not database_id:
            raise RotationError("새 DB ID를 확인할 수 없습니다.")
        manifest["replacement_db_id"] = database_id
        manifest["phase"] = PHASE_DB_CREATED
        _write_manifest(manifest_path, manifest)
        print(f"새 DB 생성 요청 완료: {database_id}")
    else:
        database_id = database.get("id")
        known_id = manifest.get("replacement_db_id")
        if database_id != known_id:
            if known_id or not _safe_replacement(database, manifest, workspace_id):
                raise RotationError(
                    "현재 DB를 안전한 교체 대상으로 확인할 수 없습니다."
                )
            manifest["replacement_db_id"] = database_id
            manifest["phase"] = PHASE_DB_CREATED
            _write_manifest(manifest_path, manifest)

    database_id = str(manifest["replacement_db_id"])
    if database.get("status") != "available":
        database = wait_until_available(client, database_id)
    info = client.get_postgres_connection_info(database_id)
    external_url = info.get("externalConnectionString")
    if not isinstance(external_url, str):
        raise RotationError("새 DB External URL을 받지 못했습니다.")

    marker = read_rotation_marker(external_url)
    if marker != manifest["rotation_id"]:
        _suspend_service(client, service)
        try:
            restored_counts = remote_counts(external_url)
        except DatabaseOperationError:
            restored_counts = []
        if restored_counts == manifest.get("row_counts"):
            print("이미 복원된 행 수를 확인해 dump 재적용을 건너뜁니다.")
        else:
            restored_counts = restore_and_count(external_url, dump_path)
        if restored_counts != manifest.get("row_counts"):
            raise RotationError("복원 후 핵심 테이블 행 수가 백업 manifest와 다릅니다.")
        write_rotation_marker(external_url, manifest["rotation_id"])
        manifest["phase"] = PHASE_RESTORED
        manifest["restored_at"] = datetime.now(timezone.utc).isoformat()
        _write_manifest(manifest_path, manifest)
        print("새 DB 복원 및 회전 표식 기록 완료")
    else:
        manifest["phase"] = PHASE_RESTORED
        _write_manifest(manifest_path, manifest)
        print("이미 복원된 DB이므로 dump 재적용을 건너뜁니다.")

    current = client.get_postgres(database_id)
    if _owner_id(current) != workspace_id or current.get("id") != database_id:
        raise RotationError("환경변수 갱신 전 새 DB 범위 검증에 실패했습니다.")
    info = client.get_postgres_connection_info(database_id)
    internal_url = info.get("internalConnectionString")
    if not isinstance(internal_url, str):
        raise RotationError("새 DB Internal URL을 받지 못했습니다.")
    client.update_service_env_var(str(service["id"]), "DATABASE_URL", internal_url)
    _resume_service(client, service)
    deploy = client.trigger_deploy(str(service["id"]))
    deploy_id = deploy.get("id")
    if not deploy_id:
        raise RotationError("웹서비스 배포 ID를 받지 못했습니다.")
    _wait_for_deploy(client, str(service["id"]), str(deploy_id))
    _check_web(client.get_service(str(service["id"])))

    manifest["phase"] = PHASE_COMPLETED
    manifest["completed_db_id"] = database_id
    manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
    _write_manifest(manifest_path, manifest)
    print(f"Render DB 회전 완료: {database_id}")


def _abort(rotation_dir: Path) -> None:
    dump_path = rotation_dir / "learnlog.dump"
    manifest_path = rotation_dir / "manifest.json"
    manifest = _load_manifest(manifest_path, dump_path)
    client = RenderApiClient(os.getenv("RENDER_API_KEY", ""), timeout=60)
    service, databases, _ = _find_service_and_databases(
        client,
        expected_workspace_id=manifest.get("workspace_id") if manifest else None,
        expected_service_id=manifest.get("service_id") if manifest else None,
    )
    database = databases[0] if databases else None
    if database and (
        manifest is None or database.get("id") == manifest["source_db_id"]
    ):
        _resume_service(client, service)
        print("원본 DB가 남아 있어 웹서비스를 안전하게 재개했습니다.")
    else:
        print("원본 DB가 없으므로 자동 resume을 수행하지 않습니다.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("prepare", "execute", "abort"))
    args = parser.parse_args(argv)
    rotation_dir = Path(os.getenv("ROTATION_DIR", "rotation_backup"))
    try:
        if args.mode == "prepare":
            _prepare(rotation_dir)
        elif args.mode == "execute":
            _execute(rotation_dir)
        else:
            _abort(rotation_dir)
    except (
        ValueError,
        RenderApiError,
        BackupError,
        DatabaseOperationError,
        RotationError,
    ) as error:
        print(f"Render DB 회전 실패: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
