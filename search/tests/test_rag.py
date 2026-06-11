"""
RAG (pgvector 하이브리드 검색) 테스트
- RRF 결합 로직 (순수 함수)
- retrieve_similar_logs: FTS/벡터 결합, exclude, 임베딩 실패 시 FTS-only 동작
- 프롬프트 컨텍스트 블록 (500자 절삭)
임베딩 API는 호출하지 않도록 _embed를 모킹한다.
"""
from unittest.mock import patch

import pytest

from search.services import LearnlogService
from search.tests.factories import LearningLogFactory


def _service_without_clients():
    """API 클라이언트 초기화 없이 서비스 인스턴스 생성 (테스트용)"""
    return LearnlogService.__new__(LearnlogService)


class TestRRFMerge:
    def test_양쪽_상위권이_최우선(self):
        merged = LearnlogService._rrf_merge([[1, 2, 3], [2, 3, 4]])
        assert merged[0] == 2  # 1위+2위 조합이 단독 1위(1)보다 높음

    def test_빈_순위_목록(self):
        assert LearnlogService._rrf_merge([[], []]) == []

    def test_한쪽만_있어도_동작(self):
        assert LearnlogService._rrf_merge([[5, 6], []]) == [5, 6]


@pytest.mark.django_db
class TestRetrieveSimilarLogs:
    def test_임베딩_실패시_FTS_결과만으로_동작(self):
        log = LearningLogFactory(query="docker network bridge mode", ai_response="브리지 설명")
        LearningLogFactory(query="파이썬 데코레이터", ai_response="데코레이터 설명")

        service = _service_without_clients()
        with patch.object(LearnlogService, '_embed', return_value=None):
            results = service.retrieve_similar_logs("docker network")

        assert log in results

    def test_벡터_유사도_순으로_조회(self):
        near = LearningLogFactory(query="질문 A", embedding=[1.0] + [0.0] * 1023)
        far = LearningLogFactory(query="질문 B", embedding=[0.0, 1.0] + [0.0] * 1022)

        service = _service_without_clients()
        query_vec = [1.0] + [0.0] * 1023
        with patch.object(LearnlogService, '_embed', return_value=query_vec):
            results = service.retrieve_similar_logs("아무 질문")

        assert results.index(near) < results.index(far)

    def test_exclude_pks_제외(self):
        log = LearningLogFactory(query="docker compose 사용법", ai_response="설명")

        service = _service_without_clients()
        with patch.object(LearnlogService, '_embed', return_value=None):
            results = service.retrieve_similar_logs("docker compose", exclude_pks=[log.pk])

        assert log not in results

    def test_k개_제한(self):
        for i in range(5):
            LearningLogFactory(query=f"docker 질문 {i}", embedding=[1.0] + [0.0] * 1023)

        service = _service_without_clients()
        with patch.object(LearnlogService, '_embed', return_value=[1.0] + [0.0] * 1023):
            results = service.retrieve_similar_logs("docker", k=3)

        assert len(results) == 3


class TestRetrievedContext:
    def test_빈_목록이면_빈_문자열(self):
        assert LearnlogService._build_retrieved_context([]) == ""
        assert LearnlogService._build_retrieved_context(None) == ""

    def test_답변_500자_절삭(self):
        log = LearningLogFactory.build(query="질문", ai_response="가" * 1000)
        block = LearnlogService._build_retrieved_context([log])
        assert "가" * 500 in block
        assert "가" * 501 not in block

    def test_limit_파라미터로_절삭_길이_조절(self):
        log = LearningLogFactory.build(query="질문", ai_response="가" * 2000)
        block = LearnlogService._build_retrieved_context([log], limit=1500)
        assert "가" * 1500 in block
        assert "가" * 1501 not in block
