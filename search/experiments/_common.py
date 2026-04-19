"""
프롬프트 검증 공통 모듈.

- 쿼리 세트, 프롬프트 빌더, 휴리스틱 검증기, Groq 실행기, 리포트 출력.
- validate_baseline.py / validate_regression.py에서 공유.
"""

import json
import re
import textwrap
from datetime import datetime
from pathlib import Path

from groq import Groq

# ── 설정 ───────────────────────────────────────────────────────────

MODEL = "llama-3.3-70b-versatile"
TEMPERATURE = 0.4
MAX_TOKENS = 1500

RESULTS_DIR = Path(__file__).parent / "results"

# rate limit msg 판별
RATE_LIMIT_HINTS = ("rate limit", "429", "quota", "tokens per day", "requests per day")

# ── 쿼리 세트 ───────────────────────────────────────────────────────
# 숫자형 답(python_gc, binary_search, http_redirect)과 서술형 답(sql_index, jwt)을 섞음.

QUERIES: list[dict] = [
    {
        "id": "python_gc",
        "query": "파이썬 가비지 컬렉션과 참조 카운팅은 어떻게 동작하나요?",
        "response": textwrap.dedent("""\
            파이썬은 메모리 관리를 위해 참조 카운팅(reference counting)과 가비지 컬렉션(garbage collection) 두 가지를 사용합니다.

            1. 참조 카운팅: 모든 객체는 자신을 가리키는 참조의 수를 추적합니다.
               - 객체가 생성되면 참조 횟수는 1입니다.
               - 다른 변수가 같은 객체를 참조하면 횟수가 증가합니다.
               - 참조가 삭제되거나 스코프를 벗어나면 횟수가 감소합니다.
               - 횟수가 0이 되면 즉시 메모리에서 해제됩니다.

            예시 코드:
            ```python
            import sys
            obj = []           # 참조 횟수: 1
            ref = obj          # 참조 횟수: 2
            print(sys.getrefcount(obj))  # 3 (함수 인자로 임시 참조 +1)
            del ref            # 참조 횟수: 1
            del obj            # 참조 횟수: 0 → 메모리 해제
            ```

            2. 순환 참조 가비지 컬렉션: 참조 카운팅만으로는 순환 참조를 해결할 수 없습니다.
               - a = []; b = []; a.append(b); b.append(a) 같은 경우
               - gc 모듈이 주기적으로 순환 참조를 탐지하고 해제합니다.
               - 세대별(generational) 가비지 컬렉션을 사용합니다.
        """),
    },
    {
        "id": "binary_search",
        "query": "이진 탐색의 동작 원리와 시간 복잡도를 단계별로 설명해주세요",
        "response": textwrap.dedent("""\
            이진 탐색(Binary Search)은 정렬된 배열에서 특정 값을 찾는 알고리즘으로 시간 복잡도는 O(log n)입니다.

            동작 단계:
            1. left=0, right=len(arr)-1로 포인터 초기화
            2. mid = (left + right) // 2 계산
            3. arr[mid]와 target 비교:
               - arr[mid] == target: 인덱스 mid 반환 (탐색 성공)
               - arr[mid] < target: left = mid + 1 (오른쪽 절반 탐색)
               - arr[mid] > target: right = mid - 1 (왼쪽 절반 탐색)
            4. left > right가 되면 탐색 실패, -1 반환

            예시: arr=[1,3,5,7,9,11,13,15], target=11
            - 1차: left=0, right=7, mid=3, arr[3]=7 < 11 → left=4
            - 2차: left=4, right=7, mid=5, arr[5]=11 == 11 → 인덱스 5 반환

            배열 크기 n=8이면 최대 log2(8)=3번의 비교로 탐색 완료.
            배열 크기 n=1024면 최대 10번 비교로 충분합니다.
        """),
    },
    {
        "id": "http_redirect",
        "query": "HTTP 301과 302 리다이렉트의 차이와 브라우저 동작 흐름",
        "response": textwrap.dedent("""\
            HTTP 리다이렉트는 서버가 요청된 리소스의 새 위치를 알려주는 응답입니다.

            301 Moved Permanently (영구 이동):
            - 상태 코드 301 반환
            - 응답에 Location 헤더로 새 URL 포함
            - 브라우저가 캐시에 영구 저장 (다음 요청부터 새 URL로 직접 요청)
            - 검색 엔진이 페이지 랭킹을 새 URL로 이전

            302 Found (임시 이동):
            - 상태 코드 302 반환
            - 응답에 Location 헤더로 새 URL 포함
            - 브라우저가 캐시하지 않음 (매번 원래 URL로 요청)
            - 검색 엔진 페이지 랭킹 유지

            브라우저 동작 흐름 (301 예시):
            1. 클라이언트가 GET /old-page 요청
            2. 서버가 301 + Location: /new-page 응답
            3. 브라우저가 Location 헤더를 읽고 자동으로 GET /new-page 재요청
            4. 서버가 200 OK + 실제 콘텐츠 응답
            5. 브라우저가 301 응답을 캐시에 저장

            리다이렉트 체인이 너무 길면 브라우저는 10회 이상에서 중단하고 ERR_TOO_MANY_REDIRECTS 에러를 표시합니다.
        """),
    },
    {
        "id": "sql_btree_index",
        "query": "SQL B-tree 인덱스는 어떻게 동작하며 검색 속도를 높이나요?",
        "response": textwrap.dedent("""\
            B-tree 인덱스는 대부분의 RDBMS가 기본으로 사용하는 인덱스 구조로, 균형 잡힌 다진 트리입니다.

            구조:
            - 루트 노드, 내부 노드(branch), 리프 노드(leaf)로 구성
            - 각 노드는 디스크 페이지(보통 8KB)에 저장
            - 내부 노드는 정렬된 키 값과 자식 노드 포인터를 가짐
            - 리프 노드는 실제 데이터 행의 위치(ROWID 또는 Primary Key)를 가리킴
            - 리프 노드끼리는 linked list로 연결되어 있어 범위 스캔이 빠름

            검색 동작 (WHERE id = 42 예시):
            1. 루트 노드를 메모리에 로드
            2. 키 값을 비교해 42가 속한 자식 노드 포인터 선택
            3. 해당 내부 노드를 로드하고 다시 비교
            4. 리프 노드에 도달하면 ROWID를 얻어 실제 행 조회

            속도 향상 원리:
            - Full Table Scan이 O(n)인 반면 B-tree는 O(log n)
            - 노드 깊이가 보통 3~4단계라 디스크 I/O가 매우 적음
            - 정렬된 구조라 범위 검색(BETWEEN, <, >)도 빠름

            단점: INSERT/UPDATE/DELETE 시 트리 재균형 비용이 발생해 쓰기 성능은 다소 떨어집니다.
        """),
    },
    {
        "id": "jwt_verification",
        "query": "JWT 토큰 검증 과정을 단계별로 설명해주세요",
        "response": textwrap.dedent("""\
            JWT(JSON Web Token)는 헤더.페이로드.서명 세 부분이 점(.)으로 연결된 토큰입니다.

            구조:
            - Header: {"alg": "HS256", "typ": "JWT"} — 서명 알고리즘 명시
            - Payload: {"sub": "user123", "exp": 1712345678, "role": "admin"} — 클레임
            - Signature: HMAC_SHA256(base64(header) + "." + base64(payload), secret_key)

            서버 측 검증 단계:
            1. 요청의 Authorization 헤더에서 "Bearer <token>" 추출
            2. 토큰을 점(.) 기준으로 3분할 (header, payload, signature)
            3. header를 base64 디코딩해 alg 확인 (none 알고리즘 공격 방지)
            4. header + "." + payload를 비밀키로 다시 서명해 signature와 비교
               - 일치하지 않으면 401 Unauthorized 반환
            5. payload를 base64 디코딩해 exp 클레임 확인
               - 현재 시각이 exp보다 크면 만료 → 401 반환
            6. 필요 시 iss(발급자), aud(대상) 클레임 추가 검증
            7. 모든 검증 통과 시 payload의 sub(사용자 ID)로 요청 처리

            비대칭 알고리즘(RS256) 사용 시에는 공개키로 서명 검증을 수행합니다.
        """),
    },
]


