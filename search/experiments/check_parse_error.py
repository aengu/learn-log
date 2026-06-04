"""
Mistral JSON 파싱 에러 재현 스크립트.

목적: 노트에 적은 "line 5 column 19 근처 Unterminated string" 에러가 실제로 발생하는지,
어떤 형태의 응답에서 깨지는지 직접 확인.

방법: response_format을 빼고 max_tokens=1500으로 _gen_path_trace 프롬프트를 N회 호출.
     PARSE ERROR가 발생하면 정확한 에러 메시지와 raw response를 저장.

실행:
    docker compose exec -T web python manage.py shell < search/experiments/check_parse_error.py

결과:
    컨테이너 안 /tmp/parse_error_check.json
    호스트로 꺼내려면:
    docker compose cp web:/tmp/parse_error_check.json ./parse_error_check.json
"""

import json
import textwrap

from search.models import LearningLog
from search.services import ExerciseService

# ── 설정 ───────────────────────────────────────────────
N = 10            # 시도 횟수 (PARSE ERROR가 잘 발생하려면 5~10회 권장)
MAX_TOKENS = 1500 # 노트에서 PARSE ERROR가 잘 발생하던 값
RESULT_PATH = '/tmp/parse_error_check.json'

TEST_LOG = LearningLog(
    query='파이썬 참조 카운팅의 동작 원리',
    ai_response=(
        '파이썬은 참조 카운팅 메커니즘으로 객체 메모리를 관리합니다. '
        '객체가 생성되면 참조 횟수는 1로 시작합니다. 다른 변수가 같은 객체를 참조하면 횟수가 1 증가합니다. '
        'sys.getrefcount(obj) 함수를 호출하면 임시 참조가 추가되어 결과값이 +1이 됩니다. '
        'del 키워드로 참조를 제거하면 횟수가 감소하고, 0이 되면 메모리에서 해제됩니다.'
    ),
)

# _gen_path_trace와 동일한 프롬프트 (exercise_service.py에서 그대로 복사)
PROMPT = textwrap.dedent(f"""
    아래 학습 내용으로 "경로추적" 연습문제를 만들어주세요.
    실행 흐름을 단계별로 추적하며 객관식으로 답하는 유형입니다. steps는 3~5개.

    질문: {TEST_LOG.query}
    답변: {TEST_LOG.ai_response[:500]}

    JSON으로만 응답 (```없이):
    {{"scenario": "시나리오 설명", "steps": [{{"question": "질문", "choices": ["A","B","C","D"], "correct_index": 0, "correct_answer": "A", "explanation": "설명"}}]}}

    ⚠️ correct_index 규칙 (반드시 준수):
    - choices 배열의 0-based 인덱스 (첫 요소 = 0)
    - choices[correct_index]가 정답 값과 정확히 같아야 함
    - 예: choices=["1","2","3","4"], 정답="2" → correct_index=1 (choices[1]="2")
    - correct_index를 정한 뒤 choices[correct_index]로 검증하세요.

    ⚠️ correct_answer 규칙:
    - 정답의 실제 값(value). choices 배열 중 한 요소와 글자까지 정확히 같아야 함.
    - 항상 choices[correct_index]와 동일한 문자열을 넣으세요. (코드 레벨 검증용 ground truth)
""").strip()


# ── 실행 ───────────────────────────────────────────────
svc = ExerciseService()
results = []

print(f'\n>> response_format 끄고, max_tokens={MAX_TOKENS}로 {N}회 호출...\n', flush=True)

for i in range(N):
    print(f'[{i+1}/{N}] 호출 중...', end=' ', flush=True)

    # response_format 일부러 빼고 호출 (노트 시점 재현)
    try:
        response = svc.mistral_client.chat.complete(
            model=svc.MODEL,
            messages=[{"role": "user", "content": PROMPT}],
            temperature=0.4,
            max_tokens=MAX_TOKENS,
            # response_format 의도적으로 제외
        )
        raw = response.choices[0].message.content
    except Exception as e:
        print(f'API ERROR: {type(e).__name__}: {e}')
        results.append({'iter': i + 1, 'status': 'api_err', 'error_type': type(e).__name__, 'error': str(e)})
        continue

    # JSON 파싱 시도 (exercise_service.py의 _parse_json_response와 동일 로직)
    raw_stripped = raw.strip()
    if raw_stripped.startswith('```'):
        parts = raw_stripped.split('```')
        raw_stripped = parts[1] if len(parts) > 1 else raw_stripped
        if raw_stripped.startswith('json'):
            raw_stripped = raw_stripped[4:]

    try:
        json.loads(raw_stripped.strip())
        print(f'OK ({len(raw)} chars)')
        results.append({
            'iter': i + 1, 'status': 'ok',
            'raw_len': len(raw),
        })
    except json.JSONDecodeError as e:
        err_msg = str(e)
        pos = getattr(e, 'pos', None)
        around = None
        if pos is not None:
            start = max(0, pos - 40)
            end = min(len(raw), pos + 40)
            around = raw[start:end]
        print(f'❌ PARSE ERROR: {err_msg}')
        results.append({
            'iter': i + 1,
            'status': 'parse_err',
            'error_message': err_msg,
            'err_pos': pos,
            'raw_len': len(raw),
            'raw_head': raw[:300],            # 응답 시작 부분 300자
            'raw_around_err': around,          # 에러 위치 전후 80자
        })


# ── 저장 + 요약 ───────────────────────────────────────
with open(RESULT_PATH, 'w') as f:
    json.dump(results, f, ensure_ascii=False, indent=2)

parse_errs = [r for r in results if r['status'] == 'parse_err']
ok = sum(1 for r in results if r['status'] == 'ok')

print(f'\n=== 요약 ===')
print(f'  성공: {ok}/{N}')
print(f'  PARSE ERROR: {len(parse_errs)}/{N}')
print(f'  상세 결과: {RESULT_PATH}')

if parse_errs:
    print(f'\n=== PARSE ERROR 메시지 모음 ===')
    for r in parse_errs:
        print(f'  #{r["iter"]}: {r["error_message"]}')
        print(f'      에러 위치 전후: ...{r.get("raw_around_err", "?")}...')
