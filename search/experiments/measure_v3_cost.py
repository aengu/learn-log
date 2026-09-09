"""v3(오답 규약) 프롬프트가 v2보다 얼마나 느려졌는지 실측.

배경:
  오답 규약과 요령 방지 규칙을 넣으면서 프롬프트가 911자 → 1596자로 늘었다.
  지시문이 늘면 입력 토큰이 늘고, 출력에 distractors 배열이 붙으면서 출력 토큰도 는다.
  "체감상 느려졌다"가 아니라 숫자로 확인한다.

측정 방법:
  - 같은 쿼리에 대해 v2 → v3를 연달아 호출한다(쌍 비교).
    API 부하가 시간대에 따라 흔들리므로, 두 프롬프트를 다른 시각에 재면
    프롬프트 차이인지 그날 서버 상태인지 구분이 안 된다.
  - 호출당 벽시계 시간, prompt_tokens, completion_tokens, 생성된 step 수를 기록한다.
  - 한 번의 값은 버리고 중앙값으로 본다.

주의:
  - 프로덕션 _gen_path_trace는 검증 실패 시 1회 재생성한다.
    여기서 재는 것은 "호출 1회"의 비용이므로, 재생성이 붙으면 실제 지연은 최대 2배다.
    재생성 빈도까지 보려면 별도 실험이 필요하다.
  - provider를 groq으로 두면 프로덕션과 다른 모델로 재는 것이다.
    절대 지연(몇 초 걸리나)은 프로덕션 값이 아니고,
    "프롬프트가 길어지면 얼마나 늘어나나"라는 증분만 참고할 수 있다.
    2026-09-08 기준 Mistral 키는 분당 허용 요청이 0이라(x-ratelimit-limit-req-minute: 0)
    프로덕션 모델로는 측정이 불가능해 groq을 대타로 썼다.

실행:
  docker compose exec web python -m search.experiments.measure_v3_cost [반복수] [mistral|groq]

예산:
  5 쿼리 × 2 프롬프트 × 반복수 회. 기본 3 → 30 calls.
"""

import os
import statistics
import sys
import time

from search.experiments._common import (
    QUERIES,
    MISTRAL_MODEL,
    RateLimitHit,
    append_jsonl,
    build_new_prompt,
    build_v2_prompt,
    call_groq_timed,
    call_mistral_timed,
    ensure_results_dir,
    timestamp,
)

GROQ_MODEL = "qwen/qwen3.8-27b"   # mistral-small과 파라미터 규모가 가장 가까운 대타

DEFAULT_ITERATIONS = 3
VARIANTS = [("v2", build_v2_prompt), ("v3", build_new_prompt)]

# Mistral 무료 티어는 초당 요청 수를 막는다. 붙여서 쏘면 첫 호출부터 429가 난다.
PACE_SEC = float(os.environ.get("PACE_SEC", 2.0))   # 호출 사이 최소 간격
MAX_RETRY = 4         # 429를 만나면 간격을 늘려가며 재시도


def make_caller(provider):
    """(호출함수, 모델명, 클라이언트)를 돌려준다."""
    if provider == "mistral":
        from mistralai.client import Mistral
        return call_mistral_timed, MISTRAL_MODEL, Mistral(api_key=os.environ["MISTRAL_API_KEY"])
    if provider == "groq":
        from groq import Groq
        return call_groq_timed, GROQ_MODEL, Groq(api_key=os.environ["GROQ_API_KEY"])
    raise SystemExit(f"알 수 없는 provider: {provider} (mistral 또는 groq)")


def call_with_backoff(caller, client, prompt, model):
    """429를 만나면 대기 시간을 늘려가며 재시도한다.

    재시도 대기는 측정값에 넣지 않는다. 우리가 재려는 것은 프롬프트 길이가 만드는
    지연이지, 무료 티어의 대기열이 아니다. 마지막(성공한) 호출의 시간만 쓴다.
    """
    wait = PACE_SEC
    for attempt in range(MAX_RETRY):
        try:
            return caller(client, prompt, model)
        except RateLimitHit:
            wait *= 2
            print(f"      · 레이트 리밋 — {wait:.0f}초 쉬고 재시도 ({attempt + 1}/{MAX_RETRY})")
            time.sleep(wait)
    raise RateLimitHit(f"{MAX_RETRY}회 재시도 후에도 레이트 리밋")


