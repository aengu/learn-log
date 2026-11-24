"""LearnLog 운영 자동화에 필요한 Render API 클라이언트."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class RenderApiError(RuntimeError):
    """Render API 호출이 실패했을 때 발생한다."""


class RenderApiClient:
    """Render 리소스 조회와 명시적인 갱신 작업을 제공한다."""

    def __init__(self, api_key: str, *, timeout: int = 30) -> None:
        if not api_key:
            raise ValueError("RENDER_API_KEY가 필요합니다.")
        self._api_key = api_key
        self._timeout = timeout
        self._base_url = "https://api.render.com/v1"

    def list_workspaces(self) -> list[dict[str, Any]]:
        return self._list_all("/owners", "owner")

    def get_postgres(self, postgres_id: str) -> dict[str, Any]:
        result = self._request_json("GET", f"/postgres/{postgres_id}")
        if not isinstance(result, dict):
            raise RenderApiError("PostgreSQL 조회 응답 형식이 올바르지 않습니다.")
        return result

    def get_service(self, service_id: str) -> dict[str, Any]:
        result = self._request_json("GET", f"/services/{service_id}")
        if not isinstance(result, dict):
            raise RenderApiError("서비스 조회 응답 형식이 올바르지 않습니다.")
        return result

    def get_postgres_connection_info(self, postgres_id: str) -> dict[str, Any]:
        result = self._request_json("GET", f"/postgres/{postgres_id}/connection-info")
        if not isinstance(result, dict):
            raise RenderApiError("PostgreSQL 접속정보 응답 형식이 올바르지 않습니다.")
        return result

    def create_postgres(self, settings: dict[str, Any]) -> dict[str, Any]:
        result = self._request_json("POST", "/postgres", body=settings)
        if not isinstance(result, dict):
            raise RenderApiError("PostgreSQL 생성 응답 형식이 올바르지 않습니다.")
        return result

    def delete_postgres(self, postgres_id: str) -> None:
        self._request_json("DELETE", f"/postgres/{postgres_id}")

    def suspend_service(self, service_id: str) -> None:
        self._request_json("POST", f"/services/{service_id}/suspend")

    def resume_service(self, service_id: str) -> None:
        self._request_json("POST", f"/services/{service_id}/resume")

    def update_service_env_var(self, service_id: str, key: str, value: str) -> None:
        self._request_json(
            "PUT",
            f"/services/{service_id}/env-vars/{key}",
            body={"value": value},
        )

    def trigger_deploy(self, service_id: str) -> dict[str, Any]:
        result = self._request_json(
            "POST",
            f"/services/{service_id}/deploys",
            body={"clearCache": "do_not_clear"},
        )
        if not isinstance(result, dict):
            raise RenderApiError("배포 생성 응답 형식이 올바르지 않습니다.")
        return result

    def get_deploy(self, service_id: str, deploy_id: str) -> dict[str, Any]:
        result = self._request_json(
            "GET", f"/services/{service_id}/deploys/{deploy_id}"
        )
        if not isinstance(result, dict):
            raise RenderApiError("배포 조회 응답 형식이 올바르지 않습니다.")
        return result

    def list_postgres(
        self, name: str, *, workspace_id: str | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "name": name,
            "includeReplicas": "false",
        }
        if workspace_id:
            params["ownerId"] = workspace_id
        return self._list_all("/postgres", "postgres", params)

    def list_web_services(
        self, name: str, *, workspace_id: str | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "name": name,
            "type": "web_service",
            "includePreviews": "false",
        }
        if workspace_id:
            params["ownerId"] = workspace_id
        return self._list_all("/services", "service", params)

    def _list_all(
        self,
        path: str,
        item_key: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        query = dict(params or {})
        query["limit"] = 100
        resources: list[dict[str, Any]] = []

        while True:
            page = self._request_json("GET", path, params=query)
            if not isinstance(page, list):
                raise RenderApiError("Render API가 예상하지 못한 응답을 반환했습니다.")

            for entry in page:
                resource = entry.get(item_key)
                if isinstance(resource, dict):
                    resources.append(resource)

            cursor = page[-1].get("cursor") if page else None
            if not cursor or len(page) < 100:
                return resources
            query["cursor"] = cursor

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        query = f"?{urlencode(params, doseq=True)}" if params else ""
        url = f"{self._base_url}{path}{query}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            url,
            data=data,
            headers=headers,
            method=method,
        )

        try:
            with urlopen(request, timeout=self._timeout) as response:  # noqa: S310
                payload = response.read()
                if not payload:
                    return None
                return json.loads(payload)
        except HTTPError as error:
            raise RenderApiError(
                f"Render API 요청이 실패했습니다. HTTP {error.code}"
            ) from None
        except URLError as error:
            raise RenderApiError("Render API에 연결할 수 없습니다.") from error
