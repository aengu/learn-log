"""
프롬프트 경량화 벤치마크
현재 프롬프트 vs 경량 프롬프트: 속도 + 답변 품질 비교
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

# 실제 Tavily 검색 결과와 유사한 크기
SEARCH_RESULTS = [
    {
        "url": "https://docs.docker.com/get-started/overview/",
        "content": "Docker는 애플리케이션을 개발, 배포, 실행하기 위한 오픈 플랫폼입니다. Docker를 사용하면 애플리케이션을 인프라에서 분리할 수 있습니다. Docker는 컨테이너라는 느슨하게 격리된 환경에서 애플리케이션을 패키징하고 실행할 수 있는 기능을 제공합니다. 컨테이너는 가볍고 호스트 머신의 커널을 직접 사용하므로 가상 머신보다 효율적입니다. Docker 컨테이너는 이미지를 기반으로 생성되며, 이미지는 컨테이너를 만들기 위한 읽기 전용 템플릿입니다.",
    },
    {
        "url": "https://www.redhat.com/en/topics/containers/containers-vs-vms",
        "content": "가상 머신(VM)은 하이퍼바이저를 통해 물리적 하드웨어를 가상화합니다. 각 VM에는 전체 OS 사본, 애플리케이션, 필요한 바이너리 및 라이브러리가 포함됩니다. VM은 수십 GB를 차지할 수 있으며 부팅 시간이 깁니다. 반면 컨테이너는 OS 커널을 공유하고 앱 계층만 격리하므로 수십 MB 정도로 가볍습니다. 컨테이너는 거의 즉시 시작되며 VM보다 훨씬 적은 리소스를 사용합니다.",
    },
    {
        "url": "https://kubernetes.io/docs/concepts/overview/",
        "content": "Kubernetes는 컨테이너화된 워크로드와 서비스를 관리하기 위한 오픈소스 플랫폼입니다. Kubernetes는 선언적 구성과 자동화를 모두 지원합니다. 컨테이너는 애플리케이션을 패키징하고 실행하는 좋은 방법입니다. 프로덕션 환경에서는 컨테이너를 실행하는 데 사용하는 여러 호스트를 관리해야 합니다. Kubernetes는 분산 시스템을 탄력적으로 실행하기 위한 프레임워크를 제공합니다.",
    },
]


# ── 현재 프롬프트 (원본) ──────────────────────────────────────
def build_current_prompt():
    context = "\n\n".join([
        f"출처: {r['url']}\n내용: {r['content'][:400]}"
        for r in SEARCH_RESULTS[:3]
    ])
    instructions = """- 한국어로 작성 (한자 사용 금지, 한글로만 표기)
- 기술적으로 정확하게
- 개념 설명 → 동작 원리 → 코드 예시 → 주의사항 순서로 구성
- 코드 예시는 반드시 포함하고, 각 줄에 주석으로 설명 추가
- 관련 개념이 있으면 함께 설명 (예: A를 쓸 때 B도 알아야 하는 경우)
- 핵심 포인트는 빠뜨리지 말고 충분히 상세하게"""

    return f"""당신은 친절하고 정확한 개발 전문가입니다.

사용자 질문: {QUERY}

참고 자료:
{context}

위 참고 자료를 바탕으로 질문에 대한 명확하고 상세한 답변을 작성해주세요.

요구사항:
{instructions}"""


# ── 경량 프롬프트 ─────────────────────────────────────────────
def build_light_prompt():
    context = "\n".join([
        f"[{r['url']}] {r['content'][:200]}"
        for r in SEARCH_RESULTS[:2]
    ])
    return f"""개발 질문에 한국어로 답변하세요.

질문: {QUERY}

참고:
{context}

형식: 개념 → 동작 원리 → 코드 예시 → 주의사항. 코드에 주석 포함."""


def call_mistral(prompt, max_tokens):
    start = time.time()
    response = client.chat.complete(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
        max_tokens=max_tokens,
    )
    elapsed = time.time() - start
    content = response.choices[0].message.content
    input_tokens = response.usage.prompt_tokens
    output_tokens = response.usage.completion_tokens
    return elapsed, input_tokens, output_tokens, content


def main():
    current_prompt = build_current_prompt()
    light_prompt = build_light_prompt()

    print("=" * 65)
    print("프롬프트 경량화 벤치마크")
    print("=" * 65)

    print(f"\n프롬프트 길이 비교:")
    print(f"  현재: {len(current_prompt)}자")
    print(f"  경량: {len(light_prompt)}자 ({len(light_prompt)/len(current_prompt)*100:.0f}%)")

    ROUNDS = 2
    configs = {
        "현재 (3결과×400자, max=2000)": (current_prompt, 2000),
        "경량 (2결과×200자, max=2000)": (light_prompt, 2000),
    }

    results = {}
    for label, (prompt, max_tokens) in configs.items():
        times = []
        print(f"\n{'─' * 65}")
        print(f"📌 {label}")
        print(f"{'─' * 65}")

        for i in range(ROUNDS):
            elapsed, in_tok, out_tok, content = call_mistral(prompt, max_tokens)
            times.append(elapsed)
            tps = out_tok / elapsed if elapsed > 0 else 0
            print(f"  Round {i+1}: {elapsed:.2f}s | in={in_tok} out={out_tok} | {tps:.0f} tok/s")
            if i == 0:
                print(f"  답변 미리보기: {content[:300]}...")

        avg = sum(times) / ROUNDS
        results[label] = avg
        print(f"  평균: {avg:.2f}s")

    # 요약
    labels = list(results.keys())
    print(f"\n{'=' * 65}")
    print("요약")
    print(f"{'=' * 65}")
    base = results[labels[0]]
    for label, avg in results.items():
        saved = base - avg
        pct = saved / base * 100 if base > 0 else 0
        print(f"  {label}: {avg:.2f}s (절감: {saved:.2f}s, {pct:.0f}%)")


if __name__ == "__main__":
    main()
