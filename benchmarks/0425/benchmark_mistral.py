"""
Mistral 모델별 속도 벤치마크
- mistral-large-latest vs mistral-small-latest
- 작업별(답변 생성, 태그 추출, 마크다운 변환) 소요시간 비교
"""
import os
import time
from dotenv import load_dotenv
from mistralai.client import Mistral

load_dotenv()

client = Mistral(
    api_key=os.getenv("MISTRAL_API_KEY"),
    timeout_ms=120_000,  # 2분 타임아웃
)

MODELS = ["mistral-large-latest", "mistral-small-latest"]

# 테스트용 프롬프트 (실제 서비스와 유사)
TASKS = {
    "답변 생성 (heavy)": {
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
    "태그 추출 (light)": {
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
    "마크다운 변환 (medium)": {
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


def benchmark(model, task_name, task):
    start = time.time()
    response = client.chat.complete(
        model=model,
        messages=[{"role": "user", "content": task["prompt"]}],
        temperature=task["temperature"],
        max_tokens=task["max_tokens"],
    )
    elapsed = time.time() - start
    tokens = response.usage.completion_tokens
    return elapsed, tokens


def main():
    print("=" * 60)
    print("Mistral 모델별 속도 벤치마크")
    print("=" * 60)

    results = {}

    for model in MODELS:
        print(f"\n--- {model} ---")
        results[model] = {}
        for task_name, task in TASKS.items():
            elapsed, tokens = benchmark(model, task_name, task)
            results[model][task_name] = (elapsed, tokens)
            print(f"  {task_name}: {elapsed:.2f}s ({tokens} tokens, {tokens/elapsed:.0f} tok/s)")

    # 비교 요약
    print("\n" + "=" * 60)
    print("비교 요약")
    print("=" * 60)
    print(f"{'작업':<25} {'large':>10} {'small':>10} {'속도 차이':>10}")
    print("-" * 60)
    for task_name in TASKS:
        large_t = results["mistral-large-latest"][task_name][0]
        small_t = results["mistral-small-latest"][task_name][0]
        ratio = large_t / small_t if small_t > 0 else 0
        print(f"{task_name:<25} {large_t:>8.2f}s {small_t:>8.2f}s {ratio:>8.1f}x")

    total_large = sum(v[0] for v in results["mistral-large-latest"].values())
    total_small = sum(v[0] for v in results["mistral-small-latest"].values())
    print("-" * 60)
    print(f"{'전체 합계':<25} {total_large:>8.2f}s {total_small:>8.2f}s {total_large/total_small:>8.1f}x")
    print(f"\n하이브리드 예상 (답변=large, 나머지=small):")
    hybrid = results["mistral-large-latest"]["답변 생성 (heavy)"][0] + \
             results["mistral-small-latest"]["태그 추출 (light)"][0] + \
             results["mistral-small-latest"]["마크다운 변환 (medium)"][0]
    print(f"  예상 소요: {hybrid:.2f}s (전부 large: {total_large:.2f}s, 절감: {(1-hybrid/total_large)*100:.0f}%)")


if __name__ == "__main__":
    main()
