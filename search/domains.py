"""
기술 스택별 공식 문서 도메인 매핑
- 질문에서 키워드 추출 시 해당 도메인으로 검색 범위 제한
- 태그 추출 결과와도 매핑 가능
- 버전 path가 있는 문서는 current/stable/메이저 경로까지 명시해서
  Tavily가 옛 버전·릴리스 노트를 결과에 포함하지 않도록 함
"""
import re

TECH_DOCS_MAP = {
    # 컨테이너 / 인프라
    'docker': ['docs.docker.com'],
    '도커': ['docs.docker.com'],
    'kubernetes': ['kubernetes.io'],
    '쿠버네티스': ['kubernetes.io'],
    'k8s': ['kubernetes.io'],
    'helm': ['helm.sh'],
    '헬름': ['helm.sh'],
    'terraform': ['developer.hashicorp.com/terraform'],
    '테라폼': ['developer.hashicorp.com/terraform'],
    'ansible': ['docs.ansible.com'],
    '앤서블': ['docs.ansible.com'],
    'nginx': ['nginx.org/en/docs'],
    '엔진엑스': ['nginx.org/en/docs'],
    'apache': ['httpd.apache.org/docs/current'],
    '아파치': ['httpd.apache.org/docs/current'],

    # 클라우드
    'aws': ['docs.aws.amazon.com'],
    'gcp': ['cloud.google.com/docs'],
    'azure': ['learn.microsoft.com/azure'],

    # 언어
    'python': ['docs.python.org/3'],
    '파이썬': ['docs.python.org/3'],
    'javascript': ['developer.mozilla.org'],
    '자바스크립트': ['developer.mozilla.org'],
    'typescript': ['typescriptlang.org/docs'],
    '타입스크립트': ['typescriptlang.org/docs'],
    'java': ['docs.oracle.com/en/java'],
    '자바': ['docs.oracle.com/en/java'],
    'go': ['go.dev/doc'],
    'golang': ['go.dev/doc'],
    '고랭': ['go.dev/doc'],
    'rust': ['doc.rust-lang.org'],
    '러스트': ['doc.rust-lang.org'],
    'c++': ['en.cppreference.com'],
    'cpp': ['en.cppreference.com'],

    # 프레임워크 - Python
    'django': ['docs.djangoproject.com/en'],
    '장고': ['docs.djangoproject.com/en'],
    'flask': ['flask.palletsprojects.com/en/stable'],
    '플라스크': ['flask.palletsprojects.com/en/stable'],
    'fastapi': ['fastapi.tiangolo.com'],
    'celery': ['docs.celeryq.dev/en/stable'],
    '셀러리': ['docs.celeryq.dev/en/stable'],

    # 프레임워크 - JavaScript
    'react': ['react.dev'],
    '리액트': ['react.dev'],
    'vue': ['vuejs.org'],
    '뷰': ['vuejs.org'],
    'angular': ['angular.io/docs'],
    '앵귤러': ['angular.io/docs'],
    'next': ['nextjs.org/docs'],
    'nextjs': ['nextjs.org/docs'],
    '넥스트': ['nextjs.org/docs'],
    'nuxt': ['nuxt.com/docs'],
    '넉스트': ['nuxt.com/docs'],
    'node': ['nodejs.org/docs/latest/api'],
    'nodejs': ['nodejs.org/docs/latest/api'],
    '노드': ['nodejs.org/docs/latest/api'],
    'express': ['expressjs.com'],
    '익스프레스': ['expressjs.com'],
    'htmx': ['htmx.org'],

    # 프레임워크 - Java
    'spring': ['docs.spring.io'],
    '스프링': ['docs.spring.io'],
    'springboot': ['docs.spring.io/spring-boot'],
    '스프링부트': ['docs.spring.io/spring-boot'],

    # 데이터베이스
    'postgresql': ['postgresql.org/docs/current'],
    'postgres': ['postgresql.org/docs/current'],
    '포스트그레스': ['postgresql.org/docs/current'],
    'mysql': ['dev.mysql.com/doc'],
    '마이에스큐엘': ['dev.mysql.com/doc'],
    'mongodb': ['mongodb.com/docs'],
    '몽고디비': ['mongodb.com/docs'],
    '몽고': ['mongodb.com/docs'],
    'redis': ['redis.io/docs'],
    '레디스': ['redis.io/docs'],
    'elasticsearch': ['elastic.co/guide'],
    '엘라스틱서치': ['elastic.co/guide'],

    # 메시지 큐
    'kafka': ['kafka.apache.org/documentation'],
    '카프카': ['kafka.apache.org/documentation'],
    'rabbitmq': ['rabbitmq.com/docs'],
    '래빗엠큐': ['rabbitmq.com/docs'],

    # 버전 관리 / CI/CD
    'git': ['git-scm.com/doc'],
    '깃': ['git-scm.com/doc'],
    'github': ['docs.github.com'],
    '깃허브': ['docs.github.com'],
    '깃헙': ['docs.github.com'],
    'gitlab': ['docs.gitlab.com'],
    '깃랩': ['docs.gitlab.com'],

    # 기타
    'graphql': ['graphql.org/learn'],
    '그래프큐엘': ['graphql.org/learn'],
    'linux': ['man7.org', 'kernel.org/doc'],
    '리눅스': ['man7.org', 'kernel.org/doc'],
    'bash': ['gnu.org/software/bash/manual'],
    '배쉬': ['gnu.org/software/bash/manual'],
    'shell': ['gnu.org/software/bash/manual'],
    '쉘': ['gnu.org/software/bash/manual'],
}


def _tech_in_query(tech: str, query_lower: str) -> bool:
    """
    substring 오매칭을 막는 키워드 매칭 ("django"의 'go', "javascript"의 'java' 등).
    - ASCII 키: 영숫자 경계 검사. 표준 \\b는 한글을 단어문자로 취급해
      "go언어"처럼 한글이 붙는 표기가 매칭 실패하므로, 경계를 [a-z0-9] 부재로 정의.
    - 한글 키: 조사가 붙어("도커를") 경계 검사가 불가. 대신 맵에 등록된 더 긴 키
      ("자바스크립트")의 등장 부분을 지운 뒤 짧은 키("자바")를 검사한다.
    """
    if tech.isascii():
        pattern = rf"(?<![a-z0-9]){re.escape(tech)}(?![a-z0-9])"
        return re.search(pattern, query_lower) is not None

    longer_keys = [k for k in TECH_DOCS_MAP if tech in k and k != tech]
    cleaned = query_lower
    for k in longer_keys:
        cleaned = cleaned.replace(k, ' ')
    return tech in cleaned


def get_domains_for_query(query: str) -> list[str] | None:
    """
    질문에서 키워드를 추출하여 관련 공식 문서 도메인 반환.
    매칭되는 기술이 없으면 None을 반환해 도메인 제한 없이 전체 웹을 검색하게 한다.
    (github.com을 무조건 포함하면 한국어 질문이 임의 개인 레포로 오염되므로 제외)
    """
    query_lower = query.lower()
    domains = []

    for tech, urls in TECH_DOCS_MAP.items():
        if _tech_in_query(tech, query_lower):
            domains.extend(urls)

    # 중복 제거
    domains = list(dict.fromkeys(domains))

    return domains if domains else None


def is_official_doc(url: str) -> bool:
    """
    URL이 공식 문서인지 판단
    """
    url_lower = url.lower()
    official_domains = {d for domains in TECH_DOCS_MAP.values() for d in domains}

    return any(domain in url_lower for domain in official_domains)

