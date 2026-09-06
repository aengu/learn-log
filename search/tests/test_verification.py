"""
환각 방어 패키지 테스트
- verify_log: 비동기 모순 검증의 상태 전이 (passed/suspect/미검증)
- save_learning_log: answer_source에 따른 verification 초기값
- search_agent: finish_reason 잘림 플래그
- 배지/경고 템플릿 렌더링
LLM 호출은 전부 모킹한다.
"""
from unittest.mock import Mock, patch

import pytest
from django.urls import reverse

from search.models import LearningLog
from search.services import LearnlogService
from search.services.search_agent import build_search_agent
from search.tests.factories import LearningLogFactory


def _service_without_clients():
    """API 클라이언트 초기화 없이 서비스 인스턴스 생성 (테스트용)"""
    return LearnlogService.__new__(LearnlogService)


@pytest.mark.django_db
class TestVerifyLog:
    def _make_log(self):
        return LearningLogFactory(verification='pending')

    def test_모순_없으면_passed(self):
        log = self._make_log()
        service = _service_without_clients()
        with patch.object(LearnlogService, '_call_groq_json', return_value={'consistent': True, 'note': ''}):
            service.verify_log(log, search_results={'results': [{'url': 'u', 'content': '내용'}]})
        log.refresh_from_db()
        assert log.verification == 'passed'
        assert log.verification_note == ''

    def test_모순_있으면_suspect_와_메모(self):
        log = self._make_log()
        service = _service_without_clients()
        with patch.object(LearnlogService, '_call_groq_json',
                          return_value={'consistent': False, 'note': '버전 표기가 어긋남'}):
            service.verify_log(log, search_results={'results': [{'url': 'u', 'content': '내용'}]})
        log.refresh_from_db()
        assert log.verification == 'suspect'
        assert log.verification_note == '버전 표기가 어긋남'

    def test_judge_실패시_미검증으로(self):
        log = self._make_log()
        service = _service_without_clients()
        with patch.object(LearnlogService, '_call_groq_json', side_effect=ValueError('파싱 실패')):
            service.verify_log(log, search_results={'results': [{'url': 'u', 'content': '내용'}]})
        log.refresh_from_db()
        assert log.verification == ''

    def test_컨텍스트_없으면_judge_호출없이_미검증(self):
        log = self._make_log()
        service = _service_without_clients()
        with patch.object(LearnlogService, '_call_groq_json') as judge:
            service.verify_log(log, retrieved_logs=None, search_results=None)
        judge.assert_not_called()
        log.refresh_from_db()
        assert log.verification == ''


@pytest.mark.django_db
class TestSaveVerificationInit:
    def _save(self, service, answer_source):
        return service.save_learning_log(
            '질문입니다', '답변', '## md', {'results': []}, [],
            answer_source=answer_source,
        )

    @pytest.mark.parametrize('source, expected', [
        ('both', 'pending'),
        ('logs', 'pending'),
        ('web', 'pending'),
        ('none', ''),
        ('', ''),
    ])
    def test_answer_source별_verification_초기값(self, source, expected):
        service = _service_without_clients()
        with patch.object(LearnlogService, '_embed', return_value=None):
            assert self._save(service, source).verification == expected


class TestTruncatedFlag:
    def _make_service(self, finish_reason):
        def stream(*args, meta=None, **kwargs):
            if meta is not None and finish_reason:
                meta['finish_reason'] = finish_reason
            return iter(['답', '변'])

        service = Mock()
        service.retrieve_similar_logs.return_value = []
        service.decide_route.return_value = {'use_logs': False, 'need_web': True, 'reason': ''}
        service.search_official_docs.return_value = {'results': []}
        service.generate_answer_stream.side_effect = stream
        return service

    def test_finish_reason_length면_truncated(self):
        agent = build_search_agent(self._make_service('length'))
        result = agent.invoke({'query': '테스트 질문입니다', 'custom_instructions': None, 'parent': None})
        assert result['truncated'] is True

    def test_정상종료면_truncated_아님(self):
        agent = build_search_agent(self._make_service('stop'))
        result = agent.invoke({'query': '테스트 질문입니다', 'custom_instructions': None, 'parent': None})
        assert result['truncated'] is False


