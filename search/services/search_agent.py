"""
LangGraph 검색 라우팅 에이전트.

기존 직선 파이프라인(검색 → 웹 → 생성)을 조건 분기 그래프로 전환:

    START → retrieve_logs → router ─ need_web=True  → web_search → generate → END
                                   └ need_web=False → generate (웹검색 생략)

노드는 전부 LearnlogService의 기존 메서드를 감싼 것이고, 이 모듈은 연결만 담당한다.
generate 노드는 get_stream_writer로 토큰을 흘려보내 SSE 스트리밍을 유지한다.

답변 검증(모순 검사·잘림 플래그)은 동기 노드로 두면 재생성 대기가 30초라
저장 후 비동기로 처리한다 — 그래프 밖, 별도 작업 (0611 결정).
"""
from typing import Optional, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import StateGraph, START, END


class SearchState(TypedDict, total=False):
    query: str
    custom_instructions: Optional[str]
    parent: object              # LearningLog | None (꼬리질문)
    retrieved_logs: list        # 하이브리드 검색 결과 (LearningLog 목록)
    use_logs: bool              # 라우터: 로그를 답변 컨텍스트로 쓸지
    need_web: bool              # 라우터: 웹 검색 보강이 필요한지
    route_reason: str
    search_results: dict        # Tavily 결과
    answer: str


def build_search_agent(service):
    """LearnlogService 인스턴스를 노드로 감싼 그래프 반환"""

    def retrieve_logs(state):
        parent = state.get('parent')
        exclude = [parent.pk] if parent else None
        logs = service.retrieve_similar_logs(state['query'], exclude_pks=exclude)
        return {'retrieved_logs': logs}

    def router(state):
        decision = service.decide_route(state['query'], state['retrieved_logs'])
        return {
            'use_logs': decision['use_logs'],
            'need_web': decision['need_web'],
            'route_reason': decision['reason'],
        }

    def web_search(state):
        results = service.search_official_docs(state['query'], parent=state.get('parent'))
        return {'search_results': results}

    def generate(state):
        writer = get_stream_writer()

        retrieved = state['retrieved_logs'] if state.get('use_logs', True) else None
        # 웹 생략 경로는 Tavily 블록이 빠진 토큰 예산을 로그 컨텍스트에 재배분 (0611 벤치마크)
        retrieved_limit = 500 if state.get('need_web', True) else 1500
        chunks = []
        for chunk in service.generate_answer_stream(
            state['query'],
            state.get('search_results', {'results': []}),
            state.get('custom_instructions'),
            parent=state.get('parent'),
            retrieved_logs=retrieved,
            retrieved_limit=retrieved_limit,
        ):
            chunks.append(chunk)
            writer({'token': chunk})
        return {'answer': ''.join(chunks).strip()}

    def after_router(state):
        return 'web_search' if state.get('need_web', True) else 'generate'

    graph = StateGraph(SearchState)
    graph.add_node('retrieve_logs', retrieve_logs)
    graph.add_node('router', router)
    graph.add_node('web_search', web_search)
    graph.add_node('generate', generate)

    graph.add_edge(START, 'retrieve_logs')
    graph.add_edge('retrieve_logs', 'router')
    graph.add_conditional_edges('router', after_router, {'web_search': 'web_search', 'generate': 'generate'})
    graph.add_edge('web_search', 'generate')
    graph.add_edge('generate', END)

    return graph.compile()
