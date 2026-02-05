from groq import Groq
from tavily import TavilyClient
from django.conf import settings
from django.utils.text import slugify
from .models import LearningLog, Tag, Reference
from .domains import get_domains_for_query, is_official_doc


"""
todo
- [완료] include_domain 자동 변경
- groq temperature 수치 조정
- [완료] query 질의 후 progress bar + log
- 참고자료에 github 뺄까...
- 태그 계층화 (ex: database-postgresql-isolation_level)
- groq, tavily 프롬프트 수정 기능 (글자 수, 요구조건 등)
- sse 비동기로 변경
"""

class LearnlogService:
    """
    Learnlog 로직
    - Tavily로 웹 검색
    - Groq로 AI 답변 생성
    - Groq로 태그 자동 추출
    - Groq로 마크다운 변환
    """
    
    def __init__(self):
        self.groq_client = Groq(api_key=settings.GROQ_API_KEY)
        self.tavily_client = TavilyClient(api_key=settings.TAVILY_API_KEY)
    
    def process_query(self, user_query):
        """
        메인 처리 로직 (HTMX용 - 동기 처리)
        SSE 스트리밍은 QuerySSEView에서 각 메서드를 직접 호출
        """
        # 1. 웹 검색
        search_results = self.search_official_docs(user_query)

        # 2. AI 답변 생성
        ai_answer = self.generate_answer(user_query, search_results)

        # 3. 태그 자동 추출
        tag_names = self.extract_tags(user_query, ai_answer)

        # 4. 마크다운 변환
        markdown = self.convert_to_markdown(user_query, ai_answer, search_results)

        # 5. DB 저장
        return self.save_learning_log(user_query, ai_answer, markdown, search_results, tag_names)

    def save_learning_log(self, query, ai_answer, markdown, search_results, tag_names):
        """
        LearningLog 및 관련 데이터 DB 저장
        """
        # LearningLog 생성
        log = LearningLog.objects.create(
            query=query,
            ai_response=ai_answer,
            markdown_content=markdown,
        )

        # Reference 생성 및 연결
        for result in search_results.get('results', []):
            ref, _ = Reference.objects.get_or_create(
                url=result.get('url', ''),
                defaults={
                    'title': result.get('title', 'Untitled'),
                    'excerpt': result.get('content', '')[:500],
                    'source_type': self._determine_source_type(result.get('url', '')),
                }
            )
            log.references.add(ref)

        # Tag 생성 및 연결
        for tag_name in tag_names:
            tag, _ = Tag.objects.get_or_create(
                name=tag_name,
                defaults={'slug': slugify(tag_name)}
            )
            log.tags.add(tag)

        return log
    
    def _determine_source_type(self, url):
        """
        URL을 분석해서 출처 유형 결정
        - tech_domains.py의 공식 문서 도메인 목록 활용
        """
        url_lower = url.lower()

        if 'stackoverflow.com' in url_lower:
            return 'stackoverflow'
        elif 'github.com' in url_lower:
            return 'github'
        elif is_official_doc(url):
            return 'official'
        elif any(keyword in url_lower for keyword in ['blog', 'medium.com', 'dev.to']):
            return 'blog'
        else:
            return 'other'

    
    def search_official_docs(self, query):
        """
        Tavily API로 공식 문서 검색
        - 질문에서 기술 스택을 추출하여 해당 공식 문서 도메인으로 검색
        """
        # 질문에서 관련 도메인 키워드 추출
        domains = get_domains_for_query(query)
        print(f"  검색 도메인: {domains}")

        try:
            search_params = {
                'query': query,
                'search_depth': 'advanced',
                'max_results': 5,
            }

            # 도메인이 있으면 include_domains 추가
            if domains:
                search_params['include_domains'] = domains

            results = self.tavily_client.search(**search_params)
            return results
        except Exception as e:
            print(f"검색 오류: {e}")
            return {'results': []}

    
    def generate_answer(self, query, search_results):
        """
        Groq API로 AI 답변 생성
        """
        # 검색 결과를 컨텍스트로 포맷팅
        context = "\n\n".join([
            f"출처: {r.get('url', 'N/A')}\n내용: {r.get('content', '')[:400]}"
            for r in search_results.get('results', [])[:3]  # 상위 3개만
        ])
        
        prompt = f"""
                당신은 친절하고 정확한 개발 전문가입니다.

                사용자 질문: {query}

                참고 자료:
                {context if context else "참고 자료 없음"}

                위 참고 자료를 바탕으로 질문에 대한 명확하고 상세한 답변을 작성해주세요.

                요구사항:
                - 한국어로 작성
                - 기술적으로 정확하게
                - 초보자도 이해할 수 있게 설명
                - 가능하면 코드 예시 포함
                - 간결하지만 핵심은 빠뜨리지 않게
            """
        
        try:
            response = self.groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7, # 답변 말투의 변동성
                max_tokens=2000 # 최대 글자 수, 최댓값 16384
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            print(f"❌ AI 답변 생성 오류: {e}")
            return "답변 생성 중 오류가 발생했습니다."
    
    def extract_tags(self, query, ai_response):
        """
        Groq API로 태그 자동 추출
        """
        prompt = f"""
            다음 개발 질문과 답변에서 핵심 기술 태그를 추출해주세요.

            질문: {query}
            답변: {ai_response[:500]}

            규칙:
            - 정확히 3~5개의 태그만 추출
            - 모두 소문자, 영어만 사용
            - 쉼표로 구분
            - 기술명, 도구명, 핵심 개념만 포함
            - 불필요한 단어 제외 (예: "how", "what", "difference")
            - 공백은 하이픈(-)으로 대체

            출력 형식 예시: docker, network, bridge-mode, container

            태그:"""
        
        try:
            response = self.groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,  # 낮은 temperature로 일관성 확보
                max_tokens=50
            )
            
            tags_text = response.choices[0].message.content.strip()
            
            # 파싱 및 정제
            tags = [
                tag.strip().lower().replace(' ', '-')
                for tag in tags_text.split(',')
                if tag.strip() and len(tag.strip()) > 1
            ]
            
            return tags[:5]  # 최대 5개
            
        except Exception as e:
            print(f"❌ 태그 추출 오류: {e}")
            # 실패 시 간단히 질문에서 추출
            return self._fallback_tag_extraction(query)
    
    def _fallback_tag_extraction(self, query):
        """
        태그 추출 실패 시 대체 방법
        """
        common_terms = [
            'docker', 'python', 'javascript', 'react', 'django',
            'api', 'database', 'network', 'kubernetes', 'git'
        ]
        
        query_lower = query.lower()
        tags = [term for term in common_terms if term in query_lower]
        
        return tags[:3] if tags else ['general']
    
    def convert_to_markdown(self, query, answer, search_results):
        """
        Groq API로 노션 스타일 마크다운 변환
        """
        refs = "\n".join([
            f"- [{r.get('title', 'N/A')}]({r.get('url', '')})"
            for r in search_results.get('results', [])
        ])
        
        prompt = f"""
            다음 내용을 노션 스타일 마크다운으로 정리해주세요:

            질문: {query}

            답변:
            {answer}

            참고 자료:
            {refs if refs else "없음"}

            요구사항:
            - 제목은 ## 질문 형식으로
            - 핵심 내용은 명확하게 구조화
            - 차이점이나 비교는 표(table) 사용
            - 코드 예시는 적절한 언어로 ```언어 코드블록``` 사용
            - 참고 자료는 맨 아래 "## 참고 자료" 섹션에
            - 노션에 바로 복사/붙여넣기 가능하게
            - 이모지 적절히 사용

            출력:"""
        
        try:
            response = self.groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.5,
                max_tokens=3000
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            print(f"❌ 마크다운 변환 오류: {e}")
            # 실패 시 기본 포맷
            return f"## {query}\n\n{answer}\n\n## 참고 자료\n{refs}"