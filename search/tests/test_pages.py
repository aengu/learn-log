import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db

# ============================================
# 메인 페이지, 리스트 페이지 응답 테스트
# ============================================

class TestMainPage:
    def test_returns_200(self, client):
        """메인 페이지 정상 응답"""
        resp = client.get(reverse("search:main"))
        assert resp.status_code == 200

    def test_uses_correct_template(self, client):
        """메인 페이지가 main.html 템플릿을 사용하는지 확인"""
        resp = client.get(reverse("search:main"))
        assert "search/main.html" in [t.name for t in resp.templates]


class TestLogListPage:
    def test_returns_200(self, client):
        """학습로그 리스트 페이지 정상 응답"""
        resp = client.get(reverse("search:log_list"))
        assert resp.status_code == 200

    def test_uses_correct_template(self, client):
        """리스트 페이지가 list.html 템플릿을 사용하는지 확인"""
        resp = client.get(reverse("search:log_list"))
        assert "search/list.html" in [t.name for t in resp.templates]