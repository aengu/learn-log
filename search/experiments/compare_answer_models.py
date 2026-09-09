"""답변 생성(ANSWER_MODEL) 대체 후보를 비교한다.

배경:
  ANSWER_MODEL = mistral-large-latest 가 은퇴(403)해서 답변 생성이 멈췄다.
  출제(compare_models.py)와는 성격이 달라 같은 잣대를 쓸 수 없다.
    - 출력이 JSON이 아니라 산문이라 결정적 게이트가 없다
    - RAG 컨텍스트가 붙어 입력이 훨씬 길다
    - 프로덕션은 스트리밍이지만, 여기서는 총 소요시간만 보면 되므로 비스트리밍으로 잰다

무엇을 재는가:
  1. 지시 준수 — DEFAULT_INSTRUCTIONS가 "개념 → 동작 원리 → 코드 예시 → 주의사항"과
     "코드에 주석 포함"을 요구한다. 코드블록과 주석 유무는 기계로 볼 수 있다.
  2. 잘림     — finish_reason == 'length' 면 max_tokens에 걸려 답변이 중간에 끊긴 것.
  3. 근거 이탈 — check_consistency(LLM Judge)로 컨텍스트와 모순되는지 본다.
                 Judge는 생성 모델과 다른 계열이라 교차 검증이 된다.
  4. 한국어    — "한국어로 답변하세요" 지시를 지키는지.
  5. 지연/토큰

주의:
  1·4는 형식 검사다. 코드블록이 있다고 코드가 맞다는 뜻은 아니다.
  3만 내용을 보는데, 그 Judge도 LLM이라 판정이 흔들릴 수 있다.

실행:
  docker compose exec web python -m search.experiments.compare_answer_models [반복수]
  PACE_SEC=25 ONLY=<label> 로 조절.
"""

import os
import re
import statistics
import sys
import time

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from search.experiments._common import (           # noqa: E402
    QUERIES,
    RATE_LIMIT_HINTS,
    RateLimitHit,
    append_jsonl,
    ensure_results_dir,
    timestamp,
)
from search.services.learnlog_service import LearnlogService   # noqa: E402

PACE_SEC = float(os.environ.get("PACE_SEC", 25))
MAX_TOKENS = 2000        # 프로덕션 generate_answer_stream과 동일

CANDIDATES = [
    ("gpt-oss-120b",  "groq",    "openai/gpt-oss-120b"),
    ("gpt-oss-20b",   "groq",    "openai/gpt-oss-20b"),
    ("ministral-14b", "mistral", "ministral-14b-2512"),
]


def call_answer(client, kind, prompt, model):
    """산문 답변을 받는다. (경과초, 본문, usage, finish_reason, 에러).

    _common의 call_*_timed는 response_format=json_object를 강제한다. 출제는 JSON이라
    맞지만 답변은 마크다운 산문이라 그걸 쓰면 안 된다. 프로덕션 generate_answer_stream과
    같은 설정(temperature=0.7, max_tokens=2000, JSON 강제 없음)으로 맞춘다.
    """
    started = time.perf_counter()
    try:
        if kind == "groq":
            r = client.chat.completions.create(
                model=model, messages=[{"role": "user", "content": prompt}],
                temperature=0.7, max_tokens=MAX_TOKENS)
        else:
            r = client.chat.complete(
                model=model, messages=[{"role": "user", "content": prompt}],
                temperature=0.7, max_tokens=MAX_TOKENS)
    except Exception as e:
        if any(h in str(e).lower() for h in RATE_LIMIT_HINTS):
            raise RateLimitHit(str(e)) from e
        return time.perf_counter() - started, "", {}, None, f"[API 에러] {e}"

    elapsed = time.perf_counter() - started
    ch = r.choices[0]
    u = getattr(r, "usage", None)
    usage = {"prompt_tokens": getattr(u, "prompt_tokens", None),
             "completion_tokens": getattr(u, "completion_tokens", None)}
    return elapsed, ch.message.content or "", usage, str(ch.finish_reason), None


def get_client(provider):
    if provider == "groq":
        from groq import Groq
        return Groq(api_key=os.environ["GROQ_API_KEY"]), "groq"
    from mistralai.client import Mistral
    return Mistral(api_key=os.environ["MISTRAL_API_KEY"]), "mistral"


def build_prompt(q):
    """프로덕션 _build_answer_prompt를 그대로 쓴다.

    실제 호출은 Tavily 검색 결과와 과거 로그가 붙는다. 여기서는 DB와 외부 API를
    끌어오지 않고, 픽스처의 응답을 웹 컨텍스트로 흉내 내 입력 길이를 비슷하게 맞춘다.
    후보들끼리 같은 입력을 받는 게 중요하지, 실제 검색 결과일 필요는 없다.
    """
    svc = LearnlogService.__new__(LearnlogService)      # API 키 없이 메서드만 쓴다
    fake_search = {"results": [{"url": "https://example.com/doc", "content": q["response"]}]}

    # 프로덕션은 과거 로그가 RAG로 붙어 입력이 길다. 다른 픽스처 2개를 기록처럼 넣어
    # 입력 길이를 실제에 맞춘다(_build_retrieved_context는 .query/.ai_response만 읽는다).
    class _Log:
        def __init__(self, d):
            self.query, self.ai_response = d["query"], d["response"]
    others = [_Log(o) for o in QUERIES if o["id"] != q["id"]][:2]

    return LearnlogService._build_answer_prompt(
        svc, q["query"], fake_search, None, None, others, 500), fake_search


