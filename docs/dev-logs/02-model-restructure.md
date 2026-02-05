# 학습 로그 모델 구조 개선 및 서비스 로직 업데이트

> **커밋**: [`dd1d50e`](https://github.com/aengu/learn-log/commit/dd1d50e38328ffd06ce50a542c0794285f02b8f0)
> **날짜**: 2026-01-30

| 항목 | 내용 |
| --- | --- |
| 목적 | 학습 로그 데이터 모델 정규화 및 서비스 계층 분리 |
| 방식 | Django ORM ManyToMany 관계 + 서비스 패턴 |

---

## 개요

초기에는 `models.py`가 빈 상태(`# Create your models here.`)였고, 질문-답변 데이터를 저장할 구조가 없었다.

이 커밋에서 **3개 모델(Tag, Reference, LearningLog)** 을 설계하고, `services.py`에서 외부 API 호출 → DB 저장까지의 전체 파이프라인을 구현했다.

**변경 전:**
- `models.py`: 빈 파일
- 서비스 로직: 없음

**변경 후:**
- `models.py`: Tag, Reference, LearningLog 3개 모델
- `services.py`: LearnlogService 클래스 (검색 → AI 답변 → 태그 추출 → 마크다운 변환 → DB 저장)

---

## 구조

### 모델 관계도

```
Tag (태그)                    Reference (레퍼런스)
  │                               │
  │ M2M                           │ M2M
  └──────── LearningLog ──────────┘
              (학습 로그)
              │
              ├── query (질문)
              ├── ai_response (AI 답변)
              ├── markdown_content (마크다운)
              ├── is_bookmarked (북마크)
              ├── view_count (조회수)
              └── created_at / updated_at
```

### 서비스 처리 흐름

```
사용자 질문
    │
    ▼
[1] search_official_docs()  ── Tavily API ──▶ 검색 결과
    │
    ▼
[2] generate_answer()       ── Groq API  ──▶ AI 답변
    │
    ▼
[3] extract_tags()          ── Groq API  ──▶ 태그 목록
    │
    ▼
[4] convert_to_markdown()   ── Groq API  ──▶ 마크다운
    │
    ▼
[5] DB 저장 (LearningLog + Reference + Tag)
```

---

## 구현

### 1. models.py - 모델 설계

**Tag 모델**: 태그별 검색 및 통계를 위해 별도 모델로 분리.

```python
class Tag(models.Model):
    name = models.CharField(max_length=50, unique=True, verbose_name="태그명")
    slug = models.SlugField(max_length=50, unique=True, verbose_name="슬러그")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="생성일")

    class Meta:
        ordering = ['name']
```

- `slug`: URL에서 사용할 수 있도록 SlugField 추가 (예: `docker-network`)
- `unique=True`: 동일 태그 중복 생성 방지

**Reference 모델**: 검색 결과로 얻은 공식 문서/블로그 등의 출처를 체계적으로 관리.

```python
class Reference(models.Model):
    url = models.URLField(max_length=500, unique=True, verbose_name="URL")
    title = models.CharField(max_length=300, verbose_name="문서 제목")
    excerpt = models.TextField(verbose_name="핵심 내용 발췌")
    source_type = models.CharField(
        max_length=50,
        choices=[
            ('official', '공식 문서'),
            ('blog', '기술 블로그'),
            ('stackoverflow', 'Stack Overflow'),
            ('github', 'GitHub'),
            ('other', '기타'),
        ],
        default='official',
    )
    fetched_at = models.DateTimeField(auto_now_add=True)
```

- `source_type`: choices로 출처 유형을 제한해 데이터 일관성 유지
- `url`에 `unique=True`: 같은 URL의 레퍼런스를 중복 저장하지 않음

**LearningLog 모델**: 핵심 모델. Tag, Reference와 ManyToMany로 연결.

```python
class LearningLog(models.Model):
    query = models.CharField(max_length=500, db_index=True, verbose_name="질문")
    ai_response = models.TextField(verbose_name="AI 답변")
    markdown_content = models.TextField(verbose_name="마크다운 내용")

    references = models.ManyToManyField(Reference, related_name='learning_logs', blank=True)
    tags = models.ManyToManyField(Tag, related_name='learning_logs', blank=True)

    is_bookmarked = models.BooleanField(default=False)
    view_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['created_at']),
            models.Index(fields=['query']),
            models.Index(fields=['is_bookmarked']),
        ]
```

**ManyToMany를 선택한 이유:**

| 방식 | 장점 | 단점 |
| --- | --- | --- |
| ArrayField | 단순, 별도 테이블 불필요 | PostgreSQL 전용, 태그 통계 어려움 |
| **ManyToMany** | **태그별 조회/통계 가능, DB 무관** | **중간 테이블 생성** |

하나의 학습 로그에 여러 태그가 붙고, 하나의 태그로 여러 학습 로그를 조회해야 하므로 ManyToMany가 적합하다.

**인덱스 설계:**
- `created_at`: 최신순 정렬 (리스트 페이지)
- `query`: 질문 검색 최적화
- `is_bookmarked`: 북마크 필터링

### 2. services.py - 서비스 계층

뷰에서 비즈니스 로직을 분리하기 위해 `LearnlogService` 클래스를 도입했다.

```python
class LearnlogService:
    def __init__(self):
        self.groq_client = Groq(api_key=settings.GROQ_API_KEY)
        self.tavily_client = TavilyClient(api_key=settings.TAVILY_API_KEY)

    def process_query(self, user_query):
        """메인 처리 로직 - 5단계 파이프라인"""
        search_results = self.search_official_docs(user_query)  # Tavily
        ai_answer = self.generate_answer(user_query, search_results)  # Groq
        tag_names = self.extract_tags(user_query, ai_answer)  # Groq
        markdown = self.convert_to_markdown(user_query, ai_answer, search_results)  # Groq

        # DB 저장
        log = LearningLog.objects.create(
            query=user_query, ai_response=ai_answer, markdown_content=markdown,
        )

        # Reference 연결 (get_or_create로 중복 방지)
        for result in search_results.get('results', []):
            ref, created = Reference.objects.get_or_create(
                url=result.get('url', ''),
                defaults={
                    'title': result.get('title', 'Untitled'),
                    'excerpt': result.get('content', '')[:500],
                    'source_type': self._determine_source_type(result.get('url', '')),
                }
            )
            log.references.add(ref)

        # Tag 연결
        for tag_name in tag_names:
            tag, created = Tag.objects.get_or_create(
                name=tag_name, defaults={'slug': slugify(tag_name)}
            )
            log.tags.add(tag)

        return log
```

**핵심 설계 포인트:**

- **`get_or_create`**: 같은 URL의 Reference, 같은 이름의 Tag가 이미 있으면 재사용
- **5단계 파이프라인**: 각 단계가 독립적인 메서드로 분리되어 있어 개별 테스트 및 재사용 가능
- **외부 API 클라이언트를 `__init__`에서 초기화**: 요청마다 재생성하지 않음

**출처 유형 자동 판별:**

```python
def _determine_source_type(self, url):
    url_lower = url.lower()
    if 'stackoverflow.com' in url_lower:
        return 'stackoverflow'
    elif 'github.com' in url_lower:
        return 'github'
    elif any(domain in url_lower for domain in ['docs.', 'documentation', 'doc.']):
        return 'official'
    elif any(domain in url_lower for domain in ['blog', 'medium.com', 'dev.to']):
        return 'blog'
    else:
        return 'other'
```

---

## 파일 구조

```
search/
├── models.py          # Tag, Reference, LearningLog 모델
├── services.py        # LearnlogService (검색 → AI → 태그 → 마크다운 → 저장)
├── admin.py           # 3개 모델 admin 등록
└── migrations/
    └── 0001_initial.py  # 초기 마이그레이션
```

---

## 앞으로의 개선

### 검색 도메인 하드코딩 → 기술 스택별 자동 매핑

> **커밋**: [`bb8c09b`](https://github.com/aengu/learn-log/commit/bb8c09bf47fd82de0694727b7293465d5246ab53) feat: 기술 스택별 검색 도메인 자동 매핑 및 출처 판단 개선

`search_official_docs()`에서 도메인이 4개로 하드코딩되어 있었다:

```python
# 변경 전
include_domains=[
    "docs.docker.com",
    "docs.python.org",
    "docs.djangoproject.com",
    "github.com",
]
```

`domains.py`로 기술-도메인 매핑을 분리하고, 질문에서 기술 스택을 추출해 해당 공식 문서 도메인만 검색하도록 개선:

```python
# 변경 후
from .domains import get_domains_for_query, is_official_doc

domains = get_domains_for_query(query)  # 질문에서 관련 도메인 자동 추출
if domains:
    search_params['include_domains'] = domains
```

### process_query 단일 메서드 → save_learning_log 분리

> **커밋**: [`09bca51`](https://github.com/aengu/learn-log/commit/09bca5169f4d38256e33e6e36c993e54d6589363) feat: 검색 후 프로그레스바 + 진행로그를 위한 SSE 구현

SSE 스트리밍 도입 시, `process_query()` 하나로 묶여 있으면 중간에 yield를 할 수 없었다. DB 저장 로직을 `save_learning_log()`로 분리하여 SSE 뷰에서 각 단계를 개별 호출할 수 있게 리팩토링:

```python
# 변경 전: process_query()가 검색~저장까지 전부 처리
def process_query(self, user_query):
    search_results = self.search_official_docs(user_query)
    ai_answer = self.generate_answer(user_query, search_results)
    # ... 중간에 yield 불가

# 변경 후: 저장 로직 분리
def process_query(self, user_query):
    search_results = self.search_official_docs(user_query)
    ai_answer = self.generate_answer(user_query, search_results)
    tag_names = self.extract_tags(user_query, ai_answer)
    markdown = self.convert_to_markdown(user_query, ai_answer, search_results)
    return self.save_learning_log(user_query, ai_answer, markdown, search_results, tag_names)

def save_learning_log(self, query, ai_answer, markdown, search_results, tag_names):
    """SSE 뷰에서도 재사용 가능한 저장 메서드"""
    log = LearningLog.objects.create(...)
    # Reference, Tag 연결
    return log
```

### 출처 판별 문자열 매칭 → 도메인 기반 판별

> **커밋**: [`bb8c09b`](https://github.com/aengu/learn-log/commit/bb8c09bf47fd82de0694727b7293465d5246ab53) feat: 기술 스택별 검색 도메인 자동 매핑 및 출처 판단 개선

`_determine_source_type()`에서 `docs.` 문자열 포함 여부로 공식 문서를 판별하던 방식을 `is_official_doc()` 함수로 교체:

```python
# 변경 전
elif any(domain in url_lower for domain in ['docs.', 'documentation', 'doc.']):
    return 'official'

# 변경 후
from .domains import is_official_doc

elif is_official_doc(url):  # 등록된 공식 문서 도메인 목록 기반 판별
    return 'official'
```
