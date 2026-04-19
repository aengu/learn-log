"""
v2 프롬프트 검증: python_gc × v2 × 20회 (=20 calls).

목적:
  - v1 (baseline_20260417_150110.jsonl) 대비 v2에서 shifted choices 버그가 줄었는지 확인.
  - v1은 이미 저장되어 있으므로 재실행하지 않고 v2만 동일 조건(쿼리/반복수)으로 추가.

가설:
  - v2는 correct_index 규칙 예시를 shifted choices (['1','2','3','4'])로 교체.
  - v1에서 확인된 '정답 값 N과 choices의 N번째 위치 혼동'을 정면으로 짚는 예시 + self-check.

실행:
  GROQ_API_KEY=... python -m search.experiments.validate_v2
  이후 python -m search.experiments.reanalyze <v2_jsonl_path> 로 재분석.

예산:
  1 쿼리 × 1 프롬프트(v2) × 20회 = 20 calls.
  일일 예비로 20 calls 남음 (후속 regression 용).
"""

import sys

from groq import Groq

from search.experiments._common import (
    RateLimitHit,
    build_new_prompt,
    ensure_results_dir,
    get_query,
    print_stats,
    run_batch,
    save_summary,
    timestamp,
)

QUERY_ID = "python_gc"
ITERATIONS = 20


def main():
    client = Groq()
    results_dir = ensure_results_dir()
    ts = timestamp()
    jsonl_path = results_dir / f"v2_{ts}.jsonl"
    json_path = results_dir / f"v2_{ts}.json"

    query = get_query(QUERY_ID)
    rate_limited = False

    print(f"[v2] 결과 저장: {jsonl_path}")
    print(f"[v2] 쿼리: {query['id']}, 반복: {ITERATIONS}회 × v2 프롬프트 = {ITERATIONS} calls")

    try:
        stats = run_batch(
            client,
            build_new_prompt,  # v2 = 현재 build_new_prompt
            [query],
            ITERATIONS,
            label="V2",
            jsonl_path=jsonl_path,
        )
    except RateLimitHit as e:
        print(f"\n⚠️ Rate limit 도달: {e}")
        print(f"   지금까지 수집된 결과는 {jsonl_path}에 저장됨.")
        return 1

    print("\n" + "=" * 70)
    print("  v2 실험 리포트")
    print("=" * 70)
    print_stats(stats)
    print("=" * 70)

    save_summary([stats], json_path)
    print(f"\n[v2] 집계 저장: {json_path}")
    print("\n다음 단계:")
    print(f"  docker exec learnlog_web python -m search.experiments.reanalyze {jsonl_path}")
    print("  → v2 재분석 결과를 v1(baseline_20260417_150110.jsonl)과 비교")

    return 1 if rate_limited else 0


if __name__ == "__main__":
    sys.exit(main())
