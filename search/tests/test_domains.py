"""도메인 매핑 키워드 매칭 테스트 — substring 오매칭 방지"""
from search.domains import get_domains_for_query


def domains_of(query):
    return get_domains_for_query(query) or []


class TestSubstringFalsePositives:
    def test_django_does_not_match_go(self):
        domains = domains_of("Django 모델 설계 방법")
        assert 'docs.djangoproject.com/en' in domains
        assert 'go.dev/doc' not in domains

    def test_javascript_does_not_match_java(self):
        domains = domains_of("javascript 비동기 처리")
        assert 'developer.mozilla.org' in domains
        assert 'docs.oracle.com/en/java' not in domains

    def test_korean_javascript_does_not_match_java(self):
        domains = domains_of("자바스크립트 비동기 처리")
        assert 'developer.mozilla.org' in domains
        assert 'docs.oracle.com/en/java' not in domains

    def test_mongodb_does_not_match_go(self):
        assert 'go.dev/doc' not in domains_of("mongodb 인덱스 설정")


class TestLegitimateMatches:
    def test_go_standalone(self):
        assert 'go.dev/doc' in domains_of("go 언어의 고루틴이란?")

    def test_go_with_attached_korean(self):
        # 한국어에서 흔한 "go언어" 붙여쓰기도 매칭되어야 한다
        assert 'go.dev/doc' in domains_of("go언어 채널 사용법")

    def test_java_standalone(self):
        assert 'docs.oracle.com/en/java' in domains_of("java 스트림 API 사용법")

    def test_korean_java_standalone(self):
        assert 'docs.oracle.com/en/java' in domains_of("자바 스트림 API")

    def test_korean_with_josa(self):
        # 조사가 붙은 한글 키워드 ("도커를")
        assert any('docker' in d for d in domains_of("도커를 쓸 때 주의점"))

    def test_no_match_returns_none(self):
        assert get_domains_for_query("gRPC란 무엇인가") is None