def run(caller, client, model, iterations, jsonl_path):
    """쿼리마다 v2와 v3를 연달아 호출하고 기록. variant별 측정치 목록을 돌려준다."""
    records = {name: [] for name, _ in VARIANTS}
    calls = 0
    stop = None   # 레이트 리밋 등으로 중단된 사유

    for q in QUERIES:
        for i in range(iterations):
            for name, builder in VARIANTS:
                if stop:
                    break
                calls += 1
                prompt = builder(q["query"], q["response"])
                time.sleep(PACE_SEC)
                try:
                    elapsed, parsed, usage, err = call_with_backoff(caller, client, prompt, model)
                except RateLimitHit as e:
                    # 여기서 던지면 이미 모은 측정치가 통째로 날아간다. 멈추고 있는 걸로 리포트한다.
                    stop = str(e)
                    break

                steps = len(parsed.get("steps", [])) if parsed else 0
                row = {
                    "variant": name,
                    "query_id": q["id"],
                    "iteration": i,
                    "elapsed_sec": round(elapsed, 3),
                    "prompt_chars": len(prompt),
                    "steps": steps,
                    "error": err,
                    **usage,
                }
                append_jsonl(jsonl_path, row)
                records[name].append(row)

                mark = "!" if err else " "
                print(f"  {mark}[{name}] {q['id']:<18} {i+1}/{iterations} "
                      f"{elapsed:6.2f}s  in={usage.get('prompt_tokens')} "
                      f"out={usage.get('completion_tokens')} steps={steps}")

    if stop:
        print(f"\n  중단: {stop}")
    print(f"  성공 호출 {sum(len(v) for v in records.values())}회 (시도 {calls})")
    return records


def summarize(rows):
    ok = [r for r in rows if not r["error"]]
    if not ok:
        return None
    pick = lambda k: [r[k] for r in ok if r.get(k) is not None]
    lat = pick("elapsed_sec")
    return {
        "n": len(ok),
        "실패": len(rows) - len(ok),
        "지연_중앙값": statistics.median(lat),
        "지연_최소": min(lat),
        "지연_최대": max(lat),
        "입력토큰": statistics.median(pick("prompt_tokens") or [0]),
        "출력토큰": statistics.median(pick("completion_tokens") or [0]),
        "step수": statistics.median(pick("steps") or [0]),
    }


def report(records, model):
    s2, s3 = summarize(records["v2"]), summarize(records["v3"])
    if not (s2 and s3):
        print("\n  성공한 호출이 부족해 비교할 수 없다.")
        return

    print(f"\n  모델 {model} / 성공 호출 v2={s2['n']} v3={s3['n']} "
          f"(실패 v2={s2['실패']} v3={s3['실패']})\n")
    print(f"  {'항목':<14}{'v2':>10}{'v3':>10}{'차이':>12}")
    print("  " + "-" * 46)
    for key, unit in [("지연_중앙값", "s"), ("입력토큰", ""), ("출력토큰", ""), ("step수", "")]:
        a, b = s2[key], s3[key]
        delta = f"{b - a:+.2f}{unit}" if unit else f"{b - a:+.0f}"
        ratio = f" ({b / a:.2f}배)" if a else ""
        print(f"  {key:<14}{a:>9.2f}{unit}{b:>9.2f}{unit}{delta + ratio:>12}")

    print(f"\n  지연 범위:  v2 {s2['지연_최소']:.2f}~{s2['지연_최대']:.2f}s"
          f"   v3 {s3['지연_최소']:.2f}~{s3['지연_최대']:.2f}s")
    print("  * 프로덕션은 검증 실패 시 1회 재생성하므로 최악의 경우 위 값의 2배가 든다.")


def main():
    iterations = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ITERATIONS
    provider = sys.argv[2] if len(sys.argv) > 2 else "mistral"
    caller, model, client = make_caller(provider)

    results_dir = ensure_results_dir()
    jsonl_path = results_dir / f"v3_cost_{provider}_{timestamp()}.jsonl"

    print(f"v2 vs v3 소요시간 측정 — 쿼리 {len(QUERIES)}개 × 반복 {iterations} × 2 프롬프트")
    print(f"모델: {model} ({provider})")
    if provider != "mistral":
        print("  ! 프로덕션 모델이 아니다. 절대 지연이 아니라 v2 대비 증분만 볼 것.")
    print(f"결과: {jsonl_path}\n")

    records = run(caller, client, model, iterations, jsonl_path)
    report(records, model)
    return 0


if __name__ == "__main__":
    sys.exit(main())
