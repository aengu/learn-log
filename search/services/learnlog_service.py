import json
import textwrap
from concurrent.futures import ThreadPoolExecutor

from groq import Groq
from mistralai.client import Mistral
from pgvector.django import CosineDistance
from tavily import TavilyClient
from django.conf import settings
from django.contrib.postgres.search import SearchVector, SearchQuery, SearchRank
from django.utils.text import slugify

from ..models import LearningLog, Tag, Reference
from ..domains import get_domains_for_query, is_official_doc


class LearnlogService:
    """
    Learnlog 로직
    - Tavily로 웹 검색
    - Mistral로 AI 답변 생성
    - Groq으로 태그 자동 추출
    - Groq으로 마크다운 변환
    """

    ANSWER_MODEL = "mistral-large-latest"
    LIGHT_MODEL = "llama-3.3-70b-versatile"
    EMBED_MODEL = "mistral-embed"  # 1024차원

    def __init__(self):
        self.mistral_client = Mistral(
            api_key=settings.MISTRAL_API_KEY,
            timeout_ms=120_000,
        )
        self.groq_client = Groq(api_key=settings.GROQ_API_KEY)
        self.tavily_client = TavilyClient(api_key=settings.TAVILY_API_KEY)

    def process_query(self, user_query):
        """
        메인 처리 로직 (HTMX용 - 동기 처리)
        SSE 스트리밍은 QuerySSEView에서 각 메서드를 직접 호출
        """
        # 1. 과거 학습 기록 검색 (RAG retrieval)
        retrieved_logs = self.retrieve_similar_logs(user_query)

        # 2. 웹 검색
        search_results = self.search_official_docs(user_query)

        # 3. AI 답변 생성
        ai_answer = self.generate_answer(user_query, search_results, retrieved_logs=retrieved_logs)

        # 4. 태그 추출 + 마크다운 변환 (병렬)
        with ThreadPoolExecutor(max_workers=2) as executor:
            tags_future = executor.submit(self.extract_tags, user_query, ai_answer)
            md_future = executor.submit(self.convert_to_markdown, user_query, ai_answer, search_results)
            tag_names = tags_future.result()
            markdown = md_future.result()

        # 5. DB 저장
        return self.save_learning_log(user_query, ai_answer, markdown, search_results, tag_names)

    def save_learning_log(self, query, ai_answer, markdown, search_results, tag_names, parent=None, answer_source='', is_truncated=False):
        """
        LearningLog 및 관련 데이터 DB 저장
        """
        # 컨텍스트가 있었던 답변만 비동기 검증 대상 (verify_log가 pending을 풀어준다)
        verification = 'pending' if answer_source in ('both', 'logs', 'web') else ''

        # LearningLog 생성 (임베딩 실패해도 저장은 진행 — _embed가 None 반환)
        log = LearningLog.objects.create(
            query=query,
            ai_response=ai_answer,
            markdown_content=markdown,
            parent=parent,
            embedding=self._embed(self._embedding_input(query, ai_answer)),
            answer_source=answer_source,
            is_truncated=is_truncated,
            verification=verification,
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

    # ── RAG: 임베딩 + 하이브리드 검색 ──────────────────────────────────

    @staticmethod
    def _embedding_input(query, ai_response):
        """임베딩 대상 텍스트. markdown_content는 ai_response와 중복이라 제외"""
        return f"{query}\n{ai_response[:2000]}"

    def _embed(self, text):
        """
        mistral-embed로 1024차원 임베딩 생성.
        실패 시 None 반환 — 저장·검색 메인 흐름을 막지 않는다.
        """
        try:
            response = self.mistral_client.embeddings.create(
                model=self.EMBED_MODEL,
                inputs=[text],
            )
            return response.data[0].embedding
        except Exception as e:
            print(f"임베딩 생성 오류: {e}")
            return None

    def retrieve_similar_logs(self, query, k=3, exclude_pks=None):
        """
        과거 학습 로그 하이브리드 검색 (RAG retrieval).
        - FTS(키워드 정확 매칭)와 벡터 코사인 유사도(의미 매칭)를 각각 top-10 조회
        - RRF로 두 순위를 결합해 top-k 반환
        - 임베딩 실패 시 FTS 결과만으로 동작
        """
        base = LearningLog.objects.all()
        if exclude_pks:
            base = base.exclude(pk__in=exclude_pks)

        fts_vector = (
            SearchVector('query', weight='A', config='simple') +
            SearchVector('ai_response', weight='B', config='simple')
        )
        fts_query = SearchQuery(query, config='simple')
        fts_pks = list(
            base.annotate(rank=SearchRank(fts_vector, fts_query))
            .filter(rank__gt=0)
            .order_by('-rank')
            .values_list('pk', flat=True)[:10]
        )

        vec_pks = []
        query_embedding = self._embed(query)
        if query_embedding is not None:
            vec_pks = list(
                base.exclude(embedding=None)
                .order_by(CosineDistance('embedding', query_embedding))
                .values_list('pk', flat=True)[:10]
            )

        merged_pks = self._rrf_merge([fts_pks, vec_pks])[:k]
        logs = LearningLog.objects.in_bulk(merged_pks)
        return [logs[pk] for pk in merged_pks if pk in logs]

    @staticmethod
    def _rrf_merge(rankings, k=60):
        """
        Reciprocal Rank Fusion: 각 순위 목록에서 1/(k+순위)를 합산해 재정렬.
        점수 스케일이 다른 FTS rank와 코사인 거리를 순위로만 결합한다 (k=60은 관례값).
        """
        scores = {}
        for ranking in rankings:
            for rank, pk in enumerate(ranking, start=1):
                scores[pk] = scores.get(pk, 0.0) + 1.0 / (k + rank)
        return sorted(scores, key=scores.get, reverse=True)

    # ── 에이전트: 라우팅 판단 (search_agent의 router 노드에서 호출) ──

    def decide_route(self, query, retrieved_logs):
        """
        라우터: 검색된 과거 로그를 답변 컨텍스트로 쓸지(use_logs),
        웹 검색 보강이 필요한지(need_web)를 LLM이 판단.
        실패 시 둘 다 True — 라우팅 도입 전 파이프라인과 동일한 안전 기본값.
        """
        log_lines = "\n".join(
            f"- {log.query}: {log.ai_response[:200]}"
            for log in retrieved_logs
        ) or "(검색된 기록 없음)"
        prompt = textwrap.dedent(f"""
            사용자의 개발 질문과, 사용자가 과거에 학습한 기록 목록입니다.

            질문: {query}

            과거 학습 기록 (제목: 내용 앞부분):
            {log_lines}

            JSON으로만 응답하세요 (```없이):
            {{
              "use_logs": true/false,
              "need_web": true/false,
              "reason": "판단 근거 한 문장"
            }}

            판단 기준:
            - use_logs: 기록이 질문과 같은 주제를 다뤄서 답변 컨텍스트로 유용한가.
              주제가 다른 기록뿐이면 false (무관한 기록을 넣으면 답변 품질이 떨어짐)
            - need_web: 기록만으로 부족해서 웹 검색 보강이 필요한가.
              기록이 질문의 답을 이미 충분히 담고 있을 때만 false.
              단, 구체적인 버전·설정/옵션 이름·API 시그니처·정확한 수치·최신 동향을 묻는
              질문은 기록이 충분해 보여도 true (기록은 LLM 생성물이라 공식 문서 대조 필요)
        """).strip()
        try:
            result = self._call_groq_json(prompt, max_tokens=150)
            return {
                'use_logs': bool(result.get('use_logs', True)),
                'need_web': bool(result.get('need_web', True)),
                'reason': result.get('reason', ''),
            }
        except Exception as e:
            print(f"라우팅 판단 오류: {e}")
            return {'use_logs': True, 'need_web': True, 'reason': '판단 실패 — 기본 경로'}

    def check_consistency(self, ai_response, retrieved_logs=None, retrieved_limit=500, search_results=None):
        """
        답변이 제공된 컨텍스트와 모순되는지 LLM Judge로 판정만 한다 (저장 없음).
        Judge는 Groq(생성 모델 Mistral과 다른 계열)라 교차 검증 효과가 있다.
        반환: {'consistent': bool, 'note': str} — 컨텍스트가 없으면 None.
        판정 실패는 예외로 올린다 (호출자가 미검증 처리).
        """
        retrieved = self._build_retrieved_context(retrieved_logs or [], limit=retrieved_limit)
        web = "\n".join(
            f"[{r.get('url', '')}] {r.get('content', '')[:200]}"
            for r in (search_results or {}).get('results', [])[:2]
        )
        context = f"{retrieved}{web}".strip()
        if not context:
            return None

        prompt = textwrap.dedent(f"""
            AI 답변이 생성에 사용된 참고 컨텍스트와 모순되는지 검사하세요.

            참고 컨텍스트:
            {context}

            답변:
            {ai_response[:3000]}

            JSON으로만 응답하세요 (```없이):
            {{
              "consistent": true/false,
              "note": "모순이 있으면 어떤 주장이 어긋나는지 한 문장 (없으면 빈 문자열)"
            }}

            판단 기준:
            - 답변의 주장(수치, 동작 설명, API 사용법)이 컨텍스트 내용과 명백히 어긋나면 consistent=false
            - 컨텍스트에 없는 내용을 답변이 추가로 다루는 것은 모순이 아님
        """).strip()
        result = self._call_groq_json(prompt, max_tokens=200)
        consistent = bool(result.get('consistent', True))
        return {
            'consistent': consistent,
            'note': '' if consistent else result.get('note', ''),
        }

    def verify_log(self, log, retrieved_logs=None, retrieved_limit=500, search_results=None):
        """
        저장된 로그를 검사하고 결과를 verification 필드에 기록.
        저장 후 백그라운드 스레드에서 호출된다 — 응답 흐름을 막지 않고, 결과는 배지로만 표시.
        컨텍스트가 없거나 검증에 실패하면 미검증('')으로 남긴다.
        """
        try:
            verdict = self.check_consistency(
                log.ai_response, retrieved_logs, retrieved_limit, search_results,
            )
        except Exception as e:
            print(f"비동기 검증 오류: {e}")
            verdict = None
        if verdict is None:
            log.verification = ''
            log.verification_note = ''
        else:
            log.verification = 'passed' if verdict['consistent'] else 'suspect'
            log.verification_note = verdict['note']
        log.save(update_fields=['verification', 'verification_note'])

    def _call_groq_json(self, prompt, max_tokens=300):
        """
        Groq 경량 모델 호출 후 JSON 파싱.
        response_format=json_object로 모델 레벨에서 valid JSON을 강제한다
        (코드펜스·잡설 방지). 프롬프트에 'JSON' 단어가 있어야 동작.
        """
        response = self.groq_client.chat.completions.create(
            model=self.LIGHT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        return json.loads(response.choices[0].message.content)

    def search_official_docs(self, query, parent=None):
        """
        Tavily API로 공식 문서 검색
        - 한국어 질문을 영어 검색어로 변환해 영어 공식 문서 매칭률을 높임
        - 질문에서 기술 스택을 추출하여 해당 공식 문서 도메인으로 검색
        - 꼬리질문(parent)이면 루트+직속 부모 질문을 변환 컨텍스트로 사용
          (체인에서 직속 부모도 모호할 수 있으므로 자기완결적인 루트 질문으로 주제 보장)
        """
        context_queries = None
        if parent:
            context_queries = list(dict.fromkeys([parent.root.query, parent.query]))

        # 한국어 → 영어 검색어 변환 (영어 공식 문서 매칭률 향상)
        search_query = self._to_search_query(query, context_queries)

        # 도메인 매칭은 원본(한국어 키워드 포함) + 변환 쿼리 + 부모 질문에서 추출
        domain_source = f"{query} {search_query} {' '.join(context_queries or [])}"
        domains = get_domains_for_query(domain_source)
        print(f"  검색어: {search_query} / 도메인: {domains}")

        try:
            search_params = {
                'query': search_query,
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

    def _to_search_query(self, query, context_queries=None):
        """
        한국어 개발 질문을 영어 웹 검색용 키워드로 변환.
        꼬리질문은 지시어("그러면", "그거")뿐이라 부모 질문 없이는 변환이 깨지므로
        context_queries(루트+직속 부모 질문)를 함께 넘긴다 (0610 벤치마크: 0% → 100%).
        실패 시 원본 질문을 그대로 반환한다.
        """
        context_lines = "".join(
            f"Previous question (context): {q}\n" for q in (context_queries or [])
        )
        prompt = (
            "Convert this developer question into a concise English web search query.\n"
            "Output only the search keywords (tech names, concepts), no explanation.\n\n"
            f"{context_lines}Question: {query}\n\n"
            "Search query:"
        )
        try:
            response = self.groq_client.chat.completions.create(
                model=self.LIGHT_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=40,
            )
            converted = response.choices[0].message.content.strip()
            return converted or query
        except Exception as e:
            print(f"검색어 변환 오류: {e}")
            return query

    DEFAULT_INSTRUCTIONS = (
        "형식: 개념 → 동작 원리 → 코드 예시 → 주의사항. 코드에 주석 포함. "
        "코드 예시는 질문에 언어/스택 지정이 없으면 Python(백엔드 맥락은 Django) 기준으로. "
        # grounding: 환각이 잘 생기는 '그럴듯한 디테일 지어내기' 차단
        "구체적인 버전·수치·설정 이름·API 이름은 참고 자료나 과거 기록에 근거가 있을 때만 단정하고, "
        "근거가 없으면 불확실하다고 명시할 것."
    )

    @staticmethod
    def _build_retrieved_context(retrieved_logs, limit=500):
        """
        RAG: 하이브리드 검색으로 찾은 과거 로그 블록. 로그당 답변 limit자 절삭
        (0610 벤치마크: 전문 주입은 답변이 길어져 max_tokens에 잘림).
        웹검색 생략 경로는 Tavily 블록이 빠진 예산만큼 늘려 받는다 (search_agent).
        """
        if not retrieved_logs:
            return ""
        blocks = "\n".join(
            f"[기록{i}] Q: {log.query}\nA: {log.ai_response[:limit]}"
            for i, log in enumerate(retrieved_logs, start=1)
        )
        return f"과거에 학습한 관련 기록:\n{blocks}\n\n"

    @staticmethod
    def _build_conversation_context(parent):
        """
        꼬리질문용 이전 대화 블록. 직속 부모의 질문 + 답변 500자만 포함한다.
        (0610 벤치마크: 전문 주입은 답변이 길어져 max_tokens에 잘림. 500자면
        지시어 해석에 충분하고 부모 답변이 체인의 주제를 운반함)
        """
        if not parent:
            return ""
        return (
            f"이전 대화:\n[이전 질문] {parent.query}\n"
            f"[이전 답변] {parent.ai_response[:500]}\n\n"
        )

    def generate_answer(self, query, search_results, custom_instructions=None, parent=None, retrieved_logs=None, retrieved_limit=500):
        """
        Mistral API로 AI 답변 생성
        """
        context = "\n".join([
            f"[{r.get('url', '')}] {r.get('content', '')[:200]}"
            for r in search_results.get('results', [])[:2]
        ])

        instructions = custom_instructions.strip() if custom_instructions else self.DEFAULT_INSTRUCTIONS
        conversation = self._build_conversation_context(parent)
        retrieved = self._build_retrieved_context(retrieved_logs, limit=retrieved_limit)

        # 개행 포함 블록을 f-string에 넣으면 dedent가 무효라 직접 조립
        prompt = (
            "개발 질문에 한국어로 답변하세요.\n\n"
            f"{retrieved}{conversation}질문: {query}\n\n"
            f"참고:\n{context if context else '없음'}\n\n"
            f"{instructions}"
        )

        try:
            response = self.mistral_client.chat.complete(
                model=self.ANSWER_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=2000
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            print(f"AI 답변 생성 오류: {e}")
            return "답변 생성 중 오류가 발생했습니다."

    def generate_answer_stream(self, query, search_results, custom_instructions=None, parent=None, retrieved_logs=None, retrieved_limit=500, meta=None):
        """
        Mistral API 스트리밍 답변 생성 — 토큰 단위로 yield.
        meta dict를 넘기면 마지막 이벤트의 finish_reason을 채워준다
        ('length'면 max_tokens 잘림 — 호출자가 잘림 플래그에 사용).
        """
        context = "\n".join([
            f"[{r.get('url', '')}] {r.get('content', '')[:200]}"
            for r in search_results.get('results', [])[:2]
        ])

        instructions = custom_instructions.strip() if custom_instructions else self.DEFAULT_INSTRUCTIONS
        conversation = self._build_conversation_context(parent)
        retrieved = self._build_retrieved_context(retrieved_logs, limit=retrieved_limit)

        # 개행 포함 블록을 f-string에 넣으면 dedent가 무효라 직접 조립
        prompt = (
            "개발 질문에 한국어로 답변하세요.\n\n"
            f"{retrieved}{conversation}질문: {query}\n\n"
            f"참고:\n{context if context else '없음'}\n\n"
            f"{instructions}"
        )

        try:
            stream = self.mistral_client.chat.stream(
                model=self.ANSWER_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=2000
            )
            for event in stream:
                choice = event.data.choices[0]
                if meta is not None and choice.finish_reason:
                    meta['finish_reason'] = str(choice.finish_reason)
                chunk = choice.delta.content
                if chunk:
                    yield chunk
        except Exception as e:
            print(f"AI 답변 스트리밍 오류: {e}")
            yield "답변 생성 중 오류가 발생했습니다."

    def extract_tags(self, query, ai_response):
        """
        Groq API로 태그 자동 추출
        """
        prompt = textwrap.dedent(f"""
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

            태그:
        """).strip()

        try:
            response = self.groq_client.chat.completions.create(
                model=self.LIGHT_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
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
            print(f"태그 추출 오류: {e}")
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

        prompt = textwrap.dedent(f"""
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

            출력:
        """).strip()

        try:
            response = self.groq_client.chat.completions.create(
                model=self.LIGHT_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.5,
                max_tokens=2000
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            print(f"마크다운 변환 오류: {e}")
            # 실패 시 기본 포맷
            return f"## {query}\n\n{answer}\n\n## 참고 자료\n{refs}"
