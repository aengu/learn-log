"""
2단계: _gen_path_trace NEW 프롬프트를 다양한 쿼리로 회귀 검증.

목적:
  - 프롬프트 수정 후 새로운 쿼리에서도 correct_index 오류율이 기준 이내인지 확인.
  - pytest assertion 없음. 리포트에 기준선(15%) 대비 경고만 출력.

실행:
  GROQ_API_KEY=... python -m search.experiments.validate_regression

예산:
  5 쿼리 × 1 프롬프트 × 8회 = 40 calls (Groq 무료 일 한도).
  429 도달 시 즉시 중단하고 지금까지 수집된 결과로 리포트.
"""

import sys

from groq import Groq

from search.experiments._common import (
    QUERIES,
    RateLimitHit,
    build_new_prompt,
    ensure_results_dir,
    print_stats,
    run_batch,
    save_summary,
    timestamp,
)

ITERATIONS_PER_QUERY = 8
WARN_ERROR_RATE = 0.15  # 15%를 넘으면 콘솔에 경고 표시 (assertion 아님)


def main():
    client = Groq()
    results_dir = ensure_results_dir()
    ts = timestamp()
    jsonl_path = results_dir / f"regression_{ts}.jsonl"
    json_path = results_dir / f"regression_{ts}.json"

    total_calls = len(QUERIES) * ITERATIONS_PER_QUERY
    print(f"[regression] 결과 저장: {jsonl_path}")
    print(f"[regression] 쿼리 {len(QUERIES)}개 × {ITERATIONS_PER_QUERY}회 = {total_calls} calls")

    rate_limited = False
    try:
        stats = run_batch(
            client,
            build_new_prompt,
            QUERIES,
            ITERATIONS_PER_QUERY,
            label="NEW",
            jsonl_path=jsonl_path,
        )
    except RateLimitHit as e:
        print(f"\n⚠️ Rate limit 도달: {e}")
        print(f"   지금까지 수집된 결과는 {jsonl_path}에 저장됨.")
        rate_limited = True
        stats = None

    print("\n" + "=" * 70)
    print("  회귀 검증 리포트 (NEW 프롬프트, 다중 쿼리)")
    print("=" * 70)

    if stats is None:
        print("  실행 중단 — 부분 결과는 JSONL 참고")
        return 1

    print_stats(stats)

    print(f"\n{'─' * 70}")
    er = stats["error_rate"]
    print(f"  전체 오류율: {er:.1%}  (기준선: {WARN_ERROR_RATE:.0%})")
    if stats["verified_steps"] == 0:
        print("  ⚠️ verified_steps=0 — 검증 휴리스틱이 아무것도 잡아내지 못함. 샘플 수 부족 또는 휴리스틱 점검 필요.")
    elif er > WARN_ERROR_RATE:
        print(f"  ⚠️ 경고: 오류율이 기준선을 초과. 프롬프트 검토 필요.")
    else:
        print(f"  ✅ 기준선 이내.")

    # 쿼리별 경고
    over_queries = [
        qid for qid, q in stats["by_query"].items()
        if q["verified_steps"] > 0 and (q["error_steps"] / q["verified_steps"]) > WARN_ERROR_RATE
    ]
    if over_queries:
        print(f"  ⚠️ 기준선 초과 쿼리: {', '.join(over_queries)}")
    print("=" * 70)

    save_summary([stats], json_path)
    print(f"\n[regression] 집계 저장: {json_path}")

    return 1 if rate_limited else 0


if __name__ == "__main__":
    sys.exit(main())