class TestStreamFailure:
    """스트리밍 실패 시 에러 문구가 정상 답변처럼 저장되면 안 된다 (복습 대상이 됨)"""

    def test_스트리밍_실패는_에러문구_yield_대신_예외전파(self):
        service = _service_without_clients()
        service.mistral_client = Mock()
        service.mistral_client.chat.stream.side_effect = RuntimeError('LLM 장애')
        with pytest.raises(RuntimeError):
            list(service.generate_answer_stream('질문입니다', {'results': []}))

    @pytest.mark.django_db
    def test_스트리밍_실패시_저장없이_에러이벤트(self, client):
        with patch('search.api_views.LearnlogService'), \
             patch('search.api_views.build_search_agent') as mock_build:
            mock_build.return_value.stream.side_effect = RuntimeError('LLM 장애')
            resp = client.post(reverse('search:query_api_stream'), {'query': '테스트 질문입니다'})
            body = b''.join(resp.streaming_content).decode()
        assert 'event: error' in body
        assert LearningLog.objects.count() == 0


@pytest.mark.django_db
class TestVerifyThreadArgs:
    """
    SSE 뷰가 검증 스레드에 넘기는 인자 — 검증은 생성에 쓴 컨텍스트·절삭 길이를
    그대로 받아야 한다 (다르면 생성 때 없던 내용으로 판정 → 배지 신뢰도 붕괴).
    """

    def _run(self, client, state):
        """에이전트를 모킹해 state만 흘리고, 검증 스레드 생성 지점을 캡처한다"""
        agent = Mock()
        agent.stream.return_value = iter([('updates', {'generate': {'answer': '답변', **state}})])
        # threading "모듈 속성"만 교체 — threading.Thread를 직접 패치하면 전역이라
        # 뷰의 ThreadPoolExecutor 워커까지 Mock이 되어 데드락
        with patch('search.api_views.LearnlogService') as MockService, \
             patch('search.api_views.build_search_agent', return_value=agent), \
             patch('search.api_views.threading') as mock_threading:
            MockService.return_value.extract_tags.return_value = []
            MockService.return_value.convert_to_markdown.return_value = '## md'
            MockService.return_value.save_learning_log.return_value = LearningLogFactory()
            resp = client.post(reverse('search:query_api_stream'), {'query': '테스트 질문입니다'})
            b''.join(resp.streaming_content)  # 스트림 소진 → 뷰 로직 실행
        return mock_threading.Thread

    def test_both는_로그와_웹_컨텍스트를_생성_절삭길이로(self, client):
        web = {'results': [{'url': 'u', 'content': '내용'}]}
        thread = self._run(client, {'answer_source': 'both', 'retrieved_logs': ['로그'], 'search_results': web})
        _, retrieved, limit, search_results = thread.call_args.kwargs['args']
        assert retrieved == ['로그']
        assert limit == 500
        assert search_results == web

    def test_logs는_웹_없이_확대_절삭길이로(self, client):
        thread = self._run(client, {'answer_source': 'logs', 'retrieved_logs': ['로그']})
        _, retrieved, limit, search_results = thread.call_args.kwargs['args']
        assert retrieved == ['로그']
        assert limit == 1500
        assert search_results is None

    def test_web은_로그_컨텍스트_없이(self, client):
        web = {'results': [{'url': 'u', 'content': '내용'}]}
        thread = self._run(client, {'answer_source': 'web', 'search_results': web})
        _, retrieved, limit, search_results = thread.call_args.kwargs['args']
        assert retrieved is None
        assert limit == 500
        assert search_results == web

    def test_none은_검증_스레드_없음(self, client):
        thread = self._run(client, {'answer_source': 'none'})
        thread.assert_not_called()


@pytest.mark.django_db
class TestBadgeRendering:
    def test_의심_로그는_배지와_연습문제_경고_표시(self, client):
        log = LearningLogFactory(
            answer_source='logs', verification='suspect', verification_note='수치가 어긋남',
        )
        resp = client.get(reverse('search:log_detail_api', args=[log.pk]))
        content = resp.content.decode()
        assert '내 기록 기반' in content
        assert '불일치 의심' in content
        assert '틀린 내용으로 복습하지 않도록' in content

    def test_잘림_배지(self, client):
        log = LearningLogFactory(answer_source='web', is_truncated=True)
        content = client.get(reverse('search:log_detail_api', args=[log.pk])).content.decode()
        assert '답변 잘림' in content

    def test_기능_도입전_로그는_배지_없음(self, client):
        log = LearningLogFactory()  # answer_source=''
        content = client.get(reverse('search:log_detail_api', args=[log.pk])).content.decode()
        assert '기반' not in content
        assert '검증됨' not in content
