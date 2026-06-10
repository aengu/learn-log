"""
꼬리질문 컨텍스트 벤치마크
이전 대화(부모 질문+답변)를 프롬프트에 포함할 때의 비용과 효용을 조건별로 측정한다.

측정 항목:
  - TTFT (첫 토큰까지 시간): 컨텍스트 추가의 prefill 비용이 나타나는 핵심 지표
  - 총 시간 + 출력 토큰: 총시간 = TTFT + 출력/생성속도 로 분해해 출력 길이 변동과 분리
  - 입력 토큰: 조건별 실제 컨텍스트 크기
  - 답변 전문: JSONL 저장 → 맥락("그러면/그중에서") 해석 성공 여부는 수동 판정

조건 (5):
  baseline  컨텍스트 없음 (현재 동작)
  q_only    부모 질문만
  q_a500    부모 질문 + 답변 [:500]
  q_a1000   부모 질문 + 답변 [:1000]
  q_full    부모 질문 + 답변 전문

총 호출: 5 조건 × 2 질문쌍 × ROUNDS(2) = 20 calls
실행: python benchmarks/0610/benchmark_followup_context.py
"""
import json
import os
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from mistralai.client import Mistral

load_dotenv()

client = Mistral(
    api_key=os.getenv("MISTRAL_API_KEY"),
    timeout_ms=120_000,
)
MODEL = "mistral-large-latest"
ROUNDS = 2
CALL_GAP_SEC = 2  # 무료 티어 rate limit 완화용 호출 간격

RESULTS_DIR = Path(__file__).parent / "results"


