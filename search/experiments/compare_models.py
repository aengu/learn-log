"""출제 모델 후보를 같은 프롬프트로 돌려 비교한다.

배경:
  mistral-large-latest가 은퇴(403)하고 mistral-small-latest가 무료 티어에서 빠지면서
  (429, 분당 한도 0) 쓰던 모델 둘 다 못 쓰게 됐다. 대체 모델을 골라야 하는데,
  파라미터 수만 보고 고르면 안 된다. 실제로 재는 것은 세 가지다.

    1. 오답 규약을 지키는가  → _audit_quality 게이트 통과율
    2. 정답 자리가 맞는가    → _filter_valid_steps 생존율
    3. 얼마나 걸리는가       → 지연, 토큰

  1번이 제일 중요하다. 지시를 안 지키는 모델은 재생성이 늘어서 결국 더 느리다.

주의:
  게이트는 형식을 본다. why가 채워졌는지는 보지만 그 why가 말이 되는지는 못 본다.
  통과율이 높다고 문제가 좋다는 뜻은 아니고, 낮으면 확실히 나쁘다는 뜻이다.

  Groq 무료는 분당 8,000 토큰이 실질 제약이다(출제 1회 ≈ 2,500 토큰 → 분당 3회).
  PACE_SEC을 넉넉히 두지 않으면 측정이 아니라 429 재시도만 하게 된다.

실행:
  docker compose exec web python -m search.experiments.compare_models [반복수]
  PACE_SEC=25 로 간격 조절.
"""

import os
import statistics
import sys
import time

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from search.experiments._common import (           # noqa: E402
    QUERIES,
    RateLimitHit,
    append_jsonl,
    build_new_prompt,
    call_groq_timed,
    call_mistral_timed,
    ensure_results_dir,
    timestamp,
)
from search.services.exercise_service import ExerciseService   # noqa: E402

PACE_SEC = float(os.environ.get("PACE_SEC", 25))
DEFAULT_ITERATIONS = 1

# (표시이름, provider, 모델ID, 원래 무엇을 대체하는가)
CANDIDATES = [
    ("gpt-oss-120b",  "groq",    "openai/gpt-oss-120b", "large 2.1(123B) 대체 후보"),
    ("qwen3.8-27b",   "groq",    "qwen/qwen3.8-27b",    "small(24B) 대체 후보"),
    ("ministral-14b", "mistral", "ministral-14b-2512",  "같은 벤더 유지 후보"),
]


def get_client(provider):
    if provider == "groq":
        from groq import Groq
        return Groq(api_key=os.environ["GROQ_API_KEY"]), call_groq_timed
    from mistralai.client import Mistral
    return Mistral(api_key=os.environ["MISTRAL_API_KEY"]), call_mistral_timed


def score(parsed):
    """한 응답을 프로덕션과 똑같이 처리하고, 어느 단계에서 몇 개가 죽었는지 낸다.

    _filter_valid_steps는 정답자리 검사만 하는 게 아니라 끝에서 _apply_quality_gates까지
    호출한다. 그래서 결과 steps에 게이트를 또 돌리면 항상 전부 통과로 나온다(무의미).
    두 단계를 나누려면 반환된 _audit['dropped']의 reason을 봐야 한다.
      - reason이 문자열 'correct-index-mismatch' → 정답자리에서 탈락
      - reason이 코드 목록(list)                 → 오답 게이트에서 탈락
    """
    import copy
    raw_steps = (parsed or {}).get("steps", [])
    result = ExerciseService._filter_valid_steps(copy.deepcopy(parsed or {}))
    dropped = result.get("_audit", {}).get("dropped", [])

    idx_fail = sum(1 for d in dropped if isinstance(d.get("reason"), str))
    gate_fail = [d for d in dropped if isinstance(d.get("reason"), list)]
    violations = [code for d in gate_fail for code in d["reason"]]

    soft = [f for st in result.get("steps", []) for f in st.get("_quality_flags", [])]
    return {
        "raw": len(raw_steps),
        "정답자리_탈락": idx_fail,
        "게이트_탈락": len(gate_fail),
        "최종_생존": len(result.get("steps", [])),
        "위반": violations,
        "소프트": soft,
    }


