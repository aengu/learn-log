import pytest
from django.urls import reverse
from search.tests.factories import LearningLogFactory

pytestmark = pytest.mark.django_db

# ============================================
# 모달 API 테스트: 조회수, 북마크
# ============================================


def detail_url(pk):
    return reverse("search:log_detail_api", args=[pk])


class TestLogDetailGet:
    def test_returns_html(self, client):
        """상세 모달 HTML 반환 + query 내용 포함 확인"""
        log = LearningLogFactory()
        resp = client.get(detail_url(log.pk))
        assert resp.status_code == 200
        assert "text/html" in resp["Content-Type"]
        assert log.query.encode() in resp.content

    def test_increments_view_count(self, client):
        """상세 조회 시 조회수 1 증가"""
        log = LearningLogFactory(view_count=0)
        client.get(detail_url(log.pk))
        log.refresh_from_db()
        assert log.view_count == 1

    def test_multiple_views_increment(self, client):
        """2번 조회 시 조회수 2 증가"""
        log = LearningLogFactory(view_count=5)
        client.get(detail_url(log.pk))
        client.get(detail_url(log.pk))
        log.refresh_from_db()
        assert log.view_count == 7

    def test_not_found(self, client):
        """존재하지 않는 pk 조회 시 에러 메시지 반환"""
        resp = client.get(detail_url(99999))
        assert "찾을 수 없습니다" in resp.content.decode()


class TestLogDetailPatch:
    def test_bookmark_on(self, api_client):
        """북마크 활성화 - False → True"""
        log = LearningLogFactory(is_bookmarked=False)
        resp = api_client.patch(detail_url(log.pk), {"is_bookmarked": True}, format="json")
        assert resp.status_code == 200
        assert resp.json()["is_bookmarked"] is True
        log.refresh_from_db() # db에서 최신값 다시 읽어옴
        assert log.is_bookmarked is True

    def test_bookmark_off(self, api_client):
        """북마크 해제 - True → False"""
        log = LearningLogFactory(is_bookmarked=True)
        resp = api_client.patch(detail_url(log.pk), {"is_bookmarked": False}, format="json")
        assert resp.status_code == 200
        assert resp.json()["is_bookmarked"] is False
        log.refresh_from_db()
        assert log.is_bookmarked is False

    def test_not_found(self, api_client):
        """존재하지 않는 pk PATCH 시 404 반환"""
        resp = api_client.patch(detail_url(99999), {"is_bookmarked": True}, format="json")
        assert resp.status_code == 404