def get_query(query_id):
    for q in QUERIES:
        if q["id"] == query_id:
            return q
    raise KeyError(f"unknown query_id: {query_id}")


# ── 프롬프트 빌더 ──────────────────────────────────────────────────

# !!!build_new_prompt는 services.py의 _gen_path_trace 프롬프트와 동기화 유지 필수.
# services.py 프롬프트 수정 시 반드시 아래 템플릿도 함께 수정할 것.
# build_old_prompt: correct_index 규칙 추가 이전의 베이스라인 프롬프트 (고정, 수정 금지).
# build_v1_prompt: correct_index 규칙 추가 직후의 v1 프롬프트 (고정, 수정 금지).
# v2 실험 결과 v1보다 나빴다면 롤백 또는 재실행을 위해 보존.


def build_new_prompt(query, response):
    """현재 프로덕션(services.py)의 프롬프트 = v2. shifted choices 예시 + self-check."""
    return textwrap.dedent(f"""\
        다음 학습 내용을 바탕으로 "경로추적" 유형 연습문제를 만들어주세요.

        학습 내용:
        질문: {query}
        답변: {response[:1000]}

        "경로추적" 유형: 코드나 시스템의 실행 흐름을 단계별로 추적하며 각 단계에서 객관식으로 답합니다.

        JSON으로만 응답하세요 (```없이):
        {{
          "scenario": "추적할 시나리오 설명",
          "steps": [
            {{
              "question": "이 단계에서 무슨 일이 일어나는가?",
              "choices": ["선택지A", "선택지B", "선택지C", "선택지D"],
              "correct_index": 0,
              "explanation": "왜 이것이 정답인지 설명"
            }}
          ]
        }}
        steps는 3~5개로 구성하세요.

        ⚠️ correct_index 규칙 (반드시 준수):
        - correct_index는 choices 배열의 0-based 인덱스입니다. (첫 요소 = 0)
        - choices[correct_index]의 값이 정답 값과 정확히 같아야 합니다.
        - 정답 '값(value)'과 '인덱스(index)'는 다릅니다.
          특히 choices가 1부터 시작하는 경우 헷갈리기 쉬우니 아래 예시를 반드시 확인하세요.

          예 A: choices=["1","2","3","4"], 정답 값="2"
                → choices[1]="2" → correct_index = 1 ✅
                → choices[2]="3" → correct_index = 2 는 틀림 ❌

          예 B: choices=["1","2","3","4"], 정답 값="1"
                → choices[0]="1" → correct_index = 0 ✅

        - correct_index를 정한 뒤, choices[correct_index]를 꺼내서 정답 값과 같은지 다시 확인하세요.
    """).strip()


