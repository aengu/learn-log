import json
import textwrap

from groq import Groq
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ..models import Concept, Exercise, ExerciseAttempt
from .bkt_service import BKTService
from .exercise_context import build_exercise_context
from .exercise_prompt import build_choice_prompt


class ExerciseService:
    """
    연습문제 생성·채점·간격 반복 관리
    - generation_compare: 자가 마킹 채점 (LLM 호출 없음) + on-demand "AI 한마디"
    - path_trace: 인덱스 매칭 (JS 즉시 피드백 + 서버 저장)
    """

    # 출제(JSON 생성)는 무거운 모델, 짧은 평문 코멘트는 가벼운 모델.
    # gpt-oss는 답하기 전 추론 토큰을 쓴다 — max_tokens=120에서는 빈 문자열이 온다(실측).
    MODEL = "openai/gpt-oss-120b"
    LIGHT_MODEL = "qwen/qwen3.8-27b"

    def __init__(self):
        self.client = Groq(api_key=settings.GROQ_API_KEY)

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

    @staticmethod
    def _parent_context(log):
        """
        꼬리질문 로그용 출제 컨텍스트. 꼬리질문의 query는 지시어("그러면...")뿐이라
        부모 질문 없이는 출제 LLM이 주제를 모른다. 복습은 며칠 뒤 단독으로 풀므로
        question 자체를 자기완결적으로 쓰라는 지시도 함께 넣는다.
        """
        if not log.parent:
            return ""
        return (
            f"이전 질문(맥락): {log.parent.query}\n"
            "⚠️ 위 맥락을 참고하되, 출제하는 question은 이전 맥락 없이도 "
            "단독으로 이해 가능하게 작성하세요 (기술명·주제를 명시).\n"
        )

    def _gen_generation_compare(self, log):
        prompt = textwrap.dedent(f"""
            아래 학습 내용으로 "생성→비교" 연습문제를 만들어주세요.
            학습자가 먼저 답변을 쓰고, 모범 답안과 핵심 포인트를 보며 스스로 비교/채점합니다.

            {self._parent_context(log)}질문: {log.query}
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
        return self._call_json(prompt)

    PATH_TRACE_MIN_STEPS = 3   # 최소 통과 step 수 (미달 시 재생성 시도)
    PROMPT_VERSION = 'choice-v2'
    COGNITIVE_TYPES = {'concept_discrimination', 'application', 'execution_trace'}
    DIFFICULTIES = {'easy', 'medium', 'hard'}

    @staticmethod
    def _concept_candidates(log, context):
        """관련 이름·ID가 자료에 등장하는 등록 개념만 최대 10개 제공한다."""
        text = f'{log.query}\n{context}'.casefold()
        candidates = []
        for concept in Concept.objects.filter(is_active=True).iterator():
            names = [concept.name.casefold(), concept.pk.rsplit('.', 1)[-1].replace('_', ' ')]
            if any(name in text for name in names):
                candidates.append({'id': concept.pk, 'name': concept.name, 'description': concept.description})
            if len(candidates) >= 10:
                break
        return candidates

    def _gen_path_trace(self, log):
        """자료에 근거한 객관식 출제. 형식·태그를 검증하며 사실성은 보장하지 않는다."""
        context, context_audit = build_exercise_context(log)
        concepts = self._concept_candidates(log, context)
        prompt = build_choice_prompt(log.query, context, concepts, self._parent_context(log))

        content, raw_count = self._gen_and_validate(prompt)
        attempts = [content.get('_audit', {})]
        # 구조·태그 위반 또는 유효 step 부족이면 1회 재생성, 더 많은 쪽 채택
        all_ok = len(content.get('steps', [])) == raw_count and raw_count >= self.PATH_TRACE_MIN_STEPS
        if not all_ok:
            retry, _ = self._gen_and_validate(prompt)
            attempts.append(retry.get('_audit', {}))
            if len(retry.get('steps', [])) > len(content.get('steps', [])):
                content = retry
        # 채택하지 않은 시도의 탈락 사유도 남긴다. 버리면 발동률을 잴 수 없다.
        content['_audit'] = {'attempts': attempts}
        content['_generation'] = {
            'prompt_version': self.PROMPT_VERSION,
            'context': context_audit,
            'concept_ids': [concept['id'] for concept in concepts],
        }

        # 재생성 트리거가 아니라 출제 실패 조건이다.
        # 이전에는 빈 배열만 막아서, 4개가 조용히 사라진 1-step 문항이 그대로 나갔다.
        if len(content.get('steps', [])) < self.PATH_TRACE_MIN_STEPS:
            # 실패하면 content가 통째로 사라지므로 사유를 예외 메시지에 싣는다
            raise ValueError(
                f"path_trace 출제 실패: 유효한 step이 {len(content.get('steps', []))}개 "
                f"(최소 {self.PATH_TRACE_MIN_STEPS}개 필요) / audit={attempts}"
            )
        return content

    def _gen_and_validate(self, prompt):
        """
        LLM 호출 + JSON 파싱 + 검증을 묶음. 응답이 깨져도 재생성으로 흡수되도록 예외는 빈 결과로 강등.
        raw step 수도 같이 반환해 호출자가 구조·태그 위반에 따른 탈락 여부를 판정한다.
        """
        try:
            raw = self._call_json(prompt)
        except (json.JSONDecodeError, ValueError):
            return {'steps': []}, 0
        if not isinstance(raw, dict) or not isinstance(raw.get('steps'), list):
            return {'steps': [], '_audit': {'dropped': [{'reason': 'invalid-response-shape'}]}}, 0
        raw_count = len(raw['steps'])
        return self._filter_valid_steps(raw, require_tags=True), raw_count

    # ── 오답 품질 게이트 ───────────────────────────────────────────
    # 정답 자리(correct_index)가 맞는지는 _filter_valid_steps가 본다.
    # 여기서 보는 것은 "오답 3개가 기능하는가" — 요령으로 풀리는 문항을 잡는다.
    # 저작 규약 출처: github.com/midagedev/cachehit AUTHORING.md
    #
    # 발문 누출(§3-9) 검사는 넣지 않는다. 규약이 "기계로 검사되지 않는다"로 분류했고,
    # 저자가 구현해서 측정했으나 지목군과 대조군의 분포가 분리되지 않았다고 기록했다.
    # 한국어에서는 조사 때문에 "트랜잭션이" != "트랜잭션은"이라 겹침이 더 낮게 나온다.
    BANNED_CHOICE_PATTERNS = ("위의 모든", "모두 정답", "정답 없음", "해당 없음", "위 모두")
    ABSOLUTE_WORDS = ("항상", "절대", "모든", "반드시", "전혀", "결코", "무조건")
    DISTRACTOR_TYPES = {
        "adjacent", "inverted", "overgeneralized",
        "one-step-short", "vendor-mixup", "outdated", "plausible-number",
    }
    LENGTH_BIAS_RATIO = 1.4   # 정답 길이가 오답 평균의 이 배를 넘으면 "제일 긴 것 고르기"로 풀린다

    @classmethod
    def _audit_quality(cls, step):
        """
        (하드 위반, 소프트 위반)을 코드 목록으로 돌려준다.
        하드는 명백한 규칙 위반이라 step을 탈락시키고,
        소프트는 휴리스틱이라 기록만 한다(오판 가능 — 발동률을 재본 뒤 강제 여부를 정한다).
        """
        hard, soft = [], []
        choices = step.get("choices", [])
        ci = step.get("correct_index")
        wrong = [c for i, c in enumerate(choices) if i != ci]

        # H1. 금지 보기
        if any(p in str(c) for c in choices for p in cls.BANNED_CHOICE_PATTERNS):
            hard.append("banned-choice")

        # 오답 메타는 index별로 정확히 하나씩 있어야 한다.
        # 개수만 세면 중복 index로 개수를 채워 빈 why를 통과시킬 수 있다.
        meta = [d for d in (step.get("distractors") or []) if isinstance(d, dict)]
        want = {i for i in range(len(choices)) if i != ci} if ci is not None else set()
        by_index, duplicated = {}, False
        for d in meta:
            idx = d.get("index")
            if idx in by_index:
                duplicated = True
            by_index[idx] = d

        if duplicated or set(by_index) != want:
            # 결정적 검증이라 correct_index 검사와 같은 등급이다
            hard.append("distractor-index-mismatch")
        elif want:
            # 각 오답이 자기 why를 갖고 있는지 — 개수가 아니라 항목별로 본다
            if any(not str(by_index[i].get("why", "")).strip() for i in want):
                hard.append("missing-distractor-why")

            types = [by_index[i].get("type") for i in want]
            known = [t for t in types if t in cls.DISTRACTOR_TYPES]
            if len(known) != len(types):
                hard.append("unknown-distractor-type")

        # S1. 길이 편향
        if wrong and ci is not None and 0 <= ci < len(choices):
            avg_wrong = sum(len(str(c)) for c in wrong) / len(wrong)
            if avg_wrong and len(str(choices[ci])) > avg_wrong * cls.LENGTH_BIAS_RATIO:
                soft.append("length-bias")

        # S2. 단정 표현이 오답에만 몰리면 그 단어만 보고 소거된다
        if wrong and ci is not None and 0 <= ci < len(choices):
            in_wrong = sum(any(w in str(c) for w in cls.ABSOLUTE_WORDS) for c in wrong)
            in_correct = any(w in str(choices[ci]) for w in cls.ABSOLUTE_WORDS)
            if in_wrong == len(wrong) and not in_correct:
                soft.append("absolute-word-skew")

        return hard, soft

    @classmethod
    def _apply_quality_gates(cls, content):
        """하드 위반 step을 걸러내고, 소프트 위반은 step에 기록해 나중에 집계할 수 있게 남긴다."""
        audit = content.setdefault("_audit", {"raw_count": len(content.get("steps", [])), "dropped": []})
        kept = []
        for step in content.get("steps", []):
            hard, soft = cls._audit_quality(step)
            if hard:
                # 왜 버렸는지 남긴다. 없으면 잘못 버린 비율을 영영 측정할 수 없다.
                audit["dropped"].append({"reason": hard})
                continue
            if soft:
                step["_quality_flags"] = soft
            kept.append(step)
        content["steps"] = kept

        # C1. 정답 위치가 전 step에서 같으면 위치만 보고 찍을 수 있다
        idxs = [st.get("correct_index") for st in kept]
        if len(idxs) >= 3 and len(set(idxs)) == 1:
            for st in kept:
                st.setdefault("_quality_flags", []).append("correct-index-fixed")
        return content

    @classmethod
    def _filter_valid_steps(cls, content, require_tags=False):
        """구조·인덱스·문자열 일치와 태그를 검사한다. 정답의 사실성 검사는 아니다."""
        raw = content.get('steps', [])
        valid, dropped = [], []
        concept_ids = set(Concept.objects.filter(is_active=True).values_list('pk', flat=True)) if require_tags else set()
        for step in raw:
            if not isinstance(step, dict):
                dropped.append({'reason': 'invalid-step-shape'})
                continue
            ci = step.get('correct_index')
            ca = step.get('correct_answer')
            cs = step.get('choices', [])
            if not isinstance(cs, list) or len(cs) != 4 or any(not isinstance(c, str) or not c.strip() for c in cs):
                dropped.append({'reason': 'invalid-choices'})
                continue
            if len({choice.strip().casefold() for choice in cs}) != len(cs):
                dropped.append({'reason': 'duplicate-choices'})
                continue
            if type(ci) is not int or not 0 <= ci < len(cs) or cs[ci] != ca:
                dropped.append({'reason': 'correct-index-mismatch'})
                continue
            if require_tags:
                if any(not isinstance(step.get(key), str) or not step[key].strip() for key in ('question', 'explanation')):
                    dropped.append({'reason': 'missing-question-or-explanation'})
                    continue
                reason = cls._validate_step_tags(step, concept_ids)
                if reason:
                    dropped.append({'reason': reason})
                    continue
            metadata = step.get('distractors')
            if not isinstance(metadata, list) or any(
                not isinstance(item, dict) or type(item.get('index')) is not int
                or not isinstance(item.get('type'), str) or not isinstance(item.get('why'), str)
                for item in metadata
            ):
                dropped.append({'reason': 'invalid-distractor-shape'})
                continue
            valid.append(step)
        content['steps'] = valid
        content['_audit'] = {'raw_count': len(raw), 'dropped': dropped}
        return cls._apply_quality_gates(content)

    @classmethod
    def _validate_step_tags(cls, step, concept_ids):
        if not isinstance(step.get('cognitive_type'), str) or step['cognitive_type'] not in cls.COGNITIVE_TYPES:
            return 'unknown-cognitive-type'
        if not isinstance(step.get('estimated_difficulty'), str) or step['estimated_difficulty'] not in cls.DIFFICULTIES:
            return 'unknown-difficulty'
        concept_id = step.get('concept_id')
        candidate = step.get('concept_candidate')
        if concept_id is not None:
            if not isinstance(concept_id, str) or concept_id not in concept_ids or candidate is not None:
                return 'invalid-concept-id'
        elif not isinstance(candidate, dict) or any(
            not isinstance(candidate.get(key), str) or not 0 < len(candidate[key].strip()) <= limit
            for key, limit in [('name', 100), ('description', 300)]
        ):
            return 'missing-concept-candidate'
        return None

    def _call_json(self, prompt):
        """
        JSON 응답을 요청한다.
        response_format=json_object가 모델 레벨에서 valid JSON을 보장하므로
        별도의 코드 펜스 처리 없이 바로 json.loads로 파싱한다.
        (Groq은 이 옵션을 쓸 때 프롬프트에 'json'이라는 단어가 있어야 한다.)

        max_tokens는 3000. gpt-oss는 서술이 길어 2000에서 JSON이 잘리는 일이 있었고,
        잘리면 파싱이 실패해 step이 통째로 날아간다. 상한일 뿐이라 실제 소비는 늘지 않는다.
        """
        response = self.client.chat.completions.create(
            model=self.MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=3000,
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
        key_point_count = len(key_points)
        total = max(key_point_count, 1)
        # 범위 밖 인덱스와 중복 체크는 점수에 포함하지 않는다.
        covered = {
            i for i in user_answer.get('covered_indices', [])
            if isinstance(i, int) and 0 <= i < key_point_count
        }
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
            response = self.client.chat.completions.create(
                model=self.LIGHT_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.6,
                max_tokens=120,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            return f"코멘트 생성 오류: {e}"

    # ── 저장 & 간격 반복 ────────────────────────────────────────────────

    @transaction.atomic
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
        BKTService.record_attempt(attempt)
        return attempt

    @staticmethod
    def get_due_exercises():
        return (
            Exercise.objects
            .filter(Q(next_review_at__isnull=True) | Q(next_review_at__lte=timezone.now()))
            .select_related('learning_log')
            .order_by('next_review_at', '-created_at')
        )
