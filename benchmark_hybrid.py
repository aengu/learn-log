"""
하이브리드 벤치마크: Mistral Large (답변) + Groq Llama 3.3 (태그, 마크다운)
vs 전부 Mistral Large / 전부 Groq
"""
import os
import time
from dotenv import load_dotenv
from mistralai.client import Mistral
from groq import Groq

load_dotenv()

mistral_client = Mistral(
    api_key=os.getenv("MISTRAL_API_KEY"),
    timeout_ms=120_000,
)
groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

TASKS = {
    "답변 생성": {
        "prompt": """당신은 친절하고 정확한 개발 전문가입니다.
사용자 질문: Docker 컨테이너와 가상머신의 차이점은 무엇인가요?
위 질문에 대한 명확하고 상세한 답변을 작성해주세요.
요구사항:
- 한국어로 작성
- 개념 설명 → 동작 원리 → 코드 예시 → 주의사항 순서로 구성
- 코드 예시는 반드시 포함""",
        "max_tokens": 2000,
        "temperature": 0.7,
    },
    "태그 추출": {
        "prompt": """다음 개발 질문과 답변에서 핵심 기술 태그를 추출해주세요.
질문: Docker 컨테이너와 가상머신의 차이점은 무엇인가요?
답변: Docker는 OS 수준의 가상화를 사용하고 VM은 하드웨어 수준...
규칙:
- 정확히 3~5개의 태그만 추출
- 모두 소문자, 영어만 사용
- 쉼표로 구분
태그:""",
        "max_tokens": 50,
        "temperature": 0.2,
    },
    "마크다운 변환": {
        "prompt": """다음 내용을 노션 스타일 마크다운으로 정리해주세요:
질문: Docker 컨테이너와 가상머신의 차이점은 무엇인가요?
답변: Docker는 컨테이너 기술로 OS 커널을 공유하며...
요구사항:
- 제목은 ## 질문 형식으로
- 핵심 내용은 명확하게 구조화
- 차이점이나 비교는 표(table) 사용
출력:""",
        "max_tokens": 1000,
        "temperature": 0.5,
    },
}


def call_mistral(task):
    start = time.time()
    response = mistral_client.chat.complete(
        model="mistral-large-latest",
        messages=[{"role": "user", "content": task["prompt"]}],
        temperature=task["temperature"],
        max_tokens=task["max_tokens"],
    )
    elapsed = time.time() - start
    tokens = response.usage.completion_tokens
    return elapsed, tokens


def call_groq(task):
    start = time.time()
    response = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": task["prompt"]}],
        temperature=task["temperature"],
        max_tokens=task["max_tokens"],
    )
    elapsed = time.time() - start
    tokens = response.usage.completion_tokens
    return elapsed, tokens


# 조합 정의: (작업명, 호출 함수, 라벨)
CONFIGS = {
    "A) 전부 Mistral Large": {
        "답변 생성": call_mistral,
        "태그 추출": call_mistral,
        "마크다운 변환": call_mistral,
    },
    "B) 전부 Groq Llama3.3": {
        "답변 생성": call_groq,
        "태그 추출": call_groq,
        "마크다운 변환": call_groq,
    },
    "C) 하이브리드 (답변=Mistral, 나머지=Groq)": {
        "답변 생성": call_mistral,
        "태그 추출": call_groq,
        "마크다운 변환": call_groq,
    },
}


def main():
    print("=" * 65)
    print("Mistral Large vs Groq Llama3.3 하이브리드 벤치마크")
    print("=" * 65)

    all_results = {}

    for config_name, task_funcs in CONFIGS.items():
        print(f"\n--- {config_name} ---")
        all_results[config_name] = {}
        for task_name, func in task_funcs.items():
            elapsed, tokens = func(TASKS[task_name])
            all_results[config_name][task_name] = (elapsed, tokens)
            print(f"  {task_name}: {elapsed:.2f}s ({tokens} tokens, {tokens/elapsed:.0f} tok/s)")

    # 요약
    print("\n" + "=" * 65)
    print("비교 요약")
    print("=" * 65)
    print(f"{'조합':<45} {'합계':>8} {'tok/s':>8}")
    print("-" * 65)
    for config_name, results in all_results.items():
        total_time = sum(v[0] for v in results.values())
        total_tokens = sum(v[1] for v in results.values())
        avg_tps = total_tokens / total_time if total_time > 0 else 0
        print(f"{config_name:<45} {total_time:>6.2f}s {avg_tps:>6.0f}")

    # 하이브리드 vs 전부 large 절감률
    large_total = sum(v[0] for v in all_results["A) 전부 Mistral Large"].values())
    hybrid_total = sum(v[0] for v in all_results["C) 하이브리드 (답변=Mistral, 나머지=Groq)"].values())
    groq_total = sum(v[0] for v in all_results["B) 전부 Groq Llama3.3"].values())
    print("-" * 65)
    print(f"하이브리드 vs 전부 Large: {(1-hybrid_total/large_total)*100:.0f}% 절감")
    print(f"하이브리드 vs 전부 Groq:  +{(hybrid_total/groq_total-1)*100:.0f}% (품질 향상 대가)")


if __name__ == "__main__":
    main()