# ── 질문 쌍: 부모 질문/답변(고정) + 지시어가 들어간 꼬리질문 ──────────
# 부모 답변은 프로덕션 형식(개념 → 동작 원리 → 코드 예시 → 주의사항)으로 고정.
# 매번 생성하면 변동이 생기고 호출 예산도 늘어나므로 고정 텍스트를 쓴다.
PAIRS = [
    {
        "name": "docker_network",
        "parent_query": "Docker bridge 네트워크와 host 네트워크의 차이가 뭐야?",
        "parent_answer": """## 개념

Docker 네트워크 모드는 컨테이너가 외부 및 다른 컨테이너와 통신하는 방식을 결정합니다. bridge는 기본 모드로, Docker가 호스트에 가상 브리지(docker0)를 만들고 각 컨테이너에 내부 IP를 할당합니다. host 모드는 네트워크 격리를 제거하고 컨테이너가 호스트의 네트워크 스택을 직접 사용합니다.

## 동작 원리

bridge 모드에서 컨테이너는 veth(가상 이더넷) 쌍으로 docker0 브리지에 연결됩니다. 외부로 나가는 트래픽은 NAT(masquerade)를 거치고, 외부에서 들어오는 트래픽은 -p 옵션의 포트 매핑(DNAT)을 통해 컨테이너로 전달됩니다. 같은 브리지에 연결된 컨테이너끼리는 내부 IP로 직접 통신할 수 있습니다.

host 모드에서는 컨테이너가 호스트의 네트워크 네임스페이스를 공유합니다. 별도 IP가 할당되지 않고, 컨테이너 안에서 열리는 포트가 곧 호스트의 포트입니다. NAT와 포트 매핑 과정이 없어서 네트워크 오버헤드가 가장 적습니다.

## 코드 예시

```bash
# bridge 모드 (기본): 호스트 8080 → 컨테이너 80 포트 매핑
docker run -d -p 8080:80 nginx

# host 모드: 컨테이너의 80 포트가 곧 호스트의 80 포트
docker run -d --network host nginx

# 사용자 정의 bridge 네트워크: 컨테이너 이름으로 DNS 통신 가능
docker network create mynet
docker run -d --network mynet --name web nginx
docker run -d --network mynet --name app myapp  # http://web 으로 접근 가능
```

## 주의사항

- host 모드는 포트 충돌에 주의해야 합니다. 호스트에서 이미 사용 중인 포트는 쓸 수 없습니다.
- host 모드는 Linux에서만 완전히 지원됩니다. Docker Desktop(Mac/Windows)은 VM 안에서 돌기 때문에 기대처럼 동작하지 않습니다.
- 기본 bridge보다는 사용자 정의 bridge가 권장됩니다. 컨테이너 이름 기반 DNS 해석이 가능하고 격리 수준도 더 좋습니다.
- 프로덕션에서 성능이 중요한 경우(예: 고처리량 프록시)에만 host 모드를 고려하고, 그 외에는 격리가 보장되는 bridge가 안전합니다.

## 관련 개념: 그 외 네트워크 드라이버

- **none**: 네트워크 인터페이스를 아예 붙이지 않습니다. 완전 격리가 필요한 배치 작업 등에 사용합니다.
- **overlay**: 여러 Docker 호스트에 걸친 컨테이너를 하나의 가상 네트워크로 묶습니다. Swarm/멀티 호스트 환경에서 사용합니다.
- **macvlan**: 컨테이너에 물리 네트워크상의 MAC 주소를 직접 부여해, 네트워크 장비 입장에서 물리 장치처럼 보이게 합니다. 레거시 장비와의 연동에 쓰입니다.

| 모드 | 격리 | 성능 | 포트 매핑 | 주 용도 |
| --- | --- | --- | --- | --- |
| bridge | O | NAT 오버헤드 있음 | 필요 (-p) | 일반적인 단일 호스트 배포 |
| host | X | 가장 빠름 | 불필요 | 고처리량 네트워크 워크로드 |
| overlay | O | VXLAN 오버헤드 | 필요 | 멀티 호스트 클러스터 |
| none | 완전 격리 | - | - | 네트워크 불필요 작업 |

docker-compose를 쓰면 프로젝트마다 자동으로 사용자 정의 bridge 네트워크가 생성되므로, 서비스 이름(web, db 등)으로 바로 통신할 수 있습니다. 별도 설정 없이 `db:5432`처럼 접근하는 게 그 예입니다.""",
        "follow_up": "그러면 컨테이너끼리 통신할 때는 어떤 모드가 유리해?",
        "search_results": [
            {
                "url": "https://docs.docker.com/network/network-tutorial-standalone/",
                "content": "사용자 정의 브리지 네트워크에서는 컨테이너가 서로의 이름을 DNS로 해석할 수 있습니다. 기본 브리지 네트워크에서는 --link 옵션(레거시)을 쓰거나 IP 주소를 직접 사용해야 합니다. 같은 네트워크에 연결된 컨테이너끼리는 모든 포트가 열려 있으며, 다른 네트워크의 컨테이너와는 격리됩니다.",
            },
            {
                "url": "https://docs.docker.com/network/drivers/bridge/",
                "content": "프로덕션에서는 사용자 정의 브리지 네트워크 사용이 권장됩니다. 동일 호스트에서 실행되는 컨테이너 간 통신에 더 나은 격리와 상호 운용성을 제공합니다. 컨테이너 간 통신은 브리지를 통해 직접 이루어지며 NAT를 거치지 않습니다.",
            },
        ],
    },
    {
        "name": "pg_isolation",
        "parent_query": "PostgreSQL 트랜잭션 격리 수준에는 어떤 것들이 있어?",
        "parent_answer": """## 개념

트랜잭션 격리 수준은 동시에 실행되는 트랜잭션이 서로의 변경 사항을 어디까지 볼 수 있는지를 정의합니다. SQL 표준은 Read Uncommitted, Read Committed, Repeatable Read, Serializable 네 가지를 정의하지만, PostgreSQL은 내부적으로 세 가지만 실제로 구현합니다. Read Uncommitted를 요청해도 Read Committed로 동작합니다 (MVCC 구조상 더티 리드 자체가 발생하지 않기 때문).

## 동작 원리

PostgreSQL은 MVCC(다중 버전 동시성 제어)로 격리를 구현합니다. 각 트랜잭션은 스냅샷을 통해 데이터를 봅니다.

- **Read Committed (기본값)**: 각 쿼리가 시작될 때마다 새 스냅샷을 찍습니다. 같은 트랜잭션 안에서도 쿼리 사이에 다른 트랜잭션이 커밋한 변경이 보일 수 있습니다 (non-repeatable read 발생 가능).
- **Repeatable Read**: 트랜잭션 시작 시점의 스냅샷 하나를 끝까지 사용합니다. 트랜잭션 도중 다른 커밋이 보이지 않습니다. 직렬화 충돌이 감지되면 에러를 내고 재시도가 필요합니다.
- **Serializable**: Repeatable Read에 SSI(Serializable Snapshot Isolation)를 더해, 트랜잭션들을 순차 실행한 것과 동일한 결과를 보장합니다. 위반이 감지되면 serialization_failure 에러가 발생합니다.

## 코드 예시

```sql
-- 현재 격리 수준 확인
SHOW transaction_isolation;  -- 기본: read committed

-- 트랜잭션 단위로 격리 수준 지정
BEGIN ISOLATION LEVEL REPEATABLE READ;
SELECT balance FROM accounts WHERE id = 1;
-- ... 이 트랜잭션 안에서는 시작 시점 스냅샷만 보임
COMMIT;

-- Serializable: 충돌 시 에러가 나므로 애플리케이션에서 재시도 필요
BEGIN ISOLATION LEVEL SERIALIZABLE;
UPDATE accounts SET balance = balance - 100 WHERE id = 1;
COMMIT;  -- ERROR: could not serialize access 가능
```

## 주의사항

- Repeatable Read 이상에서는 직렬화 실패 에러(40001)가 정상 동작입니다. 재시도 로직 없이 쓰면 간헐적 실패로 보입니다.
- 격리 수준을 올릴수록 동시성 처리량이 떨어질 수 있으므로, 필요한 트랜잭션에만 선택적으로 적용하는 것이 일반적입니다.
- Django의 ATOMIC_REQUESTS나 transaction.atomic()은 격리 수준을 바꾸지 않습니다. DB 기본값(Read Committed)을 그대로 사용합니다.

## 관련 개념: 격리 수준별 발생 가능한 이상 현상

| 이상 현상 | Read Committed | Repeatable Read | Serializable |
| --- | --- | --- | --- |
| Dirty Read | 불가능 | 불가능 | 불가능 |
| Non-repeatable Read | 가능 | 불가능 | 불가능 |
| Phantom Read | 가능 | 불가능* | 불가능 |
| Serialization Anomaly | 가능 | 가능 | 불가능 |

*PostgreSQL의 Repeatable Read는 SQL 표준과 달리 phantom read도 막습니다. 스냅샷 하나를 끝까지 쓰기 때문입니다.

격리 수준을 전역으로 바꾸려면 postgresql.conf의 default_transaction_isolation을 수정하거나, Django라면 DATABASES OPTIONS에 지정할 수 있습니다:

```python
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'OPTIONS': {
            # 커넥션 기본 격리 수준을 변경 (psycopg)
            'isolation_level': 2,  # ISOLATION_LEVEL_REPEATABLE_READ
        },
    }
}
```

다만 전역 변경은 모든 트랜잭션의 직렬화 실패 가능성을 높이므로, 정합성이 중요한 일부 트랜잭션에만 BEGIN ISOLATION LEVEL로 지정하는 편이 일반적입니다.""",
        "follow_up": "그중에서 기본값을 그대로 쓰면 어떤 문제가 생길 수 있어?",
        "search_results": [
            {
                "url": "https://www.postgresql.org/docs/current/transaction-iso.html",
                "content": "Read Committed에서는 SELECT 쿼리가 쿼리 시작 직전까지 커밋된 데이터만 봅니다. 그러나 같은 트랜잭션 내에서 연속된 두 SELECT가 서로 다른 데이터를 볼 수 있습니다. 다른 트랜잭션이 그 사이에 커밋했기 때문입니다. UPDATE나 DELETE는 대상 행이 다른 트랜잭션에 의해 변경 중이면 그 트랜잭션의 커밋을 기다린 후 갱신된 행에 조건을 재평가합니다.",
            },
            {
                "url": "https://www.postgresql.org/docs/current/applevel-consistency.html",
                "content": "Read Committed 수준에서 read-modify-write 패턴(읽고 계산해서 다시 쓰는 작업)을 수행하면 lost update가 발생할 수 있습니다. 이를 방지하려면 SELECT FOR UPDATE로 행을 잠그거나, 더 높은 격리 수준을 사용하거나, 원자적 UPDATE 문 하나로 작성해야 합니다.",
            },
        ],
    },
]


