"""마크다운 변환이 불완전하면 원문 포맷으로 대체하는지 검증한다.

배경: 잘린 응답은 예외가 아니라 정상 200 + finish_reason='length'로 온다.
그대로 반환하면 끝이 끊긴 마크다운이 저장되고, is_truncated는 답변 쪽 값이라
화면에 아무 표시도 남지 않는다. 조용히 내용이 사라지는 경로였다.
(2026-09-11 Codex 적대적 리뷰 지적 — round-01)

LLM 호출은 모킹한다. 모델이 실제로 살아 있는지는 test_model_healthcheck.py가 본다.
"""
from unittest.mock import Mock

import pytest

from search.services import LearnlogService

QUERY = "파이썬 GC"
ANSWER = "참조 카운팅과 세대별 GC를 함께 씁니다. " * 20
SEARCH = {"results": [{"title": "doc", "url": "https://e.com", "content": "내용"}]}


def _service():
    """API 클라이언트 초기화 없이 인스턴스만 만든다."""
    return LearnlogService.__new__(LearnlogService)


def _response(content, finish_reason):
    choice = Mock()
    choice.message.content = content
    choice.finish_reason = finish_reason
    response = Mock()
    response.choices = [choice]
    return response


def _call_with(response):
    service = _service()
    service.groq_client = Mock()
    service.groq_client.chat.completions.create.return_value = response
    return service.convert_to_markdown(QUERY, ANSWER, SEARCH)


def _is_fallback(result):
    """원문 포맷인지 — 답변 전문과 참고 자료 섹션이 살아 있어야 한다."""
    return ANSWER in result and "## 참고 자료" in result


class TestMarkdownFallback:
    def test_정상_응답은_그대로_반환한다(self):
        result = _call_with(_response("## 제목\n\n본문", "stop"))
        assert result == "## 제목\n\n본문"

    def test_잘린_응답은_원문_포맷으로_대체한다(self):
        # finish_reason='length' — 내용은 있지만 끝이 끊긴 상태
        result = _call_with(_response("## 제목\n\n본문이 여기서 끊겨", "length"))
        assert _is_fallback(result), "잘린 마크다운이 그대로 저장되면 안 된다"

    @pytest.mark.parametrize("content", ["", "   ", None])
    def test_빈_응답은_원문_포맷으로_대체한다(self, content):
        result = _call_with(_response(content, "stop"))
        assert _is_fallback(result)

    def test_예외는_원문_포맷으로_대체한다(self):
        service = _service()
        service.groq_client = Mock()
        service.groq_client.chat.completions.create.side_effect = RuntimeError("API 장애")
        assert _is_fallback(service.convert_to_markdown(QUERY, ANSWER, SEARCH))

    def test_예산이_답변_예산보다_크다(self):
        """변환 출력은 답변 전문을 재포맷한 것이라 답변보다 짧아질 수 없다.

        답변이 max_tokens=3000으로 생성되므로 변환 예산이 그 이하면 구조적으로 잘린다.
        """
        service = _service()
        service.groq_client = Mock()
        service.groq_client.chat.completions.create.return_value = _response("ok", "stop")
        service.convert_to_markdown(QUERY, ANSWER, SEARCH)

        sent = service.groq_client.chat.completions.create.call_args.kwargs
        assert sent["max_tokens"] > 3000, (
            f"변환 예산({sent['max_tokens']})이 답변 예산(3000) 이하다. "
            f"긴 답변에서 반드시 잘린다."
        )
