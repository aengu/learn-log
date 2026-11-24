"""가장 최근의 회전 백업 Artifact를 다음 실행에서 사용할 수 있게 받는다."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ARTIFACT_PREFIX = "render-rotation-backup-"


class DownloadError(RuntimeError):
    """회전 백업 Artifact를 안전하게 받을 수 없을 때 발생한다."""


def _github_json(url: str, token: str) -> dict:
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310
            return json.load(response)
    except (HTTPError, URLError) as error:
        raise DownloadError("GitHub Artifact 목록을 조회하지 못했습니다.") from error


def _download(url: str, token: str, destination: Path) -> None:
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(request, timeout=60) as response:  # noqa: S310
            with destination.open("wb") as output:
                shutil.copyfileobj(response, output)
    except (HTTPError, URLError) as error:
        raise DownloadError("GitHub Artifact를 다운로드하지 못했습니다.") from error


def download_latest(output_dir: Path) -> bool:
    token = os.getenv("GITHUB_TOKEN", "")
    repository = os.getenv("GITHUB_REPOSITORY", "")
    if not token or not repository:
        raise DownloadError("GITHUB_TOKEN과 GITHUB_REPOSITORY가 필요합니다.")

    artifacts = []
    page = 1
    while not artifacts:
        query = urlencode({"per_page": 100, "page": page})
        data = _github_json(
            f"https://api.github.com/repos/{repository}/actions/artifacts?{query}",
            token,
        )
        entries = data.get("artifacts", [])
        artifacts = [
            item
            for item in entries
            if not item.get("expired")
            and str(item.get("name", "")).startswith(ARTIFACT_PREFIX)
        ]
        if len(entries) < 100:
            break
        page += 1
    if not artifacts:
        print("이전 회전 백업 Artifact가 없습니다.")
        return False

    latest = max(artifacts, key=lambda item: int(item.get("id", 0)))
    output_dir.mkdir(parents=True, exist_ok=True)
    for filename in ("learnlog.dump", "manifest.json"):
        (output_dir / filename).unlink(missing_ok=True)
    with tempfile.TemporaryDirectory() as temp_dir:
        archive = Path(temp_dir) / "artifact.zip"
        _download(latest["archive_download_url"], token, archive)
        with zipfile.ZipFile(archive) as zipped:
            for member in zipped.infolist():
                target = (output_dir / member.filename).resolve()
                if not target.is_relative_to(output_dir.resolve()):
                    raise DownloadError(
                        "안전하지 않은 Artifact 경로가 포함돼 있습니다."
                    )
            zipped.extractall(output_dir)
    print(f"이전 회전 백업 Artifact 다운로드 완료: {latest['id']}")
    return True


def main() -> int:
    try:
        download_latest(Path(os.getenv("ROTATION_DIR", "rotation_backup")))
    except DownloadError as error:
        print(f"회전 백업 다운로드 실패: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
