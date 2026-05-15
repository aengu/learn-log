"""
연습문제 생성 + 채점 프롬프트 경량화 벤치마크
이전 프롬프트 vs 경량 프롬프트: 속도 비교
"""
import os
import time
from dotenv import load_dotenv
from mistralai.client import Mistral

load_dotenv()

client = Mistral(
    api_key=os.getenv("MISTRAL_API_KEY"),
    timeout_ms=120_000,
)
MODEL = "mistral-small-latest"

# ── 테스트 데이터 ─────────────────────────────────────────────
QUERY = "Docker 컨테이너와 가상머신의 차이점은 무엇인가요?"
AI_RESPONSE = """Docker는 OS 커널을 공유하는 컨테이너 기술이고, VM은 하이퍼바이저 위에 전체 OS를 실행합니다.
Docker 컨테이너는 가볍고 빠르게 시작되며, 호스트 OS의 커널을 직접 사용합니다.
반면 VM은 각각 독립된 OS를 가지므로 더 많은 리소스를 소비하지만 완전한 격리를 제공합니다.
컨테이너는 마이크로서비스 아키텍처에 적합하고, VM은 서로 다른 OS가 필요한 환경에 적합합니다.
Docker는 이미지를 기반으로 동작하며 이미지는 Dockerfile로 정의합니다. 컨테이너는 이미지의 실행 인스턴스입니다.
VM은 하이퍼바이저(KVM, VMware, VirtualBox 등)를 통해 물리적 하드웨어를 가상화합니다.
컨테이너는 cgroups와 namespaces를 사용하여 프로세스를 격리합니다.
Docker Compose를 사용하면 여러 컨테이너를 한번에 관리할 수 있습니다."""

# 채점용 예시 데이터
EXERCISE_QUESTION = "Docker 컨테이너와 가상머신의 차이점을 설명해주세요."
MODEL_ANSWER = "Docker는 OS 커널을 공유하는 경량 가상화 기술이고, VM은 하이퍼바이저를 통해 독립된 OS를 실행하는 완전 가상화 기술입니다."
USER_ANSWER = "Docker는 커널을 공유하고, VM은 전체 OS를 사용합니다."
KEY_POINTS = ["OS 커널 공유 여부", "하이퍼바이저의 역할", "리소스 사용량 차이"]

# ── 이전 프롬프트 (원본) ──────────────────────────────────────
OLD_PROMPTS = {
    "생성: generation_compare": f"""다음 학습 내용을 바탕으로 "생성→비교" 유형 연습문제를 만들어주세요.

학습 내용:
질문: {QUERY}
답변: {AI_RESPONSE[:1000]}

"생성→비교" 유형: 학습자가 먼저 자신의 답변을 작성하고 AI 모범 답안과 비교합니다.

JSON으로만 응답하세요 (```없이):
{{
  "question": "학습자에게 물어볼 질문 (핵심 개념을 직접 설명하게 유도)",
  "model_answer": "모범 답안 (핵심 포인트를 포함한 상세한 답변)"
}}""",

    "생성: retrieval_checkin": f"""다음 학습 내용을 바탕으로 "인출 체크인" 유형 연습문제를 만들어주세요.

학습 내용:
질문: {QUERY}
답변: {AI_RESPONSE[:1000]}

"인출 체크인" 유형: 핵심 개념을 기억에서 꺼내는 연습. 학습자가 답변을 쓰면 AI가 핵심 포인트를 체크합니다.

JSON으로만 응답하세요 (```없이):
{{
  "question": "기억에서 꺼내게 하는 질문",
  "key_points": [
    "체크할 핵심 포인트 1",
    "체크할 핵심 포인트 2",
    "체크할 핵심 포인트 3"
  ]
}}
key_points는 3~5개로 구성하세요.""",

    "채점: generation_compare": f"""학습자의 답변과 모범 답안을 비교하여 평가해주세요.

질문: {EXERCISE_QUESTION}
모범 답안: {MODEL_ANSWER}
학습자 답변: {USER_ANSWER}

JSON으로만 응답하세요 (```없이):
{{
  "score": 0.0~1.0,
  "is_correct": true/false,
  "feedback": "아래 구조로 한국어로 작성:\\n1. 맞게 설명한 부분 (what)\\n2. 놓친 부분\\n3. 왜 그렇게 동작하는지 (why) — 학습자가 설명했으면 인정, 안 했으면 짚어주기"
}}""",

    "채점: retrieval_checkin": f"""학습자의 답변에서 핵심 포인트가 포함됐는지 확인해주세요.

질문: {EXERCISE_QUESTION}
핵심 포인트:
- OS 커널 공유 여부
- 하이퍼바이저의 역할
- 리소스 사용량 차이
학습자 답변: {USER_ANSWER}

JSON으로만 응답하세요 (```없이):
{{
  "covered_points": [핵심 포인트와 동일한 순서로 true/false 목록],
  "feedback": "어떤 포인트를 잘 다뤘고 무엇이 빠졌는지 한국어로 설명"
}}""",
}

