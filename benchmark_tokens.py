"""
Mistral Large 답변 생성: max_tokens별 속도 벤치마크
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

PROMPT = """당신은 친절하고 정확한 개발 전문가입니다.
사용자 질문: Docker 컨테이너와 가상머신의 차이점은 무엇인가요?
위 질문에 대한 명확하고 상세한 답변을 작성해주세요.
요구사항:
- 한국어로 작성
- 개념 설명 → 동작 원리 → 코드 예시 → 주의사항 순서로 구성
- 코드 예시는 반드시 포함"""

TOKEN_LIMITS = [500, 1000, 1500, 2000, 3000]


def main():
    print("=" * 55)
    print("Mistral Large - max_tokens별 속도 벤치마크")
    print("=" * 55)
    print(f"{'max_tokens':>12} {'실제 생성':>10} {'소요시간':>10} {'tok/s':>8}")
    print("-" * 55)

    for limit in TOKEN_LIMITS:
        start = time.time()
        response = client.chat.complete(
            model="mistral-large-latest",
            messages=[{"role": "user", "content": PROMPT}],
            temperature=0.7,
            max_tokens=limit,
        )
        elapsed = time.time() - start
        actual = response.usage.completion_tokens
        tps = actual / elapsed if elapsed > 0 else 0
        print(f"{limit:>12} {actual:>8} tok {elapsed:>8.2f}s {tps:>6.0f}")

    print("-" * 55)
    print("* 실제 생성 토큰이 max_tokens보다 적으면 답변이 자연 종료된 것")


if __name__ == "__main__":
    main()
