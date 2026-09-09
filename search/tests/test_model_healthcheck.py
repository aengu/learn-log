"""실제 LLM 모델이 살아 있는지 검사한다. 기본 실행에서는 제외된다(-m "not healthcheck").

왜 별도로 두는가:
  다른 테스트는 LLM 호출을 전부 모킹한다. 그래야 코드 로직을 외부 서비스 상태와
  무관하게 결정적으로 검증할 수 있다. 대신 그 대가로 "모델이 아직 살아 있는가"는
  아무도 보지 않는다. 2026-09에 모델 4개가 죽는 동안 CI는 계속 초록이었다.

왜 push마다 돌리지 않는가:
  실제 API를 부르면 레이트 리밋이나 일시적 장애로 CI가 빨개진다. 그게 반복되면
  빨간 CI가 "내 코드가 깨졌다"는 신호가 아니게 된다. 예약 실행(healthcheck.yml)과
  수동 실행으로만 돈다.

무엇을 검사하는가 — 상태코드가 아니라 응답 내용:
  gpt-oss 계열의 실패 형태는 예외가 아니라 200 + 빈 문자열이었다. 추론 토큰을
  먼저 쓰기 때문에 max_tokens가 빠듯하면 본문 없이 정상 응답이 온다.
  예외만 잡는 검사는 이 고장을 그대로 통과시킨다.

모델 이름을 여기 적지 않는 이유:
  서비스 상수에서 import한다. 상수를 바꿨는데 검사가 옛 모델을 계속 보는 상태를
  만들지 않기 위해서다(실험 하네스가 그렇게 어긋난 적이 있다).
"""

import os

import pytest

# CI에서는 키가 없으면 실패해야 한다. 로컬에서는 없어도 그냥 건너뛴다.
# 이게 없으면 시크릿 설정을 빠뜨렸을 때 헬스체크가 조용히 초록으로 통과한다 —
# 조용한 실패를 잡으려고 만든 검사가 조용히 실패하는 셈이 된다.
REQUIRE_KEYS = os.environ.get("HEALTHCHECK_REQUIRE_KEYS") == "1"


def _require(name):
    """실제 키를 돌려준다. 없으면 CI에서는 실패, 로컬에서는 skip."""
    key = os.environ.get(name)
    if key and not key.startswith("dummy"):
        return key
    msg = f"{name}가 없거나 더미값이다"
    if REQUIRE_KEYS:
        pytest.fail(f"{msg}. CI에서는 실제 키가 있어야 한다 (리포지토리 시크릿 확인).")
    pytest.skip(msg)
from groq import Groq
from mistralai.client import Mistral

from search.services.exercise_service import ExerciseService
from search.services.journal_service import JournalService
from search.services.learnlog_service import LearnlogService

pytestmark = pytest.mark.healthcheck

# (모델, 프로덕션에서 이 모델에 주는 가장 빠듯한 max_tokens, 어디서 쓰는지)
# 예산까지 같이 재야 의미가 있다. 넉넉한 예산으로만 검사하면 40토큰짜리 호출이
# 빈 문자열을 받는 상황을 놓친다.
CHAT_MODELS = [
    (LearnlogService.ANSWER_MODEL, 2000, "답변 생성·마크다운 변환"),
    (LearnlogService.LIGHT_MODEL, 40, "검색어 변환(가장 빠듯한 예산)"),
    (ExerciseService.MODEL, 3000, "path_trace 출제"),
    (ExerciseService.LIGHT_MODEL, 120, "풀이 코멘트"),
    (JournalService.LIGHT_MODEL, 150, "학습일지 요약"),
]

# 같은 모델이 여러 곳에 쓰이므로 (모델, 예산) 조합만 중복 제거한다.
UNIQUE_CHECKS = sorted({(m, b) for m, b, _ in CHAT_MODELS})


@pytest.fixture(scope="module")
def groq_client():
    return Groq(api_key=_require("GROQ_API_KEY"))


@pytest.mark.parametrize("model,max_tokens", UNIQUE_CHECKS)
def test_모델이_살아있고_내용을_돌려준다(groq_client, model, max_tokens):
    response = groq_client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "Reply with one short sentence in Korean."}],
        temperature=0.0,
        max_tokens=max_tokens,
    )
    content = (response.choices[0].message.content or "").strip()

    # 여기가 핵심이다. 200을 받아도 본문이 비면 프로덕션에서는 고장이다.
    assert content, (
        f"{model} (max_tokens={max_tokens}): 200을 받았지만 본문이 비어 있다. "
        f"추론 토큰이 예산을 다 썼을 가능성이 크다 "
        f"(completion_tokens={response.usage.completion_tokens})."
    )


# JSON 강제 모드를 쓰는 실제 호출 지점과 그 예산.
# 평문 호출과 토큰 소비 양상이 다를 수 있어 따로 검사한다.
JSON_CHECKS = [
    (LearnlogService.LIGHT_MODEL, 200, "모순 검증 _call_groq_json"),
    (ExerciseService.MODEL, 3000, "path_trace 출제 _call_json"),
]


@pytest.mark.parametrize("model,max_tokens,where", JSON_CHECKS)
def test_json_모드가_파싱가능한_응답을_돌려준다(groq_client, model, max_tokens, where):
    import json

    response = groq_client.chat.completions.create(
        model=model,
        # Groq은 response_format을 쓸 때 프롬프트에 'json'이라는 단어를 요구한다.
        messages=[{"role": "user", "content": 'Reply in JSON: {"ok": true}'}],
        temperature=0.0,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
    )
    content = (response.choices[0].message.content or "").strip()
    assert content, (
        f"{model} ({where}, max_tokens={max_tokens}): JSON 모드에서 본문이 비었다 "
        f"(completion_tokens={response.usage.completion_tokens})."
    )
    json.loads(content)   # 파싱 실패도 프로덕션에서는 고장이다


def test_임베딩_모델이_살아있고_차원이_맞다():
    response = Mistral(api_key=_require("MISTRAL_API_KEY")).embeddings.create(
        model=LearnlogService.EMBED_MODEL, inputs=["healthcheck"]
    )
    vector = response.data[0].embedding

    # 차원이 바뀌면 pgvector 컬럼(1024)과 어긋나 저장이 통째로 깨진다.
    assert len(vector) == 1024, (
        f"{LearnlogService.EMBED_MODEL}: 차원이 {len(vector)}다. "
        f"search/models.py의 VectorField(dimensions=1024)와 어긋난다."
    )
