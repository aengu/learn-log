"""
RAG 컨텍스트 절삭 길이 벤치마크 (웹 생략 경로: 500자 vs 1500자)

배경: 라우터가 "기존 로그로 충분"이라 판단하면 웹검색을 생략하는데,
이때 유일한 외부 컨텍스트인 로그를 500자로 자르면 판단 근거(기록이 답을
담고 있다)와 실제 주입량이 어긋난다. 1500자로 늘리면 해결되는지,
0610에서 본 "컨텍스트 늘리면 출력이 부풀어 잘림"이 재발하는지 측정한다.

실험 A (효용, 카나리): 모델이 절대 알 수 없는 가짜 디테일(임의 수치)을
  답변 텍스트의 500자 이후에만 심은 합성 로그를 주입하고, 그 디테일을
  직접 묻는다. 정답 포함 여부 = 컨텍스트 반영 여부 (자동 판정).
  ⚠️ 0610 교훈 반영: 모델 자체 지식으로 답 가능한 주제면 차이가 안 보인다.
실험 B (비용): 실제 로그 top-3을 500/1500자로 주입해 생성.
  출력 토큰·총시간·finish_reason(잘림)을 비교한다.

총 호출: A(2질문 × 2조건 × 2회) + B(2질문 × 2조건 × 2회) = 16 calls (mistral-large)
실행: docker compose exec web python benchmarks/0611/benchmark_rag_context_limit.py
"""
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django  # noqa: E402

django.setup()

from search.services import LearnlogService  # noqa: E402

ROUNDS = 2
CALL_GAP_SEC = 2
CONDITIONS = [500, 1500]
RESULTS_DIR = Path(__file__).parent / "results"


