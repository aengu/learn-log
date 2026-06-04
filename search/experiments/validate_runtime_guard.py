"""
path_trace 런타임 가드 환각/실패율 실측 스크립트.

실행 방법 (프로젝트 루트에서):
    docker compose exec -T web python manage.py shell < search/experiments/validate_runtime_guard.py

표본 크기를 바꾸려면 아래 N 값 수정.
결과는 컨테이너 안 /tmp/runtime_guard_result.json 에 저장됩니다.
컨테이너에서 꺼내려면:
    docker compose cp web:/tmp/runtime_guard_result.json ./runtime_guard_result.json
"""

import json
import time

from search.models import LearningLog
from search.services import ExerciseService

# ── 설정 ───────────────────────────────────────────────
N = 20  # 표본 크기. 빠르게 보고 싶으면 5~10으로.

# 환각이 잘 발생하는 stress case (참조 카운팅, 숫자형 선택지가 잘 나오는 주제)
TEST_LOG = LearningLog(
    query='파이썬 참조 카운팅의 동작 원리',
    ai_response=(
        '파이썬은 참조 카운팅 메커니즘으로 객체 메모리를 관리합니다. '
        '객체가 생성되면 참조 횟수는 1로 시작합니다. 다른 변수가 같은 객체를 참조하면 횟수가 1 증가합니다. '
        'sys.getrefcount(obj) 함수를 호출하면 임시 참조가 추가되어 결과값이 +1이 됩니다. '
        'del 키워드로 참조를 제거하면 횟수가 감소하고, 0이 되면 메모리에서 해제됩니다.'
    ),
)

RESULT_PATH = '/tmp/runtime_guard_result.json'


# ── 검증 로직 (exercise_service.py와 동일한 규칙) ──────
def step_valid(step):
    ci = step.get('correct_index')
    ca = step.get('correct_answer')
    cs = step.get('choices', [])
    return (
        isinstance(ci, int)
        and 0 <= ci < len(cs)
        and ca is not None
        and cs[ci] == ca
    )


# ── _call_mistral_json 트래킹으로 raw 응답 추적 ─────────
svc = ExerciseService()
original_call = svc._call_mistral_json
call_log = []


def tracking_call(prompt):
    try:
        result = original_call(prompt)
        call_log.append(('ok', result))
        return result
    except Exception as e:
        call_log.append(('parse_err', str(e)))
        raise


svc._call_mistral_json = tracking_call


# ── 측정 루프 ──────────────────────────────────────────
results = []
start_t = time.time()

print(f'>> {N}회 측정 시작...\n', flush=True)
for i in range(N):
    call_log.clear()
    iter_start = time.time()
    try:
        out = svc._gen_path_trace(TEST_LOG)
        outcome = 'success'
        final_steps = len(out.get('steps', []))
    except Exception:
        outcome = 'failure'
        final_steps = 0

    call_records = []
    for status, payload in call_log:
        if status == 'parse_err':
            call_records.append({'status': 'parse_err'})
        else:
            steps = payload.get('steps', [])
            valid = sum(1 for s in steps if step_valid(s))
            call_records.append({
                'status': 'ok',
                'raw': len(steps),
                'valid': valid,
                'hallucinated': len(steps) - valid,
            })

    iter_record = {
        'iter': i + 1,
        'outcome': outcome,
        'final_steps': final_steps,
        'calls': call_records,
        'duration_sec': round(time.time() - iter_start, 1),
    }
    results.append(iter_record)
    print(
        f'[{i+1}/{N}] {outcome} {final_steps}step '
        f'(호출 {len(call_log)}회, {iter_record["duration_sec"]}s)',
        flush=True,
    )


# ── 집계 ───────────────────────────────────────────────
total_duration = round(time.time() - start_t, 1)
total_calls = sum(len(r['calls']) for r in results)
total_raw_steps = sum(c.get('raw', 0) for r in results for c in r['calls'])
total_hallucinated = sum(c.get('hallucinated', 0) for r in results for c in r['calls'])
parse_errors = sum(1 for r in results for c in r['calls'] if c.get('status') == 'parse_err')
retries = sum(1 for r in results if len(r['calls']) > 1)
successes = sum(1 for r in results if r['outcome'] == 'success')

summary = {
    'N': N,
    'duration_sec': total_duration,
    'successes': successes,
    'failures': N - successes,
    'success_rate_pct': round(successes / N * 100, 1),
    'total_llm_calls': total_calls,
    'avg_calls_per_iter': round(total_calls / N, 2),
    'parse_errors': parse_errors,
    'retries_triggered': retries,
    'retry_rate_pct': round(retries / N * 100, 1),
    'total_raw_steps': total_raw_steps,
    'total_hallucinated_steps': total_hallucinated,
    'hallucination_rate_pct': (
        round(total_hallucinated / total_raw_steps * 100, 2)
        if total_raw_steps
        else None
    ),
}

with open(RESULT_PATH, 'w') as f:
    json.dump({'summary': summary, 'iterations': results}, f, ensure_ascii=False, indent=2)


# ── 출력 ───────────────────────────────────────────────
print(f'\n=== 요약 ({total_duration}초, N={N}) ===')
for k, v in summary.items():
    print(f'  {k}: {v}')
print(f'\n상세 결과: {RESULT_PATH}')