# ── 프롬프트 빌드: learnlog_service.generate_answer_stream과 동일 골격 ──
# ⚠️ 프로덕션 프롬프트(learnlog_service.py)가 바뀌면 여기도 함께 수정할 것.
INSTRUCTIONS = "형식: 개념 → 동작 원리 → 코드 예시 → 주의사항. 코드에 주석 포함."

CONDITIONS = ["baseline", "q_only", "q_a500", "q_a1000", "q_full"]


def build_context_block(pair, condition):
    """조건별 '이전 대화' 블록 생성. baseline은 None."""
    if condition == "baseline":
        return None
    if condition == "q_only":
        return f"[이전 질문] {pair['parent_query']}"
    limits = {"q_a500": 500, "q_a1000": 1000, "q_full": None}
    answer = pair["parent_answer"]
    if limits[condition] is not None:
        answer = answer[:limits[condition]]
    return f"[이전 질문] {pair['parent_query']}\n[이전 답변] {answer}"


def build_prompt(pair, condition):
    search_context = "\n".join(
        f"[{r['url']}] {r['content'][:200]}"
        for r in pair["search_results"][:2]
    )
    context_block = build_context_block(pair, condition)
    conversation = f"이전 대화:\n{context_block}\n\n" if context_block else ""

    return f"""개발 질문에 한국어로 답변하세요.

{conversation}질문: {pair['follow_up']}

참고:
{search_context}

{INSTRUCTIONS}"""


