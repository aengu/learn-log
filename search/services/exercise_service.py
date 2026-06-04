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
    - generation_compare: 자가 마킹 채점 (LLM 호출 없음) + on-demand "AI 한마디"
    - path_trace: 인덱스 매칭 (JS 즉시 피드백 + 서버 저장)
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
        }
        if exercise_type not in dispatch:
            raise ValueError(f"알 수 없는 유형: {exercise_type}")
        return dispatch[exercise_type](learning_log)

    def _gen_generation_compare(self, log):
        prompt = textwrap.dedent(f"""
            아래 학습 내용으로 "생성→비교" 연습문제를 만들어주세요.
            학습자가 먼저 답변을 쓰고, 모범 답안과 핵심 포인트를 보며 스스로 비교/채점합니다.

            질문: {log.query}
            답변: {log.ai_response[:500]}

            JSON으로만 응답 (```없이):
            {{
              "question": "핵심 개념을 설명하게 유도하는 질문",
              "model_answer": "핵심 포인트를 포함한 모범 답안 (3~5문장)",
              "key_points": ["채점 기준 1", "기준 2", "기준 3", "기준 4"]
            }}

            ⚠️ key_points 규칙:
            - model_answer에서 빠지면 안 되는 핵심 명사구를 짧게 (각 30자 이내)
            - 학습자가 본인 답에 포함됐는지 yes/no로 판단할 수 있는 단위
            - 3~5개
        """).strip()
        return self._call_mistral_json(prompt)

    PATH_TRACE_MIN_STEPS = 3   # 최소 통과 step 수 (미달 시 재생성 시도)

    def _gen_path_trace(self, log):
        """
        출제 후 결정적 검증(choices[correct_index] == correct_answer)으로 잘못된 step을 걸러낸다.
        통과 step이 부족하면 1회 재생성한다. 프롬프트 규칙만으로 잡지 못하는 환각을 런타임에서 차단.
        """
        prompt = textwrap.dedent(f"""
            아래 학습 내용으로 "경로추적" 연습문제를 만들어주세요.
            실행 흐름을 단계별로 추적하며 객관식으로 답하는 유형입니다. steps는 3~5개.

            질문: {log.query}
            답변: {log.ai_response[:500]}

            JSON으로만 응답 (```없이):
            {{"scenario": "시나리오 설명", "steps": [{{"question": "질문", "choices": ["A","B","C","D"], "correct_index": 0, "correct_answer": "A", "explanation": "설명"}}]}}

            ⚠️ correct_index 규칙 (반드시 준수):
            - choices 배열의 0-based 인덱스 (첫 요소 = 0)
            - choices[correct_index]가 정답 값과 정확히 같아야 함
            - 예: choices=["1","2","3","4"], 정답="2" → correct_index=1 (choices[1]="2")
            - correct_index를 정한 뒤 choices[correct_index]로 검증하세요.

            ⚠️ correct_answer 규칙:
            - 정답의 실제 값(value). choices 배열 중 한 요소와 글자까지 정확히 같아야 함.
            - 항상 choices[correct_index]와 동일한 문자열을 넣으세요. (코드 레벨 검증용 ground truth)
        """).strip()

        content, raw_count = self._gen_and_validate(prompt)
        # 환각 1개라도 발생(통과 < raw) 또는 통과 step 부족이면 1회 재생성, 더 많은 쪽 채택
        all_ok = len(content.get('steps', [])) == raw_count and raw_count >= self.PATH_TRACE_MIN_STEPS
        if not all_ok:
            retry, _ = self._gen_and_validate(prompt)
            if len(retry.get('steps', [])) > len(content.get('steps', [])):
                content = retry
        if not content.get('steps'):
            raise ValueError("path_trace 출제 실패: 유효한 step이 없습니다")
        return content

    def _gen_and_validate(self, prompt):
        """
        LLM 호출 + JSON 파싱 + 검증을 묶음. 응답이 깨져도 재생성으로 흡수되도록 예외는 빈 결과로 강등.
        raw step 수도 같이 반환해 호출자가 "환각 발생 여부"(통과 < raw)를 판정할 수 있게 한다.
        """
        try:
            raw = self._call_mistral_json(prompt)
        except (json.JSONDecodeError, ValueError):
            return {'steps': []}, 0
        raw_count = len(raw.get('steps', []))
        return self._filter_valid_steps(raw), raw_count

    @staticmethod
    def _filter_valid_steps(content):
        """
        choices[correct_index] == correct_answer를 만족하는 step만 남긴다.
        결정적 규칙 검증(LLM 판정이 아닌 코드 비교)이라 환각이 통과할 여지가 없다.
        """
        valid = []
        for step in content.get('steps', []):
            ci = step.get('correct_index')
            ca = step.get('correct_answer')
            cs = step.get('choices', [])
            if isinstance(ci, int) and 0 <= ci < len(cs) and ca is not None and cs[ci] == ca:
                valid.append(step)
        content['steps'] = valid
        return content

    def _call_mistral_json(self, prompt):
        """
        Mistral에 JSON 응답을 요청한다.
        response_format=json_object가 모델 레벨에서 valid JSON을 보장하므로
        별도의 코드 펜스 처리 없이 바로 json.loads로 파싱한다.
        """
        response = self.mistral_client.chat.complete(
            model=self.MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=2000,
            response_format={"type": "json_object"},
        )
        return json.loads(response.choices[0].message.content)

    # ── 채점 ──────────────────────────────────────────────────────────

    def evaluate_attempt(self, exercise, user_answer):
        dispatch = {
            'path_trace': self._evaluate_path_trace,
            'generation_compare': self._evaluate_self_marked,
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

    def _evaluate_self_marked(self, exercise, user_answer):
        """
        자가 채점: 학습자가 직접 체크한 핵심 포인트 비율로 점수 산정.
        AI 호출 없음. ai_feedback 필드에는 reflection(있을 경우)을 저장한다.
        """
        key_points = exercise.content.get('key_points', [])
        total = max(len(key_points), 1)
        # 범위 정규화: 0 <= i < total
        covered = [i for i in user_answer.get('covered_indices', []) if 0 <= i < total]
        score = len(covered) / total
        return {
            'is_correct': score >= 0.6,
            'score': score,
            'ai_feedback': user_answer.get('reflection', ''),
        }

    # ── AI 한마디 (on-demand 보조 코멘트) ──────────────────────────────

    def generate_coach_comment(self, attempt):
        """학습자의 답·자가체크·회고를 보고 1~2문장 보조 코멘트 생성."""
        exercise = attempt.exercise
        ua = attempt.user_answer or {}
        key_points = exercise.content.get('key_points', [])
        covered_idx = set(i for i in ua.get('covered_indices', []) if 0 <= i < len(key_points))
        covered_str = ', '.join(p for i, p in enumerate(key_points) if i in covered_idx) or '(없음)'
        missed_str = ', '.join(p for i, p in enumerate(key_points) if i not in covered_idx) or '(없음)'
        prompt = textwrap.dedent(f"""
            학습자의 자가 학습을 1~2문장으로 짧게 코멘트 해주세요.
            평가/채점이 아니라 격려·보완 한마디입니다. 한국어로.

            질문: {exercise.content.get('question', '')}
            모범 답안: {exercise.content.get('model_answer', '') or '(없음)'}
            학습자 답: {ua.get('text', '')}
            본인이 체크한 포인트: {covered_str}
            빠뜨린 포인트: {missed_str}
            본인 회고: {ua.get('reflection', '') or '(없음)'}

            ⚠️ 1~2문장, 부드럽고 구체적으로. JSON 아닌 평문으로만 응답.
        """).strip()
        try:
            response = self.mistral_client.chat.complete(
                model=self.MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.6,
                max_tokens=120,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            return f"코멘트 생성 오류: {e}"

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