def build_v1_prompt(query, response):
    """v1 프롬프트 (보존용). shifted choices 예시 도입 전 버전 = value==index 예시만."""
    return textwrap.dedent(f"""\
        다음 학습 내용을 바탕으로 "경로추적" 유형 연습문제를 만들어주세요.

        학습 내용:
        질문: {query}
        답변: {response[:1000]}

        "경로추적" 유형: 코드나 시스템의 실행 흐름을 단계별로 추적하며 각 단계에서 객관식으로 답합니다.

        JSON으로만 응답하세요 (```없이):
        {{
          "scenario": "추적할 시나리오 설명",
          "steps": [
            {{
              "question": "이 단계에서 무슨 일이 일어나는가?",
              "choices": ["선택지A", "선택지B", "선택지C", "선택지D"],
              "correct_index": 0,
              "explanation": "왜 이것이 정답인지 설명"
            }}
          ]
        }}
        steps는 3~5개로 구성하세요.

        ⚠️ correct_index 규칙 (반드시 준수):
        - correct_index는 choices 배열의 0-based 인덱스입니다.
        - choices[correct_index]의 값이 실제 정답과 반드시 일치해야 합니다.
        - 예: choices가 ["0","1","2","3"]이고 정답이 "2"이면 correct_index는 2입니다.
        - 정답 값(value)과 인덱스(index)를 혼동하지 마세요.
    """).strip()


