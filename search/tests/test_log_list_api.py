import pytest
from django.urls import reverse
from search.tests.factories import LearningLogFactory, TagFactory

pytestmark = pytest.mark.django_db

URL = reverse("search:log_list")

# ============================================
# 리스트 페이지 API 테스트: 페이지네이션, 정렬, 검색, 태그, 북마크
# ============================================


class TestLogListAPI:
    def test_empty_list(self, client):
        """로그가 없을 때 200 응답"""
        resp = client.get(URL)
        assert resp.status_code == 200

    def test_returns_html(self, client):
        """HTML 조각 반환 확인"""
        LearningLogFactory()
        resp = client.get(URL)
        assert "text/html" in resp["Content-Type"]

    def test_contains_log_card(self, client):
        """응답에 생성한 로그의 query가 포함되는지 확인"""
        log = LearningLogFactory()
        resp = client.get(URL)
        assert log.query.encode() in resp.content

    def test_pagination_has_next(self, client):
        """13개 생성 시 page 1에서 다음 페이지 트리거 존재"""
        LearningLogFactory.create_batch(13)
        resp = client.get(URL)
        assert b"hx-trigger" in resp.content

    def test_pagination_last_page(self, client):
        """13개 생성 시 page 2에서 다음 페이지 트리거 없음"""
        LearningLogFactory.create_batch(13)
        resp = client.get(URL, {"page": 2})
        assert b"hx-trigger" not in resp.content

    def test_filter_by_tag(self, client):
        """태그 필터 - django 태그가 달린 로그 1개만 조회"""
        tag = TagFactory(name="django", slug="django")
        LearningLogFactory(tags=[tag])
        LearningLogFactory()
        resp = client.get(URL, {"tag": "django"})
        assert len(resp.context['logs'].object_list) == 1

    def test_filter_bookmarked(self, client):
        """북마크 필터 - 북마크된 로그 1개만 조회"""
        LearningLogFactory(is_bookmarked=True)
        LearningLogFactory(is_bookmarked=False)
        resp = client.get(URL, {"bookmarked": "true"})
        assert len(resp.context['logs'].object_list) == 1

    def test_sort_by_views(self, client):
        """조회수순 정렬 - 조회수 높은 로그가 먼저 나오는지 확인"""
        low = LearningLogFactory(view_count=1)
        high = LearningLogFactory(view_count=100)
        resp = client.get(URL, {"sort": "views"})
        content = resp.content.decode()
        assert content.index(high.query) < content.index(low.query)

    def test_sort_oldest(self, client):
        """오래된순 정렬 - 먼저 생성된 로그가 먼저 나오는지 확인"""
        first = LearningLogFactory()
        second = LearningLogFactory()
        resp = client.get(URL, {"sort": "oldest"})
        content = resp.content.decode()
        assert content.index(first.query) < content.index(second.query)

    def test_search(self, client):
        """Full-Text Search - 'Django' 검색 시 관련 로그만 조회"""
        LearningLogFactory(query="django orm optimization", ai_response="django orm 테스트")
        LearningLogFactory(query="nginx upstream", ai_response="nginx 테스트")
        resp = client.get(URL, {"q": "Django"})
        content = resp.content.decode()
        assert "django orm optimization" in content
        assert "nginx upstream" not in content