# ── 실험 A 픽스처: 카나리(가짜 디테일)는 500자 이후에만 등장 ──────────
# 앞 500자는 일반론이라 잘려도 모델이 일반 지식으로 답할 수 있는 내용이고,
# 질문이 묻는 수치는 전부 500자 뒤에 있다.
# canaries: (이름, 정규식) — 픽스처와 모델의 예상 표현("7회", "RETRY_LIMIT=7")을 함께 커버
CANARY_CASES = [
    {
        "name": "retry_convention",
        "log_query": "우리 팀 Django 트랜잭션 재시도 컨벤션 정리",
        "log_answer": (
            "## 개념\n"
            "트랜잭션 재시도는 일시적 오류(데드락, 직렬화 실패)로 실패한 트랜잭션을 "
            "자동으로 다시 시도하는 패턴입니다. Django 자체에는 내장 재시도 메커니즘이 "
            "없어서 데코레이터로 직접 구현하는 것이 일반적입니다. 모든 예외를 재시도하는 "
            "것이 아니라 일시적 오류만 골라서 재시도하는 것이 핵심입니다. 무결성 위반 "
            "같은 영구적 오류를 재시도하면 같은 실패가 반복될 뿐이기 때문입니다.\n\n"
            "## 동작 원리\n"
            "PostgreSQL에서 동시성 충돌이 발생하면 OperationalError 계열 예외가 발생합니다. "
            "재시도 데코레이터는 이 예외를 잡아서 트랜잭션 블록 전체를 처음부터 다시 실행합니다. "
            "부분 재실행은 절대 안 되며, 반드시 atomic 블록 바깥에서 재시도해야 합니다. "
            "atomic 블록 안에서 잡으면 이미 깨진 트랜잭션 위에서 쿼리를 실행하게 되어 "
            "추가 오류가 발생합니다. 재시도 사이에는 대기 시간을 두는데, 고정 간격보다 "
            "지수 백오프가 권장됩니다. 충돌이 반복되는 상황에서 모든 워커가 동시에 "
            "재시도하면 같은 충돌이 다시 일어나기 때문입니다.\n\n"
            "## 우리 팀 컨벤션\n"
            "재시도 횟수는 7회(RETRY_LIMIT=7)로 통일한다. 일반적인 3회보다 길게 잡은 "
            "이유는 배치 작업의 데드락 빈도 때문. 지수 백오프 시작 간격은 350ms로 하고 "
            "배수는 1.8을 쓴다. 데코레이터 이름은 @team_tx_retry로 통일하고 "
            "services 레이어에만 붙인다. view에 직접 붙이는 것은 금지.\n\n"
            "## 주의사항\n"
            "재시도 대상은 멱등한 트랜잭션뿐입니다. 외부 API 호출이 섞인 블록에 붙이면 "
            "중복 호출이 발생합니다."
        ),
        "question": "우리 팀 트랜잭션 재시도 컨벤션에서 재시도 횟수랑 백오프 시작 간격이 뭐였지?",
        "canaries": [
            ("재시도 7회", r"7\s*(회|번)|RETRY_LIMIT\s*=?\s*7"),
            ("백오프 350ms", r"350"),
        ],
    },
    {
        "name": "healthcheck_policy",
        "log_query": "LearnLog 배포 헬스체크 정책 정리",
        "log_answer": (
            "## 개념\n"
            "헬스체크는 배포된 서비스가 정상 동작하는지 주기적으로 확인하는 메커니즘입니다. "
            "로드밸런서나 배포 플랫폼(Render 등)이 특정 엔드포인트를 주기적으로 호출해서 "
            "응답 코드와 응답 시간을 확인합니다. 응답이 없거나 오류가 반복되면 플랫폼이 "
            "인스턴스를 비정상으로 판정합니다.\n\n"
            "## 동작 원리\n"
            "헬스체크 엔드포인트는 보통 DB 연결 같은 핵심 의존성을 가볍게 확인하고 "
            "정상이면 200을 반환합니다. 너무 무거운 검사를 넣으면 헬스체크 자체가 서비스에 "
            "부하를 주고, 반대로 너무 가벼우면(무조건 200 반환) 실제 장애를 못 잡습니다. "
            "의존성 검사의 깊이와 호출 주기 사이의 균형이 설계의 핵심입니다. 플랫폼은 "
            "연속 실패 횟수가 임계치를 넘으면 인스턴스를 재시작하거나 트래픽에서 제외하는데, "
            "임계치가 너무 낮으면 일시적 지연에도 재시작이 반복되고, 너무 높으면 장애 상태가 "
            "오래 방치됩니다. 타임아웃 값도 마찬가지로 평소 응답 시간과 콜드스타트 상황을 "
            "함께 고려해서 정해야 합니다.\n\n"
            "## 우리 정책\n"
            "헬스체크 타임아웃은 17초로 설정한다. LLM 클라이언트 워밍업 중 콜드스타트를 "
            "고려한 값. 연속 실패 4회면 인스턴스를 재시작한다. 엔드포인트는 /healthz로 "
            "통일하고 DB는 SELECT 1만 확인한다. 외부 API(Groq, Tavily) 연결은 헬스체크에서 "
            "확인하지 않는다. 외부 장애가 우리 서비스 재시작으로 이어지면 안 되기 때문.\n\n"
            "## 주의사항\n"
            "keep-alive 핑과 헬스체크를 혼동하면 안 됩니다."
        ),
        "question": "우리 헬스체크 정책에서 타임아웃이랑 연속 실패 몇 회에 재시작하기로 했지?",
        "canaries": [
            ("타임아웃 17초", r"17\s*초"),
            ("실패 4회", r"4\s*(회|번)"),
        ],
    },
]

# ── 실험 B: 실제 로그를 검색해서 쓰는 질문 (웹 생략 경로가 뜨는 주제) ──
REAL_QUESTIONS = [
    "django ORM N+1 문제 해결 방법",
    "django에서 멀티 db 사용할 때 트랜잭션 원자성 보장하는 방법",
]


def build_skipweb_prompt(service, query, logs, limit):
    """웹 생략 경로의 프로덕션 프롬프트 재현 (Tavily 블록 = 없음)"""
    retrieved = service._build_retrieved_context(logs, limit=limit)
    return (
        "개발 질문에 한국어로 답변하세요.\n\n"
        f"{retrieved}질문: {query}\n\n"
        "참고:\n없음\n\n"
        f"{service.DEFAULT_INSTRUCTIONS}"
    )