def build_old_prompt(query, response):
    return textwrap.dedent(f"""\
        다음 학습 내용을 바탕으로 "경로추적" 유형 연습문제를 만들어주세요.

        학습 내용:
        질문: {query}
        답변: {response[:1000]}

        "경로추적" 유형: 코드나 시스템의 실행 흐름을 단계별로 추적하며 각 단계에서 객관식으로 답합니다.

        JSON으로만 응답하세요 (```없이):
        {{
          "scenario": "추적할 시나리오 설명",
          "steps": [
            {{
              "question": "이 단계에서 무슨 일이 일어나는가?",
              "choices": ["선택지A", "선택지B", "선택지C", "선택지D"],
              "correct_index": 0,
              "explanation": "왜 이것이 정답인지 설명"
            }}
          ]
        }}
        steps는 3~5개로 구성하세요.
    """).strip()


# ── Groq 호출 ─────────────────────────────────────────────────────


class RateLimitHit(Exception):
    pass


def call_groq(client, prompt):
    """Groq 호출 → (parsed_json, raw_text). 실패 시 (None, raw_text).

    429/할당량 에러는 RateLimitHit으로 즉시 전파 (배치 중단 용도).
    """
    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
        )
    except Exception as e:
        msg = str(e).lower()
        if any(h in msg for h in RATE_LIMIT_HINTS):
            raise RateLimitHit(str(e)) from e
        return None, f"[API 에러] {e}"

    raw = response.choices[0].message.content.strip()
    cleaned = raw
    if cleaned.startswith("```"):
        parts = cleaned.split("```")
        cleaned = parts[1] if len(parts) > 1 else cleaned
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]

    try:
        return json.loads(cleaned.strip()), raw
    except json.JSONDecodeError as e:
        return None, f"[JSON 파싱 실패] {e} | raw={raw[:200]}"


# ── 검증 휴리스틱 ───────────────────────────────────────────────────


def is_numeric_choices(choices):
    """모든 choice가 numeric인지 확인, 숫자 + 선택적 공백 + 짧은 단위(0~2글자)"""
    for c in choices:
        if not re.fullmatch(r"\d+\s*\S{0,2}", str(c).strip()):
            return False
    return True


def extract_numeric(text):
    """문자열 시작 부분에서 숫자만 추출"""
    m = re.match(r"(\d+)", str(text).strip())
    return m.group(1) if m else None


def extract_answer_from_explanation(explanation):
    """explanation에서 '최종 정답' 숫자를 정규식으로 추출.

    개선 이력:
      - 초기 버전은 첫 매칭을 반환 → 서사형 explanation의 중간 단계 숫자를
        정답으로 오인하는 false positive가 baseline에서 75% 발생.
      - v2: (1) '따라서/최종적으로/정답은' 같은 결론 지시어 뒤의 숫자를 우선,
            (2) 없으면 기존 패턴 중 '마지막' 매칭을 반환.
    """
    # 1순위: 결론 지시어
    conclusion_patterns = [
        r"(?:따라서|결국|최종적으로|최종|결론적으로|그러므로|즉)[^\n]*?(\d+)",
        r"정답은\s*[\"']?(\d+)",
        r"답은\s*[\"']?(\d+)",
    ]
    for pat in conclusion_patterns:
        matches = re.findall(pat, explanation)
        if matches:
            return matches[-1]

    # 2순위: 서술 패턴 — 마지막 매칭을 채택
    legacy_patterns = [
        r"(?:횟수는|값은|결과는|카운트는|개수는)\s*[\"']?(\d+)",
        r"(\d+)\s*(?:가|이)\s*(?:됩니다|됨|정답|맞습니다|올바른|된다)",
        r"(\d+)\s*(?:개|번|회)?\s*(?:가|이)\s*(?:됩니다|됨|됩|된다)",
    ]
    last_hit = None
    for pat in legacy_patterns:
        for m in re.finditer(pat, explanation):
            last_hit = m.group(1)
    return last_hit


