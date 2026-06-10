"""
꼬리질문 검색어 변환 벤치마크
모호한 꼬리질문("그중에서...", "그러면...")을 영어 검색어로 변환할 때,
부모 질문 컨텍스트 유무가 변환 품질을 가르는지 확인한다.

배경: benchmark_followup_context.py는 검색 결과를 픽스처로 고정해서
답변 생성 단계만 측정했다. 실제로 컨텍스트가 필요한 곳은 그 앞단 —
_to_search_query가 모호한 꼬리질문을 받으면 엉뚱한 검색어가 나올 가능성.

조건: no_context (현재 동작) vs with_parent (부모 질문 한 줄 추가)
판정: 변환된 영어 검색어에 부모 주제 핵심 키워드가 포함되는지 (자동) + 눈 확인
호출: 2 조건 × 4 꼬리질문 × 3회 = 24 calls (Groq, max_tokens=40 — 사실상 무료)
"""
import os
import textwrap
import time

from dotenv import load_dotenv
from groq import Groq

load_dotenv()

client = Groq(api_key=os.getenv("GROQ_API_KEY"))
MODEL = "llama-3.3-70b-versatile"
ROUNDS = 3

# 꼬리질문 세트: 지시어 강도를 다르게 (전부 모호 ~ 일부 키워드 포함)
CASES = [
    {
        "name": "pg_default",
        "parent": "PostgreSQL 트랜잭션 격리 수준에는 어떤 것들이 있어?",
        "follow_up": "그중에서 기본값을 그대로 쓰면 어떤 문제가 생길 수 있어?",
        "expect_keywords": ["postgres", "read committed", "isolation"],  # 하나라도 있으면 성공
    },
    {
        "name": "docker_comm",
        "parent": "Docker bridge 네트워크와 host 네트워크의 차이가 뭐야?",
        "follow_up": "그러면 컨테이너끼리 통신할 때는 어떤 모드가 유리해?",
        "expect_keywords": ["docker", "bridge", "network"],
    },
    {
        "name": "django_n1",
        "parent": "Django ORM의 N+1 문제가 뭐야?",
        "follow_up": "그거 해결하려면 어떻게 해야 돼?",
        "expect_keywords": ["django", "n+1", "select_related", "prefetch"],
    },
    {
        "name": "jwt_expire",
        "parent": "JWT 토큰 인증 방식이 어떻게 동작해?",
        "follow_up": "만료되면 어떻게 처리하는 게 좋아?",
        "expect_keywords": ["jwt", "token", "refresh"],
    },
]


def build_prompt(case, with_parent):
    """learnlog_service._to_search_query 프롬프트 미러 (⚠️ 프로덕션과 동기화 유지)"""
    if with_parent:
        return textwrap.dedent(f"""
            Convert this developer question into a concise English web search query.
            Output only the search keywords (tech names, concepts), no explanation.

            Previous question (context): {case['parent']}
            Question: {case['follow_up']}

            Search query:
        """).strip()
    return textwrap.dedent(f"""
        Convert this developer question into a concise English web search query.
        Output only the search keywords (tech names, concepts), no explanation.

        Question: {case['follow_up']}

        Search query:
    """).strip()


def convert(prompt):
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
        max_tokens=40,
    )
    return response.choices[0].message.content.strip()


def main():
    print("=" * 70)
    print("꼬리질문 검색어 변환 벤치마크 (no_context vs with_parent)")
    print("=" * 70)

    summary = {"no_context": [0, 0], "with_parent": [0, 0]}  # [성공, 전체]

    for case in CASES:
        print(f"\n{'─' * 70}")
        print(f"📌 {case['name']}")
        print(f"   부모: {case['parent']}")
        print(f"   꼬리: {case['follow_up']}")
        for cond, with_parent in [("no_context", False), ("with_parent", True)]:
            prompt = build_prompt(case, with_parent)
            for i in range(ROUNDS):
                try:
                    q = convert(prompt)
                except Exception as e:
                    print(f"   ❌ {cond} r{i+1}: {e}")
                    continue
                hit = any(k.lower() in q.lower() for k in case["expect_keywords"])
                summary[cond][0] += int(hit)
                summary[cond][1] += 1
                mark = "✅" if hit else "❌"
                print(f"   {mark} {cond:<12} r{i+1}: {q}")
                time.sleep(0.5)

    print(f"\n{'=' * 70}")
    print("요약 — 변환 검색어에 부모 주제 키워드 포함률")
    print(f"{'=' * 70}")
    for cond, (ok, total) in summary.items():
        pct = ok / total * 100 if total else 0
        print(f"  {cond:<12}: {ok}/{total} ({pct:.0f}%)")


if __name__ == "__main__":
    main()