CODE_FENCE = re.compile(r"```")
COMMENT = re.compile(r"^\s*(#|//|/\*)", re.M)
SECTIONS = ("개념", "동작", "원리", "예시", "주의")


def grade(text):
    """형식 지시를 지켰는지 본다. 내용의 옳고 그름은 여기서 판단하지 않는다."""
    ko = sum(1 for c in text if "가" <= c <= "힣")
    fences = len(CODE_FENCE.findall(text))
    return {
        "글자수": len(text),
        "한국어비율": round(ko / max(len(text), 1), 2),
        "코드블록": fences // 2,
        "코드주석": bool(COMMENT.search(text)),
        "섹션": sum(1 for s in SECTIONS if s in text),
    }


def main():
    iterations = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    only = os.environ.get("ONLY")
    jsonl = ensure_results_dir() / f"answer_compare_{timestamp()}.jsonl"

    # 프로덕션 LIGHT_MODEL(llama-3.3-70b-versatile)은 Groq에서 폐기(404)됐다.
    # 벤치를 돌리려면 살아있는 모델이 필요해 여기서만 갈아끼운다.
    # gpt-oss 계열은 추론 토큰이 check_consistency의 max_tokens=200을 넘겨 JSON을 못 끝낸다.
    # qwen3.8-27b는 출력이 작아 OTPM 제한에도 안 걸리고 판정도 정확해 Judge로 적합하다.
    JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "qwen/qwen3.8-27b")
    LearnlogService.LIGHT_MODEL = JUDGE_MODEL
    judge = LearnlogService()
    print(f"Judge: {JUDGE_MODEL} (프로덕션 LIGHT_MODEL은 폐기되어 대체)")

    print(f"답변 생성 모델 비교 — 쿼리 {len(QUERIES)}개 × 반복 {iterations}")
    print(f"간격 {PACE_SEC}초 / max_tokens={MAX_TOKENS}")
    print(f"결과: {jsonl}\n")

    summary = {}
    for label, provider, model in CANDIDATES:
        if only and only != label:
            continue
        client, kind = get_client(provider)
        rows, stopped = [], None
        print(f"── {label}  ({model})")

        for q in QUERIES:
            for i in range(iterations):
                if stopped:
                    break
                prompt, fake_search = build_prompt(q)
                time.sleep(PACE_SEC)
                try:
                    elapsed, text, usage, finish, err = call_answer(client, kind, prompt, model)
                except RateLimitHit as e:
                    stopped = str(e)[:60]
                    print(f"   중단: {stopped}")
                    break

                g = grade(text)
                g["잘림"] = finish == "length"
                verdict = None
                if text:
                    try:
                        v = judge.check_consistency(text, None, 500, fake_search)
                        verdict = v.get("consistent") if v else None
                    except Exception as e:
                        verdict = f"판정불가({str(e)[:30]})"

                row = {"label": label, "model": model, "query_id": q["id"], "iteration": i,
                       "elapsed_sec": round(elapsed, 2), "error": err, "finish": finish,
                       "consistent": verdict, "answer": text, **g, **usage}
                append_jsonl(jsonl, row)
                if not err:
                    rows.append(row)
                print(f"   {q['id']:<18}{elapsed:6.2f}s  {g['글자수']:>5}자 "
                      f"한글{g['한국어비율']:.0%} 코드블록{g['코드블록']} "
                      f"주석{'O' if g['코드주석'] else 'X'} 섹션{g['섹션']}/5 "
                      f"{'잘림 ' if g['잘림'] else ''}근거={verdict}"
                      f"{'  ' + err[:40] if err else ''}")

        summary[label] = (rows, stopped)

    print(f"\n{'모델':<15}{'표본':<6}{'지연':<8}{'글자':<7}{'한글':<7}"
          f"{'코드블록':<9}{'주석':<6}{'섹션':<7}{'근거OK'}")
    print("-" * 76)
    for label, (rows, stopped) in summary.items():
        if not rows:
            print(f"{label:<15}측정 실패  {stopped or ''}")
            continue
        med = lambda k: statistics.median([r[k] for r in rows])
        ok = sum(1 for r in rows if r["consistent"] is True)
        print(f"{label:<15}{len(rows):<6}{med('elapsed_sec'):<8.2f}{med('글자수'):<7.0f}"
              f"{med('한국어비율'):<7.0%}{med('코드블록'):<9.0f}"
              f"{sum(1 for r in rows if r['코드주석'])}/{len(rows):<4}"
              f"{med('섹션'):<7.0f}{ok}/{len(rows)}"
              f"{'  잘림' + str(sum(1 for r in rows if r['잘림'])) if any(r['잘림'] for r in rows) else ''}"
              f"{'  (중단됨)' if stopped else ''}")

    print("\n* 섹션 = 개념/동작/원리/예시/주의 중 몇 개가 답변에 등장하는지 (형식 검사).")
    print("* 근거OK = check_consistency(Groq Judge)가 컨텍스트와 모순 없다고 본 비율.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
