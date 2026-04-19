"""
1단계: _gen_path_trace 프롬프트의 OLD vs NEW 베이스라인 비교 (일회성).

목적:
  - correct_index 규칙 추가로 실제 오류율이 얼마나 줄었는지 기록.
  - 이 결과를 results/에 남긴 뒤 OLD 프롬프트는 영구 폐기.

실행:
  GROQ_API_KEY=... python -m search.experiments.validate_baseline

예산:
  1 쿼리 × 2 프롬프트 × 20회 = 40 calls (Groq 무료 일 한도 거의 전부 소비).
  429 도달 시 즉시 중단하고 지금까지 수집된 결과로 리포트 작성.
"""

import sys
from pathlib import Path

from groq import Groq

from search.experiments._common import (
    RateLimitHit,
    build_new_prompt,
    build_old_prompt,
    ensure_results_dir,
    get_query,
    print_stats,
    run_batch,
    save_summary,
    timestamp,
)

BASELINE_QUERY_ID = "python_gc"
ITERATIONS = 20


def main():
    client = Groq()
    results_dir = ensure_results_dir()
    ts = timestamp()
    jsonl_path = results_dir / f"baseline_{ts}.jsonl"
    json_path = results_dir / f"baseline_{ts}.json"

    query = get_query(BASELINE_QUERY_ID)
    stats_list = []
    rate_limited = False

    print(f"[baseline] 결과 저장: {jsonl_path}")
    print(f"[baseline] 쿼리: {query['id']}, 반복: {ITERATIONS}회 × 2 프롬프트 = 40 calls")

    for label, builder in [("OLD", build_old_prompt), ("NEW", build_new_prompt)]:
        print(f"\n=== {label} 프롬프트 ===")
        try:
            stats = run_batch(client, builder, [query], ITERATIONS, label, jsonl_path)
        except RateLimitHit as e:
            print(f"\n⚠️ Rate limit 도달: {e}")
            print(f"   지금까지 수집된 결과는 {jsonl_path}에 저장됨.")
            rate_limited = True
            break
        stats_list.append(stats)

    print("\n" + "=" * 70)
    print("  OLD vs NEW 베이스라인 리포트")
    print("=" * 70)
    for s in stats_list:
        print_stats(s)

    if len(stats_list) == 2:
        old_r = stats_list[0]["error_rate"]
        new_r = stats_list[1]["error_rate"]
        print(f"\n{'─' * 70}")
        print(f"  OLD 오류율: {old_r:.1%}")
        print(f"  NEW 오류율: {new_r:.1%}")
        diff = old_r - new_r
        if diff > 0:
            print(f"  개선폭:     {diff:+.1%}p (NEW가 오류 감소)")
        elif diff < 0:
            print(f"  개선폭:     {diff:+.1%}p (NEW가 오히려 오류 증가)")
        else:
            print(f"  개선폭:     0 (차이 없음)")
        print("=" * 70)

    save_summary(stats_list, json_path)
    print(f"\n[baseline] 집계 저장: {json_path}")

    return 1 if rate_limited else 0


if __name__ == "__main__":
    sys.exit(main())