def main():
    iterations = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ITERATIONS
    jsonl = ensure_results_dir() / f"model_compare_{timestamp()}.jsonl"

    print(f"출제 모델 비교 — 후보 {len(CANDIDATES)}개 × 쿼리 {len(QUERIES)}개 × 반복 {iterations}")
    print(f"간격 {PACE_SEC}초 (Groq 무료 8k tok/분 제약)")
    print(f"결과: {jsonl}\n")

    only = os.environ.get("ONLY")   # 특정 후보만 다시 돌릴 때 사용
    summary = {}
    for label, provider, model, note in CANDIDATES:
        if only and only != label:
            continue
        client, caller = get_client(provider)
        rows, stopped = [], None
        print(f"── {label}  ({model})  {note}")

        for q in QUERIES:
            for i in range(iterations):
                if stopped:
                    break
                time.sleep(PACE_SEC)
                try:
                    elapsed, parsed, usage, err = caller(client, build_new_prompt(
                        q["query"], q["response"]), model)
                except RateLimitHit as e:
                    stopped = str(e)[:60]
                    print(f"   중단: {stopped}")
                    break

                sc = score(parsed)
                row = {"model": model, "label": label, "query_id": q["id"], "iteration": i,
                       "elapsed_sec": round(elapsed, 2), "error": err,
                       # 원본 응답을 남긴다. 게이트를 고쳐도 API를 다시 안 쓰고 재채점할 수 있다.
                       "parsed": parsed, **sc, **usage}
                append_jsonl(jsonl, row)
                if not err:
                    rows.append(row)
                print(f"   {q['id']:<18}{elapsed:6.2f}s  raw={sc['raw']} "
                      f"→자리탈락 {sc['정답자리_탈락']} →게이트탈락 {sc['게이트_탈락']} "
                      f"→생존 {sc['최종_생존']}  {','.join(sorted(set(sc['위반']))) or '-'}"
                      f"{'  ' + err[:40] if err else ''}")

        summary[label] = (rows, stopped)

    print(f"\n{'모델':<15}{'표본':<6}{'지연':<8}{'생성':<7}{'자리탈락':<9}"
          f"{'게이트탈락':<11}{'생존':<7}{'생존율'}")
    print("-" * 72)
    for label, (rows, stopped) in summary.items():
        if not rows:
            print(f"{label:<15}측정 실패  {stopped or ''}")
            continue
        tot = lambda k: sum(r[k] for r in rows)
        raw_tot, alive = tot("raw"), tot("최종_생존")
        print(f"{label:<15}{len(rows):<6}"
              f"{statistics.median([r['elapsed_sec'] for r in rows]):<8.2f}"
              f"{raw_tot:<7}{tot('정답자리_탈락'):<9}{tot('게이트_탈락'):<11}{alive:<7}"
              f"{(alive / raw_tot if raw_tot else 0):.0%}"
              f"{'  (중단됨)' if stopped else ''}")

    print("\n버려진 이유:")
    for label, (rows, _) in summary.items():
        v = [c for r in rows for c in r["위반"]]
        s_ = [c for r in rows for c in r["소프트"]]
        if rows:
            hard = {c: v.count(c) for c in sorted(set(v))} or "없음"
            softd = {c: s_.count(c) for c in sorted(set(s_))} or "없음"
            print(f"  {label:<15}버림={hard}  표시만={softd}")

    print("\n* 생존율 = 최종 생존 step / 생성 step. 낮으면 재생성이 늘어 실제 지연은 더 커진다.")
    print("* 게이트는 형식만 본다. why가 채워졌는지는 보지만 말이 되는지는 못 본다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
