# 기술 스택별 검색 도메인 자동 매핑 및 출처 판단 개선

[`bb8c09b`](https://github.com/aengu/learn-log/commit/bb8c09bf47fd82de0694727b7293465d5246ab53)

검색 도메인 하드코딩과 문자열 기반 출처 판별의 한계를 개선

| 항목 | 내용 |
| --- | --- |
| 목적 | 질문에 맞는 공식 문서 도메인 자동 선택 + 출처 판별 정확도 향상 |
| 방식 | 기술-도메인 매핑 테이블(TECH_DOCS_MAP) 도입 |

---

## 요구사항

- 질문에 포함된 기술 스택을 인식해 **해당 공식 문서 도메인만** 검색한다.
- 한글 키워드("도커", "파이썬" 등)도 인식해야 한다.
- 매핑에 없는 기술을 질문하면 **도메인 제한 없이 전체 검색**한다.
- 검색 결과 URL의 출처 유형(공식 문서/블로그 등)을 **등록된 도메인 목록 기반**으로 판별한다.
- 도메인 매핑 데이터를 **한 곳에서 관리**하여 검색과 출처 판별이 동시에 갱신되도록 한다.

---

## 이전 코드의 문제점

### 1. search_official_docs() - 4개 도메인 하드코딩

```python
# 변경 전
results = self.tavily_client.search(
    query=query,
    search_depth="advanced",
    max_results=5,
    include_domains=[
        "docs.docker.com",
        "docs.python.org",
        "docs.djangoproject.com",
        "github.com",
    ]
)
```

`include_domains`는 Tavily API에서 **검색 결과를 특정 도메인으로 제한**하는 파라미터다. 블로그나 비공식 자료 대신 공식 문서만 가져오기 위해 사용한다.

문제는 4개 도메인이 하드코딩되어 있어서:

| 문제 | 예시 |
| --- | --- |
| 관련 없는 도메인에서 검색 | "React hooks란?" → `docs.docker.com`에서도 검색 시도 |
| 지원하지 않는 기술은 결과 없음 | "Kubernetes Pod 구조" → k8s 도메인이 없어서 `github.com` 결과만 반환 |
| 도메인 추가 시 코드 수정 필요 | 새 기술 추가마다 `services.py`를 직접 수정해야 함 |

### 2. _determine_source_type() - 문자열 매칭 기반 출처 판별

```python
# 변경 전
elif any(domain in url_lower for domain in ['docs.', 'documentation', 'doc.']):
    return 'official'
```

URL에 `docs.`가 포함되어 있으면 공식 문서로 판별하는 방식:

- **미탐**: `react.dev`, `kubernetes.io`, `go.dev/doc` → `docs.`가 없어서 `other`로 분류
- **오탐**: `docs.some-random-blog.com` → 비공식 사이트인데 `official`로 분류

---

## 구현

### domains.py - 기술-도메인 매핑 테이블

검색 도메인 선택과 출처 판별이 **같은 데이터**를 필요로 하므로, `TECH_DOCS_MAP`이라는 단일 매핑 테이블을 만들고 두 기능이 이를 공유하도록 설계했다.

```python
TECH_DOCS_MAP = {
    # 컨테이너 / 인프라
    'docker': ['docs.docker.com'],
    '도커': ['docs.docker.com'],
    'kubernetes': ['kubernetes.io'],
    '쿠버네티스': ['kubernetes.io'],
    'k8s': ['kubernetes.io'],

    # 언어
    'python': ['docs.python.org'],
    '파이썬': ['docs.python.org'],
    'javascript': ['developer.mozilla.org'],
    'typescript': ['typescriptlang.org/docs'],
    'go': ['go.dev/doc'],
    'rust': ['doc.rust-lang.org'],

    # 프레임워크
    'django': ['docs.djangoproject.com'],
    'react': ['react.dev'],
    'vue': ['vuejs.org'],
    'spring': ['docs.spring.io'],

    # 데이터베이스
    'postgresql': ['postgresql.org/docs'],
    'redis': ['redis.io/docs'],
    'mongodb': ['mongodb.com/docs'],
    # ... 총 60개+ 기술 스택 (한글 키워드 포함)
}
```

- 영어/한글 키워드를 모두 등록하여 "도커 네트워크"와 "docker network" 둘 다 인식
- 하나의 기술에 여러 도메인 매핑 가능 (예: `'linux': ['man7.org', 'kernel.org/doc']`)

### get_domains_for_query() - 질문에서 도메인 추출

```python
def get_domains_for_query(query: str) -> list[str] | None:
    query_lower = query.lower()
    domains = []

    for tech, urls in TECH_DOCS_MAP.items():
        if tech in query_lower:
            domains.extend(urls)

    # 중복 제거
    domains = list(dict.fromkeys(domains))

    # GitHub는 항상 포함 (유용한 예제/이슈가 많음)
    if 'github.com' not in domains:
        domains.append('github.com')

    return domains if domains else None
```

- `dict.fromkeys()`로 중복 제거: "도커"와 "docker" 둘 다 매칭되면 `docs.docker.com`이 2번 들어가는 걸 방지
- GitHub는 항상 포함: 공식 문서 외에도 예제 코드나 이슈가 유용하므로
- 매칭되는 기술이 없으면 `None` 반환 → `include_domains` 파라미터 자체를 빼서 전체 검색

### is_official_doc() - 도메인 기반 출처 판별

```python
def is_official_doc(url: str) -> bool:
    url_lower = url.lower()
    official_domains = {d for domains in TECH_DOCS_MAP.values() for d in domains}
    return any(domain in url_lower for domain in official_domains)
```

`TECH_DOCS_MAP`의 모든 도메인을 set으로 모아서 URL과 대조한다. `docs.` 문자열 매칭 대신 **등록된 공식 도메인 목록**으로 판별하므로:

- `react.dev/reference/react/useEffect` → `react.dev`가 등록되어 있으므로 `official`
- `docs.random-blog.com` → 등록되어 있지 않으므로 `official`이 아님

### services.py - 변경된 부분

**search_official_docs() 변경:**

```python
# 변경 전
results = self.tavily_client.search(
    query=query,
    search_depth="advanced",
    max_results=5,
    include_domains=[
        "docs.docker.com",
        "docs.python.org",
        "docs.djangoproject.com",
        "github.com",
    ]
)

# 변경 후
from .domains import get_domains_for_query

domains = get_domains_for_query(query)

search_params = {
    'query': query,
    'search_depth': 'advanced',
    'max_results': 5,
}
if domains:
    search_params['include_domains'] = domains

results = self.tavily_client.search(**search_params)
```

- 하드코딩된 리스트 → `get_domains_for_query()`로 동적 생성
- `domains`가 `None`이면 `include_domains` 자체를 빼서 전체 웹 검색으로 fallback

**_determine_source_type() 변경:**

```python
# 변경 전
elif any(domain in url_lower for domain in ['docs.', 'documentation', 'doc.']):
    return 'official'

# 변경 후
from .domains import is_official_doc

elif is_official_doc(url):
    return 'official'
```

---

## 동작 비교

```
질문: "Docker 네트워크 bridge와 host 차이"

변경 전: include_domains = [docs.docker.com, docs.python.org, docs.djangoproject.com, github.com]
  → docs.python.org, docs.djangoproject.com은 불필요한 검색

변경 후: include_domains = [docs.docker.com, github.com]
  → Docker 공식 문서에 집중
```

```
질문: "React useEffect 사용법"

변경 전: include_domains = [docs.docker.com, docs.python.org, docs.djangoproject.com, github.com]
  → React 도메인 자체가 없어서 유용한 결과 없음

변경 후: include_domains = [react.dev, github.com]
  → React 공식 문서에서 정확한 결과 반환
```

```
질문: "gRPC란 무엇인가"

변경 전: include_domains = [docs.docker.com, docs.python.org, docs.djangoproject.com, github.com]
  → 관련 없는 도메인에서 검색

변경 후: include_domains 없음 (TECH_DOCS_MAP에 gRPC가 없으므로)
  → 전체 웹 검색으로 fallback, 오히려 더 나은 결과
```

---

## 파일 구조

```
search/
├── domains.py     # [신규] TECH_DOCS_MAP, get_domains_for_query(), is_official_doc()
└── services.py    # [수정] search_official_docs(), _determine_source_type()
```
