import json
import textwrap

from groq import Groq
from tavily import TavilyClient
from django.conf import settings
from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify

from .models import LearningLog, Tag, Reference, Exercise, ExerciseAttempt
from .domains import get_domains_for_query, is_official_doc


"""
todo
- [완료] include_domain 자동 변경
- groq temperature 수치 조정
- [완료] query 질의 후 progress bar + log
- 참고자료에 github 뺄까...
- 태그 계층화 (ex: database-postgresql-isolation_level)
- groq, tavily 프롬프트 수정 기능 (글자 수, 요구조건 등)
- [완료] 학습기록 리스트 페이지
    - 정렬, 검색
    - 필터: 태그 
    - 북마크
- 레퍼런스, 태그, 학습기록 통계 페이지
- [진행중] 테스트코드 작성
    - [진행중] api
    - service
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

    
    DEFAULT_INSTRUCTIONS = textwrap.dedent("""
        - 한국어로 작성
        - 기술적으로 정확하게
        - 개념 설명 → 동작 원리 → 코드 예시 → 주의사항 순서로 구성
        - 코드 예시는 반드시 포함하고, 각 줄에 주석으로 설명 추가
        - 관련 개념이 있으면 함께 설명 (예: A를 쓸 때 B도 알아야 하는 경우)
        - 핵심 포인트는 빠뜨리지 말고 충분히 상세하게
    """).strip()

    def generate_answer(self, query, search_results, custom_instructions=None):
        """
        Groq API로 AI 답변 생성
        """
        context = "\n\n".join([
            f"출처: {r.get('url', 'N/A')}\n내용: {r.get('content', '')[:400]}"
            for r in search_results.get('results', [])[:3]
        ])

        instructions = custom_instructions.strip() if custom_instructions else self.DEFAULT_INSTRUCTIONS

        prompt = textwrap.dedent(f"""
            당신은 친절하고 정확한 개발 전문가입니다.

            사용자 질문: {query}

            참고 자료:
            {context if context else "참고 자료 없음"}

            위 참고 자료를 바탕으로 질문에 대한 명확하고 상세한 답변을 작성해주세요.

            요구사항:
            {instructions}
        """).strip()

        try:
            response = self.groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=4000
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            print(f"❌ AI 답변 생성 오류: {e}")
            return "답변 생성 중 오류가 발생했습니다."
    
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


class ExerciseService:
    """
    연습문제 생성·채점·간격 반복 관리
    - generation_compare: AI 비교 채점
    - path_trace: 인덱스 매칭 (JS 즉시 피드백 + 서버 저장)
    - retrieval_checkin: 핵심 포인트 체크 (AI)
    """

    def __init__(self):
        self.groq_client = Groq(api_key=settings.GROQ_API_KEY)

    # ── 생성 ──────────────────────────────────────────────────────────

    def generate_exercise(self, learning_log, exercise_type):
        content = self._generate_content(learning_log, exercise_type)
        return Exercise.objects.create(
            learning_log=learning_log,
            exercise_type=exercise_type,
            content=content,
        )

    def _generate_content(self, learning_log, exercise_type):
        dispatch = {
            'generation_compare': self._gen_generation_compare,
            'path_trace': self._gen_path_trace,
            'retrieval_checkin': self._gen_retrieval_checkin,
        }
        if exercise_type not in dispatch:
            raise ValueError(f"알 수 없는 유형: {exercise_type}")
        return dispatch[exercise_type](learning_log)

    def _gen_generation_compare(self, log):
        prompt = textwrap.dedent(f"""
            다음 학습 내용을 바탕으로 "생성→비교" 유형 연습문제를 만들어주세요.

            학습 내용:
            질문: {log.query}
            답변: {log.ai_response[:1000]}

            "생성→비교" 유형: 학습자가 먼저 자신의 답변을 작성하고 AI 모범 답안과 비교합니다.

            JSON으로만 응답하세요 (```없이):
            {{
              "question": "학습자에게 물어볼 질문 (핵심 개념을 직접 설명하게 유도)",
              "model_answer": "모범 답안 (핵심 포인트를 포함한 상세한 답변)"
            }}
        """).strip()
        return self._call_groq_json(prompt)

    def _gen_path_trace(self, log):
        prompt = textwrap.dedent(f"""
            다음 학습 내용을 바탕으로 "경로추적" 유형 연습문제를 만들어주세요.

            학습 내용:
            질문: {log.query}
            답변: {log.ai_response[:1000]}

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
        return self._call_groq_json(prompt)

    def _gen_retrieval_checkin(self, log):
        prompt = textwrap.dedent(f"""
            다음 학습 내용을 바탕으로 "인출 체크인" 유형 연습문제를 만들어주세요.

            학습 내용:
            질문: {log.query}
            답변: {log.ai_response[:1000]}

            "인출 체크인" 유형: 핵심 개념을 기억에서 꺼내는 연습. 학습자가 답변을 쓰면 AI가 핵심 포인트를 체크합니다.

            JSON으로만 응답하세요 (```없이):
            {{
              "question": "기억에서 꺼내게 하는 질문",
              "key_points": [
                "체크할 핵심 포인트 1",
                "체크할 핵심 포인트 2",
                "체크할 핵심 포인트 3"
              ]
            }}
            key_points는 3~5개로 구성하세요.
        """).strip()
        return self._call_groq_json(prompt)

    def _call_groq_json(self, prompt):
        response = self.groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=1500,
        )
        raw = response.choices[0].message.content.strip()
        if raw.startswith('```'):
            parts = raw.split('```')
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith('json'):
                raw = raw[4:]
        return json.loads(raw.strip())

    # ── 채점 ──────────────────────────────────────────────────────────

    def evaluate_attempt(self, exercise, user_answer):
        dispatch = {
            'path_trace': self._evaluate_path_trace,
            'generation_compare': self._evaluate_generation_compare,
            'retrieval_checkin': self._evaluate_retrieval_checkin,
        }
        return dispatch[exercise.exercise_type](exercise, user_answer)

    def _evaluate_path_trace(self, exercise, user_answer):
        steps = exercise.content['steps']
        selected = user_answer.get('selected_indices', [])
        correct_count = sum(
            1 for i, step in enumerate(steps)
            if i < len(selected) and selected[i] == step['correct_index']
        )
        score = correct_count / len(steps) if steps else 0
        feedback_lines = [
            f"{'✅' if (i < len(selected) and selected[i] == step['correct_index']) else '❌'} "
            f"Step {i + 1}: {step['explanation']}"
            for i, step in enumerate(steps)
        ]
        return {
            'is_correct': score >= 0.6,
            'score': score,
            'ai_feedback': '\n'.join(feedback_lines),
        }

    def _evaluate_generation_compare(self, exercise, user_answer):
        user_text = user_answer.get('text', '')
        prompt = textwrap.dedent(f"""
            학습자의 답변과 모범 답안을 비교하여 평가해주세요.

            질문: {exercise.content.get('question', '')}
            모범 답안: {exercise.content.get('model_answer', '')}
            학습자 답변: {user_text}

            JSON으로만 응답하세요 (```없이):
            {{
              "score": 0.0~1.0,
              "is_correct": true/false,
              "feedback": "잘한 점과 보완할 점을 한국어로 설명"
            }}
        """).strip()
        try:
            response = self.groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=500,
            )
            raw = response.choices[0].message.content.strip()
            if raw.startswith('```'):
                parts = raw.split('```')
                raw = parts[1] if len(parts) > 1 else raw
                if raw.startswith('json'):
                    raw = raw[4:]
            result = json.loads(raw.strip())
            return {
                'is_correct': bool(result.get('is_correct', False)),
                'score': float(result.get('score', 0)),
                'ai_feedback': result.get('feedback', ''),
            }
        except Exception as e:
            print(f"❌ generation_compare 채점 오류: {e}")
            return {'is_correct': False, 'score': 0.0, 'ai_feedback': '채점 중 오류가 발생했습니다.'}

    def _evaluate_retrieval_checkin(self, exercise, user_answer):
        user_text = user_answer.get('text', '')
        key_points = exercise.content.get('key_points', [])
        points_str = '\n'.join(f"- {p}" for p in key_points)
        prompt = textwrap.dedent(f"""
            학습자의 답변에서 핵심 포인트가 포함됐는지 확인해주세요.

            질문: {exercise.content.get('question', '')}
            핵심 포인트:
            {points_str}
            학습자 답변: {user_text}

            JSON으로만 응답하세요 (```없이):
            {{
              "covered_points": [핵심 포인트와 동일한 순서로 true/false 목록],
              "feedback": "어떤 포인트를 잘 다뤘고 무엇이 빠졌는지 한국어로 설명"
            }}
        """).strip()
        try:
            response = self.groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=500,
            )
            raw = response.choices[0].message.content.strip()
            if raw.startswith('```'):
                parts = raw.split('```')
                raw = parts[1] if len(parts) > 1 else raw
                if raw.startswith('json'):
                    raw = raw[4:]
            result = json.loads(raw.strip())
            covered = result.get('covered_points', [False] * len(key_points))
            score = sum(covered) / len(key_points) if key_points else 0
            return {
                'is_correct': score >= 0.6,
                'score': score,
                'ai_feedback': result.get('feedback', ''),
            }
        except Exception as e:
            print(f"❌ retrieval_checkin 채점 오류: {e}")
            return {'is_correct': False, 'score': 0.0, 'ai_feedback': '채점 중 오류가 발생했습니다.'}

    # ── 저장 & 간격 반복 ────────────────────────────────────────────────

    def save_attempt(self, exercise, user_answer, evaluation):
        attempt = ExerciseAttempt.objects.create(
            exercise=exercise,
            user_answer=user_answer,
            is_correct=evaluation['is_correct'],
            ai_feedback=evaluation['ai_feedback'],
            score=evaluation['score'],
        )
        if evaluation['is_correct']:
            exercise.advance_interval()
        else:
            exercise.reset_interval()
        return attempt

    @staticmethod
    def get_due_exercises():
        return (
            Exercise.objects
            .filter(Q(next_review_at__isnull=True) | Q(next_review_at__lte=timezone.now()))
            .select_related('learning_log')
            .order_by('next_review_at', '-created_at')
        )