import json
import textwrap

from mistralai.client import Mistral
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from ..models import Exercise, ExerciseAttempt


class ExerciseService:
    """
    연습문제 생성·채점·간격 반복 관리
    - generation_compare: AI 비교 채점
    - path_trace: 인덱스 매칭 (JS 즉시 피드백 + 서버 저장)
    - retrieval_checkin: 핵심 포인트 체크 (AI)
    """

    MODEL = "mistral-small-latest"

    def __init__(self):
        self.mistral_client = Mistral(
            api_key=settings.MISTRAL_API_KEY,
            timeout_ms=120_000,
        )

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
        return self._call_mistral_json(prompt)

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
        return self._call_mistral_json(prompt)

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
        return self._call_mistral_json(prompt)

    @staticmethod
    def _parse_json_response(raw):
        raw = raw.strip()
        if raw.startswith('```'):
            parts = raw.split('```')
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith('json'):
                raw = raw[4:]
        return json.loads(raw.strip())

    def _call_mistral_json(self, prompt):
        response = self.mistral_client.chat.complete(
            model=self.MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=1500,
        )
        return self._parse_json_response(response.choices[0].message.content)

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
              "feedback": "아래 구조로 한국어로 작성:\n1. 맞게 설명한 부분 (what)\n2. 놓친 부분\n3. 왜 그렇게 동작하는지 (why) — 학습자가 설명했으면 인정, 안 했으면 짚어주기"
            }}
        """).strip()
        try:
            response = self.mistral_client.chat.complete(
                model=self.MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=500,
            )
            result = self._parse_json_response(response.choices[0].message.content)
            return {
                'is_correct': bool(result.get('is_correct', False)),
                'score': float(result.get('score', 0)),
                'ai_feedback': result.get('feedback', ''),
            }
        except Exception as e:
            print(f"generation_compare 채점 오류: {e}")
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
            response = self.mistral_client.chat.complete(
                model=self.MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=500,
            )
            result = self._parse_json_response(response.choices[0].message.content)
            covered = result.get('covered_points', [False] * len(key_points))
            score = sum(covered) / len(key_points) if key_points else 0
            return {
                'is_correct': score >= 0.6,
                'score': score,
                'ai_feedback': result.get('feedback', ''),
            }
        except Exception as e:
            print(f"retrieval_checkin 채점 오류: {e}")
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