def call_mistral(service, prompt):
    t0 = time.perf_counter()
    response = service.mistral_client.chat.complete(
        model=service.ANSWER_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
        max_tokens=2000,
    )
    elapsed = time.perf_counter() - t0
    choice = response.choices[0]
    return {
        "answer": choice.message.content.strip(),
        "finish_reason": str(choice.finish_reason),
        "prompt_tokens": response.usage.prompt_tokens,
        "completion_tokens": response.usage.completion_tokens,
        "elapsed": round(elapsed, 2),
    }


def main():
    service = LearnlogService()
    RESULTS_DIR.mkdir(exist_ok=True)
    out_path = RESULTS_DIR / f"rag_context_limit_{datetime.now():%H%M%S}.jsonl"
    records = []

    # 픽스처 무결성: 카나리가 500자 이후 ~ 1500자 이내에 있는지 확인
    for case in CANARY_CASES:
        head = case["log_answer"][:500]
        body = case["log_answer"][:1500]
        for name, pattern in case["canaries"]:
            assert not re.search(pattern, head), f"{case['name']}: 카나리 [{name}]가 앞 500자에 누설됨"
            assert re.search(pattern, body), f"{case['name']}: 카나리 [{name}]가 1500자 안에 없음"

    print("=== 실험 A: 카나리 (500자 이후 디테일을 컨텍스트로 받는가) ===")
    for case in CANARY_CASES:
        log = SimpleNamespace(query=case["log_query"], ai_response=case["log_answer"])
        for limit in CONDITIONS:
            hits = 0
            for r in range(ROUNDS):
                time.sleep(CALL_GAP_SEC)
                prompt = build_skipweb_prompt(service, case["question"], [log], limit)
                result = call_mistral(service, prompt)
                found = [name for name, pattern in case["canaries"] if re.search(pattern, result["answer"])]
                hit = len(found) == len(case["canaries"])
                hits += hit
                records.append({"exp": "A", "case": case["name"], "limit": limit,
                                "round": r, "hit": hit, "found": found, **result})
                print(f"  {case['name']} limit={limit} round={r}: "
                      f"{'✅' if hit else '❌'} (발견: {found}) {result['elapsed']}s")
            print(f"  → {case['name']} limit={limit}: {hits}/{ROUNDS}")

    print("\n=== 실험 B: 비용 (실제 로그 top-3, 출력 부풀음·잘림) ===")
    for question in REAL_QUESTIONS:
        logs = service.retrieve_similar_logs(question)
        print(f"  Q: {question} (검색 {len(logs)}건)")
        for limit in CONDITIONS:
            for r in range(ROUNDS):
                time.sleep(CALL_GAP_SEC)
                prompt = build_skipweb_prompt(service, question, logs, limit)
                result = call_mistral(service, prompt)
                truncated = result["finish_reason"] != "stop"
                records.append({"exp": "B", "case": question, "limit": limit,
                                "round": r, "truncated": truncated,
                                **{k: v for k, v in result.items() if k != "answer"},
                                "answer": result["answer"]})
                print(f"    limit={limit} round={r}: 입력 {result['prompt_tokens']}tok / "
                      f"출력 {result['completion_tokens']}tok / {result['elapsed']}s"
                      f"{' ⚠️잘림' if truncated else ''}")

    with open(out_path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"\n원시 결과 저장: {out_path}")

    # 요약
    print("\n=== 요약 ===")
    for limit in CONDITIONS:
        a = [r for r in records if r["exp"] == "A" and r["limit"] == limit]
        b = [r for r in records if r["exp"] == "B" and r["limit"] == limit]
        a_hits = sum(r["hit"] for r in a)
        b_out = sum(r["completion_tokens"] for r in b) / len(b)
        b_time = sum(r["elapsed"] for r in b) / len(b)
        b_trunc = sum(r["truncated"] for r in b)
        print(f"limit={limit}: 카나리 {a_hits}/{len(a)} | "
              f"평균 출력 {b_out:.0f}tok | 평균 {b_time:.1f}s | 잘림 {b_trunc}/{len(b)}")


if __name__ == "__main__":
    main()