def call_mistral_stream(prompt):
    """스트리밍 호출로 TTFT와 총 시간을 함께 측정한다."""
    start = time.time()
    ttft = None
    chunks = []
    usage = None

    stream = client.chat.stream(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
        max_tokens=2000,
    )
    for event in stream:
        chunk = event.data.choices[0].delta.content
        if chunk:
            if ttft is None:
                ttft = time.time() - start
            chunks.append(chunk)
        if getattr(event.data, "usage", None):
            usage = event.data.usage

    total = time.time() - start
    answer = "".join(chunks).strip()
    in_tok = usage.prompt_tokens if usage else None
    out_tok = usage.completion_tokens if usage else None
    return {
        "ttft": ttft,
        "total": total,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "answer": answer,
    }


def main():
    RESULTS_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    jsonl_path = RESULTS_DIR / f"followup_{ts}.jsonl"

    print("=" * 70)
    print("꼬리질문 컨텍스트 벤치마크")
    print(f"  조건 {len(CONDITIONS)} × 질문쌍 {len(PAIRS)} × {ROUNDS}회 = "
          f"{len(CONDITIONS) * len(PAIRS) * ROUNDS} calls")
    print(f"  원시 결과: {jsonl_path}")
    print("=" * 70)

    records = []
    for condition in CONDITIONS:
        for pair in PAIRS:
            prompt = build_prompt(pair, condition)
            for i in range(ROUNDS):
                label = f"{condition} / {pair['name']} / round{i + 1}"
                try:
                    r = call_mistral_stream(prompt)
                except Exception as e:
                    print(f"  ❌ {label}: {e}")
                    time.sleep(CALL_GAP_SEC)
                    continue

                record = {
                    "condition": condition,
                    "pair": pair["name"],
                    "round": i + 1,
                    "prompt_chars": len(prompt),
                    **r,
                }
                records.append(record)
                with open(jsonl_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")

                decode_tps = (
                    r["output_tokens"] / (r["total"] - r["ttft"])
                    if r["output_tokens"] and r["ttft"] is not None and r["total"] > r["ttft"]
                    else 0
                )
                print(f"  {label}: TTFT={r['ttft']:.2f}s 총={r['total']:.2f}s | "
                      f"in={r['input_tokens']} out={r['output_tokens']} | {decode_tps:.0f} tok/s")
                time.sleep(CALL_GAP_SEC)

    # ── 조건별 요약 ──────────────────────────────────────────────
    print(f"\n{'=' * 70}")
    print("조건별 평균 (질문쌍·라운드 통합)")
    print(f"{'=' * 70}")
    print(f"  {'조건':<10} {'TTFT':>7} {'총시간':>8} {'입력tok':>8} {'출력tok':>8}")
    baseline_ttft = None
    for condition in CONDITIONS:
        rows = [r for r in records if r["condition"] == condition and r["ttft"] is not None]
        if not rows:
            print(f"  {condition:<10} (데이터 없음)")
            continue
        avg = lambda key: sum(r[key] or 0 for r in rows) / len(rows)
        ttft, total = avg("ttft"), avg("total")
        if condition == "baseline":
            baseline_ttft = ttft
        delta = f" (+{ttft - baseline_ttft:.2f}s)" if baseline_ttft is not None and condition != "baseline" else ""
        print(f"  {condition:<10} {ttft:>6.2f}s {total:>7.2f}s {avg('input_tokens'):>8.0f} "
              f"{avg('output_tokens'):>8.0f}{delta}")

    # ── 맥락 해석 수동 판정용 미리보기 ───────────────────────────
    print(f"\n{'=' * 70}")
    print("맥락 해석 판정용 미리보기 (전문은 JSONL에서 확인)")
    print("  → 꼬리질문의 '그러면/그중에서'를 부모 주제로 해석했는지 직접 확인할 것")
    print(f"{'=' * 70}")
    for condition in CONDITIONS:
        for pair in PAIRS:
            rows = [r for r in records
                    if r["condition"] == condition and r["pair"] == pair["name"]]
            if rows:
                preview = rows[0]["answer"][:150].replace("\n", " ")
                print(f"\n[{condition} / {pair['name']}]\n  {preview}...")


if __name__ == "__main__":
    main()