# ── 경량 프롬프트 (현재) ──────────────────────────────────────
NEW_PROMPTS = {
    "생성: generation_compare": f"""아래 학습 내용으로 "생성→비교" 연습문제를 만들어주세요.
학습자가 먼저 답변을 쓰고 모범 답안과 비교하는 유형입니다.

질문: {QUERY}
답변: {AI_RESPONSE[:500]}

JSON으로만 응답 (```없이):
{{"question": "핵심 개념을 설명하게 유도하는 질문", "model_answer": "핵심 포인트 포함 모범 답안"}}""",

    "생성: retrieval_checkin": f"""아래 학습 내용으로 "인출 체크인" 연습문제를 만들어주세요.
핵심 개념을 기억에서 꺼내는 유형입니다. key_points는 3~5개.

질문: {QUERY}
답변: {AI_RESPONSE[:500]}

JSON으로만 응답 (```없이):
{{"question": "기억에서 꺼내게 하는 질문", "key_points": ["핵심 포인트1", "핵심 포인트2", "핵심 포인트3"]}}""",

    "채점: generation_compare": f"""학습자 답변을 모범 답안과 비교 평가하세요.

질문: {EXERCISE_QUESTION}
모범 답안: {MODEL_ANSWER}
학습자 답변: {USER_ANSWER}

JSON으로만 응답 (```없이):
{{"score": 0.0~1.0, "is_correct": true/false, "feedback": "1. 맞은 부분 2. 놓친 부분 3. 왜 그런지 (한국어)"}}""",

    "채점: retrieval_checkin": f"""학습자 답변에서 핵심 포인트 포함 여부를 확인하세요.

질문: {EXERCISE_QUESTION}
핵심 포인트:
- OS 커널 공유 여부
- 하이퍼바이저의 역할
- 리소스 사용량 차이
학습자 답변: {USER_ANSWER}

JSON으로만 응답 (```없이):
{{"covered_points": [true/false 목록], "feedback": "잘 다룬 점과 빠진 점 (한국어)"}}""",
}


def call_mistral(prompt, max_tokens=1500):
    start = time.time()
    response = client.chat.complete(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.4,
        max_tokens=max_tokens,
    )
    elapsed = time.time() - start
    input_tokens = response.usage.prompt_tokens
    output_tokens = response.usage.completion_tokens
    return elapsed, input_tokens, output_tokens


def main():
    print("=" * 65)
    print("연습문제 프롬프트 경량화 벤치마크 (Mistral Small)")
    print("=" * 65)

    tasks = list(OLD_PROMPTS.keys())

    for task in tasks:
        old_prompt = OLD_PROMPTS[task]
        new_prompt = NEW_PROMPTS[task]
        max_tokens = 500 if "채점" in task else 1500

        print(f"\n{'─' * 65}")
        print(f"📌 {task}")
        print(f"  프롬프트: {len(old_prompt)}자 → {len(new_prompt)}자 ({len(new_prompt)/len(old_prompt)*100:.0f}%)")
        print(f"{'─' * 65}")

        # 이전
        old_elapsed, old_in, old_out = call_mistral(old_prompt, max_tokens)
        print(f"  이전: {old_elapsed:.2f}s | in={old_in} out={old_out}")

        # 경량
        new_elapsed, new_in, new_out = call_mistral(new_prompt, max_tokens)
        print(f"  경량: {new_elapsed:.2f}s | in={new_in} out={new_out}")

        saved = old_elapsed - new_elapsed
        pct = saved / old_elapsed * 100 if old_elapsed > 0 else 0
        print(f"  절감: {saved:+.2f}s ({pct:+.0f}%)")

    # 전체 합산
    print(f"\n{'=' * 65}")
    print("전체 합산")
    print(f"{'=' * 65}")

    total_old = 0
    total_new = 0
    for task in tasks:
        max_tokens = 500 if "채점" in task else 1500
        e1, _, _ = call_mistral(OLD_PROMPTS[task], max_tokens)
        e2, _, _ = call_mistral(NEW_PROMPTS[task], max_tokens)
        total_old += e1
        total_new += e2

    saved = total_old - total_new
    pct = saved / total_old * 100 if total_old > 0 else 0
    print(f"  이전 합계: {total_old:.2f}s")
    print(f"  경량 합계: {total_new:.2f}s")
    print(f"  절감: {saved:+.2f}s ({pct:+.0f}%)")


if __name__ == "__main__":
    main()
