"""
search_agent 그래프 테스트 — 노드 연결과 분기를 검증한다.
LLM/DB 호출은 전부 모킹: 노드가 올바른 순서·인자로 서비스 메서드를 부르는지가 관심사.
"""
from unittest.mock import Mock

from search.services.search_agent import build_search_agent


def make_service(route=None):
    service = Mock()
    service.retrieve_similar_logs.return_value = ['로그1', '로그2']
    service.decide_route.return_value = route or {'use_logs': True, 'need_web': True, 'reason': ''}
    service.search_official_docs.return_value = {'results': [{'url': 'u', 'content': 'c'}]}
    service.generate_answer_stream.side_effect = lambda *a, **k: iter(['답', '변'])
    return service


def run_agent(service, **state):
    agent = build_search_agent(service)
    return agent.invoke({'query': '테스트 질문입니다', 'custom_instructions': None, 'parent': None, **state})


class TestRouting:
    def test_웹_필요시_웹검색_후_생성(self):
        service = make_service(route={'use_logs': True, 'need_web': True, 'reason': ''})
        result = run_agent(service)
        service.search_official_docs.assert_called_once()
        assert result['answer'] == '답변'

    def test_로그로_충분하면_웹검색_생략(self):
        service = make_service(route={'use_logs': True, 'need_web': False, 'reason': ''})
        result = run_agent(service)
        service.search_official_docs.assert_not_called()
        kwargs = service.generate_answer_stream.call_args.kwargs
        assert kwargs['retrieved_logs'] == ['로그1', '로그2']
        assert kwargs['retrieved_limit'] == 1500  # 웹 생략 → 로그 컨텍스트 예산 확대
        assert result['answer'] == '답변'

    def test_웹_경로는_로그_컨텍스트_500자_유지(self):
        service = make_service(route={'use_logs': True, 'need_web': True, 'reason': ''})
        run_agent(service)
        assert service.generate_answer_stream.call_args.kwargs['retrieved_limit'] == 500

    def test_로그_무관하면_컨텍스트_주입_안함(self):
        service = make_service(route={'use_logs': False, 'need_web': True, 'reason': ''})
        run_agent(service)
        assert service.generate_answer_stream.call_args.kwargs['retrieved_logs'] is None

    def test_꼬리질문_부모는_검색에서_제외(self):
        parent = Mock()
        parent.pk = 7
        service = make_service()
        run_agent(service, parent=parent)
        service.retrieve_similar_logs.assert_called_once_with('테스트 질문입니다', exclude_pks=[7])
