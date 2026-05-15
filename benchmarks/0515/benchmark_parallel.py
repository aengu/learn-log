"""
태그 추출 + 마크다운 변환: 순차 vs 병렬 비교
"""
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from groq import Groq

load_dotenv()

groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
LIGHT_MODEL = "llama-3.3-70b-versatile"

# 테스트용 데이터 (실제 서비스와 동일한 크기)
QUERY = "Docker 컨테이너와 가상머신의 차이점은 무엇인가요?"
AI_ANSWER = """Docker는 OS 커널을 공유하는 컨테이너 기술이고, VM은 하이퍼바이저 위에 전체 OS를 실행합니다.
Docker 컨테이너는 가볍고 빠르게 시작되며, 호스트 OS의 커널을 직접 사용합니다.
반면 VM은 각각 독립된 OS를 가지므로 더 많은 리소스를 소비하지만 완전한 격리를 제공합니다.
컨테이너는 마이크로서비스 아키텍처에 적합하고, VM은 서로 다른 OS가 필요한 환경에 적합합니다."""
SEARCH_RESULTS = {
    "results": [
        {"title": "Docker docs", "url": "https://docs.docker.com", "content": "Docker는 컨테이너 기술..." * 10},
        {"title": "VM 비교", "url": "https://example.com", "content": "가상머신은 하이퍼바이저..." * 10},
    ]
}


def extract_tags():
    prompt = f"""다음 개발 질문과 답변에서 핵심 기술 태그를 추출해주세요.
질문: {QUERY}
답변: {AI_ANSWER[:500]}
규칙:
- 정확히 3~5개의 태그만 추출
- 모두 소문자, 영어만 사용
- 쉼표로 구분
태그:"""
    response = groq_client.chat.completions.create(
        model=LIGHT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=50,
    )
    return response.choices[0].message.content.strip()


def convert_to_markdown():
    refs = "\n".join([
        f"- [{r.get('title', 'N/A')}]({r.get('url', '')})"
        for r in SEARCH_RESULTS.get('results', [])
    ])
    prompt = f"""다음 내용을 노션 스타일 마크다운으로 정리해주세요:
질문: {QUERY}
답변: {AI_ANSWER}
참고 자료: {refs}
요구사항:
- 제목은 ## 질문 형식으로
- 핵심 내용은 명확하게 구조화
출력:"""
    response = groq_client.chat.completions.create(
        model=LIGHT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.5,
        max_tokens=2000,
    )
    return response.choices[0].message.content.strip()


def run_sequential():
    start = time.time()
    tags = extract_tags()
    markdown = convert_to_markdown()
    elapsed = time.time() - start
    return elapsed, tags, markdown


def run_parallel():
    start = time.time()
    with ThreadPoolExecutor(max_workers=2) as executor:
        tags_future = executor.submit(extract_tags)
        markdown_future = executor.submit(convert_to_markdown)
        tags = tags_future.result()
        markdown = markdown_future.result()
    elapsed = time.time() - start
    return elapsed, tags, markdown


def main():
    ROUNDS = 3

    print("=" * 60)
    print("태그 추출 + 마크다운 변환: 순차 vs 병렬")
    print("=" * 60)

    seq_times = []
    par_times = []

    for i in range(ROUNDS):
        print(f"\n--- Round {i + 1}/{ROUNDS} ---")

        # 순차
        seq_elapsed, seq_tags, _ = run_sequential()
        seq_times.append(seq_elapsed)
        print(f"  순차: {seq_elapsed:.2f}s | 태그: {seq_tags}")

        # 병렬
        par_elapsed, par_tags, _ = run_parallel()
        par_times.append(par_elapsed)
        print(f"  병렬: {par_elapsed:.2f}s | 태그: {par_tags}")

        saved = seq_elapsed - par_elapsed
        print(f"  절감: {saved:.2f}s ({saved/seq_elapsed*100:.0f}%)")

    # 요약
    avg_seq = sum(seq_times) / ROUNDS
    avg_par = sum(par_times) / ROUNDS
    avg_saved = avg_seq - avg_par

    print(f"\n{'=' * 60}")
    print("요약 (평균)")
    print(f"{'=' * 60}")
    print(f"  순차 평균: {avg_seq:.2f}s")
    print(f"  병렬 평균: {avg_par:.2f}s")
    print(f"  절감 평균: {avg_saved:.2f}s ({avg_saved/avg_seq*100:.0f}%)")


if __name__ == "__main__":
    main()
