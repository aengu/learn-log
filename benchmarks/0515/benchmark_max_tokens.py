"""
max_tokens 조정 벤치마크
경량 프롬프트 기준으로 max_tokens 값에 따른 속도 + 답변 완성도 비교
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
MODEL = "mistral-large-latest"

QUERY = "Docker 컨테이너와 가상머신의 차이점은 무엇인가요?"

SEARCH_RESULTS = [
    {
        "url": "https://docs.docker.com/get-started/overview/",
        "content": "Docker는 애플리케이션을 개발, 배포, 실행하기 위한 오픈 플랫폼입니다. Docker를 사용하면 애플리케이션을 인프라에서 분리할 수 있습니다. Docker는 컨테이너라는 느슨하게 격리된 환경에서 애플리케이션을 패키징하고 실행할 수 있는 기능을 제공합니다.",
    },
    {
        "url": "https://www.redhat.com/en/topics/containers/containers-vs-vms",
        "content": "가상 머신(VM)은 하이퍼바이저를 통해 물리적 하드웨어를 가상화합니다. 각 VM에는 전체 OS 사본, 애플리케이션, 필요한 바이너리 및 라이브러리가 포함됩니다. VM은 수십 GB를 차지할 수 있으며 부팅 시간이 깁니다.",
    },
]

context = "\n".join([
    f"[{r['url']}] {r['content'][:200]}"
    for r in SEARCH_RESULTS[:2]
])

PROMPT = f"""개발 질문에 한국어로 답변하세요.

질문: {QUERY}

참고:
{context}

형식: 개념 → 동작 원리 → 코드 예시 → 주의사항. 코드에 주석 포함."""

MAX_TOKENS_LIST = [2000, 1500, 1200]


def call_mistral(max_tokens):
    start = time.time()
    response = client.chat.complete(
        model=MODEL,
        messages=[{"role": "user", "content": PROMPT}],
        temperature=0.7,
        max_tokens=max_tokens,
    )
    elapsed = time.time() - start
    content = response.choices[0].message.content
    output_tokens = response.usage.completion_tokens
    hit_limit = output_tokens >= max_tokens - 10  # max에 거의 도달했는지
    return elapsed, output_tokens, hit_limit, content


def main():
    print("=" * 65)
    print("max_tokens 조정 벤치마크 (경량 프롬프트 기준)")
    print("=" * 65)

    ROUNDS = 2
    results = {}

    for max_tok in MAX_TOKENS_LIST:
        print(f"\n{'─' * 65}")
        print(f"📌 max_tokens={max_tok}")
        print(f"{'─' * 65}")

        times = []
        for i in range(ROUNDS):
            elapsed, out_tok, hit_limit, content = call_mistral(max_tok)
            times.append(elapsed)
            tps = out_tok / elapsed if elapsed > 0 else 0
            limit_warn = " ⚠️ MAX 도달" if hit_limit else ""
            print(f"  Round {i+1}: {elapsed:.2f}s | out={out_tok}/{max_tok} | {tps:.0f} tok/s{limit_warn}")
            if i == 0:
                # 답변 끝부분 확인 (잘렸는지 체크)
                last_100 = content[-150:]
                print(f"  답변 끝부분: ...{last_100}")

        avg = sum(times) / ROUNDS
        results[max_tok] = avg
        print(f"  평균: {avg:.2f}s")

    # 요약
    print(f"\n{'=' * 65}")
    print("요약")
    print(f"{'=' * 65}")
    base = results[MAX_TOKENS_LIST[0]]
    print(f"  {'max_tokens':<12} {'평균 시간':>10} {'절감':>10}")
    print(f"  {'-' * 35}")
    for max_tok, avg in results.items():
        saved = base - avg
        pct = saved / base * 100 if base > 0 else 0
        print(f"  {max_tok:<12} {avg:>8.2f}s {saved:>+7.2f}s ({pct:+.0f}%)")


if __name__ == "__main__":
    main()
