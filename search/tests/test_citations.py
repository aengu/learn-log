"""
인라인 인용 가드(sanitize_citations) 테스트
- 프롬프트에 넣은 참고 개수(최대 WEB_CONTEXT_RESULTS)를 벗어난 인용 번호는 제거
- 코드 블록·인라인 코드 안의 [n](배열 인덱싱 등)은 보존
결정적 코드 검증이라 LLM 모킹이 필요 없다.
"""
from search.services import LearnlogService


def _service():
    """API 클라이언트 초기화 없이 서비스 인스턴스 생성 (테스트용)"""
    return LearnlogService.__new__(LearnlogService)


def _results(n):
    return {'results': [{'url': f'https://doc{i}'} for i in range(n)]}


class TestSanitizeCitations:
    def test_유효한_인용은_유지(self):
        answer = '캐시는 TTL로 만료됩니다 [1]. 기본값은 300초입니다 [3].'
        assert _service().sanitize_citations(answer, _results(3)) == answer

    def test_범위밖_인용은_제거(self):
        out = _service().sanitize_citations('이 옵션은 5.2에 추가됐습니다 [7].', _results(3))
        assert '[7]' not in out
        assert '이 옵션은 5.2에 추가됐습니다' in out

    def test_참고가_없으면_모든_인용_제거(self):
        out = _service().sanitize_citations('근거 없는 인용 [1] 입니다.', {'results': []})
        assert '[1]' not in out

    def test_search_results가_None이어도_동작(self):
        out = _service().sanitize_citations('인용 [2] 포함.', None)
        assert '[2]' not in out

    def test_프롬프트_상한을_넘는_참고는_무효(self):
        # 결과 5건이어도 프롬프트에는 WEB_CONTEXT_RESULTS(3)건만 들어가므로 [4]는 지어낸 인용
        out = _service().sanitize_citations('내용 [3] 그리고 [4].', _results(5))
        assert '[3]' in out
        assert '[4]' not in out

    def test_코드블록_안은_보존(self):
        answer = '결과는 [9]가 아닙니다.\n```python\nprint(arr[9])\n```\n본문 끝 [9].'
        out = _service().sanitize_citations(answer, _results(3))
        assert 'arr[9]' in out          # 코드 펜스 안 보존
        assert out.count('[9]') == 1    # 본문의 두 개는 제거

    def test_인라인코드_안은_보존(self):
        out = _service().sanitize_citations('`choices[8]`를 확인하세요 [8].', _results(3))
        assert '`choices[8]`' in out
        assert out.rstrip().endswith('.')  # 본문 인용은 제거됨

    def test_zero와_두자리는_인용으로_안_봄(self):
        # [0]은 1-based 인용에 없고, [12] 같은 두 자리도 인용 표기가 아님 — 건드리지 않는다
        answer = 'arr[0]과 표의 [12]번 행'
        assert _service().sanitize_citations(answer, _results(3)) == answer
