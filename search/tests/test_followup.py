"""꼬리질문 (LearningLog.parent) 테스트"""
import pytest

from search.services import LearnlogService
from search.services.exercise_service import ExerciseService
from .factories import LearningLogFactory

pytestmark = pytest.mark.django_db


class TestParentModel:
    def test_root_walks_up_chain(self):
        q1 = LearningLogFactory(query="Docker bridge vs host 차이")
        q2 = LearningLogFactory(query="그러면 컨테이너끼리는?", parent=q1)
        q3 = LearningLogFactory(query="그걸 compose로 하려면?", parent=q2)

        assert q3.root == q1
        assert q2.root == q1
        assert q1.root == q1

    def test_follow_ups_reverse_relation(self):
        q1 = LearningLogFactory()
        q2 = LearningLogFactory(parent=q1)
        assert list(q1.follow_ups.all()) == [q2]

    def test_parent_delete_sets_null(self):
        q1 = LearningLogFactory()
        q2 = LearningLogFactory(parent=q1)
        q1.delete()
        q2.refresh_from_db()
        assert q2.parent is None


class TestConversationContext:
    def test_no_parent_returns_empty(self):
        assert LearnlogService._build_conversation_context(None) == ""

    def test_parent_context_truncates_answer_to_500(self):
        parent = LearningLogFactory(query="부모 질문", ai_response="가" * 1000)
        block = LearnlogService._build_conversation_context(parent)
        assert "[이전 질문] 부모 질문" in block
        assert "가" * 500 in block
        assert "가" * 501 not in block


class TestSearchQueryConversion:
    def test_context_queries_included_in_prompt(self, monkeypatch):
        """변환 프롬프트에 루트+직속 부모 질문이 들어가는지 (LLM 호출은 캡처로 대체)"""
        captured = {}

        def fake_create(**kwargs):
            captured['prompt'] = kwargs['messages'][0]['content']
            raise RuntimeError("호출 차단")  # 실패 시 원본 반환 경로 사용

        service = LearnlogService()
        monkeypatch.setattr(service.groq_client.chat.completions, 'create', fake_create)

        result = service._to_search_query(
            "그걸 compose로 하려면?",
            context_queries=["Docker bridge vs host 차이", "그러면 컨테이너끼리는?"],
        )
        assert result == "그걸 compose로 하려면?"  # 실패 시 원본 fallback
        assert "Previous question (context): Docker bridge vs host 차이" in captured['prompt']
        assert "Previous question (context): 그러면 컨테이너끼리는?" in captured['prompt']

    def test_no_context_prompt_unchanged(self, monkeypatch):
        captured = {}

        def fake_create(**kwargs):
            captured['prompt'] = kwargs['messages'][0]['content']
            raise RuntimeError("호출 차단")

        service = LearnlogService()
        monkeypatch.setattr(service.groq_client.chat.completions, 'create', fake_create)
        service._to_search_query("PostgreSQL 격리수준이 뭐야?")
        assert "Previous question" not in captured['prompt']


class TestExerciseParentContext:
    def test_parent_context_in_exercise_prompt(self):
        q1 = LearningLogFactory(query="Django N+1 문제가 뭐야?")
        q2 = LearningLogFactory(query="그거 해결하려면?", parent=q1)

        context = ExerciseService._parent_context(q2)
        assert "이전 질문(맥락): Django N+1 문제가 뭐야?" in context
        assert "단독으로 이해 가능하게" in context

    def test_no_parent_no_context(self):
        log = LearningLogFactory()
        assert ExerciseService._parent_context(log) == ""