def check_step(step):
    """단일 step의 correct_index 정합성 검사
        1. correct_index/choices 누락? → skipped
        2. correct_index가 범위 밖? → has_error (out_of_range)
        3. explanation에서 정답 추출 불가? → skipped
        4. choices가 숫자형? → numeric 전략
            - choices[correct_index]의 숫자 == 추출한 정답? → OK
            - 아니면 → has_error (index_mismatch)
        5. choices가 서술형? → descriptive 전략
            - 정답 숫자가 choices[correct_index] 안에 포함? → OK
            - 다른 choice에 더 명확히 포함? → has_error
            - 복수 choice에 모호하게 포함? → skipped (판단 보류) """
    choices = step.get("choices", [])
    correct_index = step.get("correct_index")
    explanation = step.get("explanation", "")

    if correct_index is None or not choices:
        return {"has_error": False, "error_type": None,
                "strategy": "skipped", "detail": "구조 불완전"}

    if not (0 <= correct_index < len(choices)):
        return {"has_error": True, "error_type": "out_of_range",
                "strategy": "numeric",
                "detail": f"correct_index={correct_index}, len(choices)={len(choices)}"}

    expected = extract_answer_from_explanation(explanation)
    if expected is None:
        return {"has_error": False, "error_type": None,
                "strategy": "skipped", "detail": "explanation에서 정답 추출 불가"}

    # 전략 1: 숫자형 choices
    if is_numeric_choices(choices):
        chosen_num = extract_numeric(choices[correct_index])
        if chosen_num == expected:
            return {"has_error": False, "error_type": None,
                    "strategy": "numeric",
                    "detail": f"OK: choices[{correct_index}]='{chosen_num}' == '{expected}'"}

        correct_at = None
        for i, c in enumerate(choices):
            if extract_numeric(c) == expected:
                correct_at = i
                break
        detail = (f"MISMATCH: explanation 정답='{expected}', "
                  f"choices[{correct_index}]='{choices[correct_index]}'")
        if correct_at is not None:
            detail += f" (정답은 choices[{correct_at}]에 존재)"
        return {"has_error": True, "error_type": "index_mismatch",
                "strategy": "numeric", "detail": detail}

    # 전략 2: 서술형 choices
    chosen_text = str(choices[correct_index])
    chosen_contains = expected in chosen_text

    better_match = None
    for i, c in enumerate(choices):
        if i == correct_index:
            continue
        c_str = str(c)
        if expected in c_str:
            answer_patterns = [
                f"{expected}가 됩", f"{expected}이 됩",
                f"{expected}입니다", f"{expected}이다", f"{expected}로 ",
                f"횟수가 {expected}", f"횟수는 {expected}",
                f"카운트가 {expected}", f"카운트는 {expected}",
            ]
            if any(p in c_str for p in answer_patterns):
                better_match = i
                break

    if chosen_contains and better_match is None:
        return {"has_error": False, "error_type": None,
                "strategy": "descriptive",
                "detail": f"OK: choices[{correct_index}] 정답 '{expected}' 포함"}

    if better_match is not None and not chosen_contains:
        detail = (
            f"MISMATCH: explanation 정답='{expected}', "
            f"choices[{correct_index}]='{chosen_text[:40]}' (정답 미포함), "
            f"choices[{better_match}]='{str(choices[better_match])[:40]}' (정답 포함)"
        )
        return {"has_error": True, "error_type": "index_mismatch",
                "strategy": "descriptive", "detail": detail}

    if better_match is not None and chosen_contains:
        return {"has_error": False, "error_type": None,
                "strategy": "skipped",
                "detail": f"복수 choice에 정답 '{expected}' 포함 — 판단 보류"}

    return {"has_error": False, "error_type": None,
            "strategy": "skipped",
            "detail": f"정답 '{expected}'이 어떤 choice에도 명확히 없음"}


# ── 실행 & 저장 ─────────────────────────────────────────────────────


def ensure_results_dir():
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    return RESULTS_DIR


def timestamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def append_jsonl(path, record):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def run_batch(
    client,
    prompt_builder,
    queries,
    iterations,
    label,
    jsonl_path,
):
    """쿼리 × iterations 만큼 호출하고 각 호출을 JSONL에 즉시 저장.

    429 감지 시 RateLimitHit 전파 — 호출자가 부분 결과로 리포트 작성.
    """
    total_steps = 0
    error_steps = 0
    skipped_steps = 0
    verified_steps = 0
    parse_failures = 0
    api_failures = 0
    errors_detail = []
    by_strategy = {"numeric": {"total": 0, "errors": 0},
                   "descriptive": {"total": 0, "errors": 0}}
    by_query = {}

    calls_made = 0
    for q in queries:
        qid = q["id"]
        by_query.setdefault(qid, {"total_steps": 0, "error_steps": 0,
                                   "verified_steps": 0, "parse_failures": 0})
        for i in range(iterations):
            calls_made += 1
            print(f"  [{label}] {qid} {i+1}/{iterations} (총 {calls_made})")

            parsed, raw_or_err = call_groq(client, prompt_builder(q["query"], q["response"]))

            append_jsonl(jsonl_path, {
                "label": label,
                "query_id": qid,
                "iteration": i,
                "parsed": parsed,
                "raw_or_err": raw_or_err,
            })

            if parsed is None:
                if raw_or_err and raw_or_err.startswith("[API"):
                    api_failures += 1
                else:
                    parse_failures += 1
                by_query[qid]["parse_failures"] += 1
                continue

            steps = parsed.get("steps", [])
            for step_idx, step in enumerate(steps):
                total_steps += 1
                by_query[qid]["total_steps"] += 1

                check = check_step(step)
                strategy = check.get("strategy", "skipped")

                if strategy == "skipped":
                    skipped_steps += 1
                    continue

                verified_steps += 1
                by_query[qid]["verified_steps"] += 1
                by_strategy[strategy]["total"] += 1

                if check["has_error"]:
                    error_steps += 1
                    by_query[qid]["error_steps"] += 1
                    by_strategy[strategy]["errors"] += 1
                    errors_detail.append(
                        f"{qid} iter{i} step{step_idx} [{strategy}]: {check['detail']}"
                    )

    return {
        "label": label,
        "queries": [q["id"] for q in queries],
        "iterations": iterations,
        "calls_made": calls_made,
        "api_failures": api_failures,
        "parse_failures": parse_failures,
        "total_steps": total_steps,
        "verified_steps": verified_steps,
        "error_steps": error_steps,
        "skipped_steps": skipped_steps,
        "error_rate": (error_steps / verified_steps) if verified_steps > 0 else 0.0,
        "by_strategy": by_strategy,
        "by_query": by_query,
        "errors_detail": errors_detail,
    }


def print_stats(stats):
    verified = stats["verified_steps"]
    print(f"\n  [{stats['label']}]")
    print(f"   쿼리:           {', '.join(stats['queries'])}")
    print(f"   호출 수행:      {stats['calls_made']}  (반복 {stats['iterations']})")
    print(f"   API 실패:       {stats['api_failures']}")
    print(f"   파싱 실패:      {stats['parse_failures']}")
    print(f"   총 step:        {stats['total_steps']}")
    print(f"   검증된 step:    {verified}  (스킵 {stats['skipped_steps']})")
    print(f"   오류 step:      {stats['error_steps']}")
    if verified > 0:
        print(f"   오류율:         {stats['error_rate']:.1%}  ({stats['error_steps']}/{verified})")
    else:
        print(f"   오류율:         N/A (verified=0)")

    for sname, sdata in stats["by_strategy"].items():
        if sdata["total"] > 0:
            sr = sdata["errors"] / sdata["total"]
            print(f"     {sname:12s}: {sdata['errors']}/{sdata['total']} ({sr:.0%})")

    if stats["by_query"]:
        print("   쿼리별:")
        for qid, q in stats["by_query"].items():
            v = q["verified_steps"]
            rate = (q["error_steps"] / v) if v > 0 else 0.0
            tag = f"{rate:.0%}" if v > 0 else "N/A"
            print(f"     {qid:20s}: {q['error_steps']}/{v} ({tag})")

    if stats["errors_detail"]:
        print("   오류 상세:")
        for d in stats["errors_detail"]:
            print(f"     - {d}")


def save_summary(stats_list, json_path):
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "model": MODEL,
        "temperature": TEMPERATURE,
        "batches": stats_list,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
