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

            {self._parent_context(log)}질문: {log.query}
            답변: {log.ai_response[:500]}

            JSON으로만 응답 (```없이):
            {{"scenario": "시나리오 설명", "steps": [{{"question": "질문", "choices": ["A","B","C","D"], "correct_index": 0, "correct_answer": "A", "explanation": "설명", "distractors": [{{"index": 1, "type": "adjacent", "why": "이 보기를 고른 사람이 오해한 것"}}, {{"index": 2, "type": "one-step-short", "why": "..."}}, {{"index": 3, "type": "inverted", "why": "..."}}]}}]}}

            ⚠️ correct_index 규칙 (반드시 준수):
            - choices 배열의 0-based 인덱스 (첫 요소 = 0)
            - choices[correct_index]가 정답 값과 정확히 같아야 함
            - 예: choices=["1","2","3","4"], 정답="2" → correct_index=1 (choices[1]="2")
            - correct_index를 정한 뒤 choices[correct_index]로 검증하세요.

            ⚠️ correct_answer 규칙:
            - 정답의 실제 값(value). choices 배열 중 한 요소와 글자까지 정확히 같아야 함.
            - 항상 choices[correct_index]와 동일한 문자열을 넣으세요. (코드 레벨 검증용 ground truth)

            ⚠️ 오답 규칙 (문제의 품질은 정답이 아니라 오답이 결정합니다):
            - distractors에 오답 3개를 전부 적으세요. index는 correct_index가 아닌 나머지 셋.
            - why: "이 보기를 고른 사람은 무엇을 오해한 것인가"를 한 문장으로.
              쓸 수 없는 보기는 아무도 안 고르는 죽은 보기이므로 다른 오답으로 바꾸세요.
            - type은 다음 중 하나:
              adjacent(인접 개념 치환) / inverted(방향·주체를 뒤집음) /
              overgeneralized(조건부 참을 단정) / one-step-short(부분적으로 맞지만 핵심 누락) /
              vendor-mixup(다른 버전·다른 도구의 동작) / outdated(예전엔 맞았던 것) /
              plausible-number(자릿수·단위가 그럴듯하게 틀림)
            - 오답 3개가 전부 같은 type이면 안 됩니다. 최소 2종을 섞으세요.
            - one-step-short를 최소 1개 넣으세요. 아는 사람과 어설프게 아는 사람을 가르는 것은
              대개 "거의 맞았지만 핵심을 빠뜨린 답"입니다.

            ⚠️ 요령으로 풀리지 않게:
            - 정답만 길게 쓰지 마세요. 네 보기의 길이와 서술 밀도를 맞추세요.
              정답이 메커니즘을 말하면 오답도 (틀린) 메커니즘을 말해야 합니다.
            - "위의 모든 것", "정답 없음", "해당 없음" 금지.
            - 발문에 정답을 흘리지 마세요. 발문과 어휘가 가장 많이 겹치는 보기가 정답이면 실패입니다.
            - "항상/절대/모든/반드시" 같은 단정 표현이 오답에만 몰리면 안 됩니다.
        """).strip()

        content, raw_count = self._gen_and_validate(prompt)
        attempts = [content.get('_audit', {})]
        # 환각 1개라도 발생(통과 < raw) 또는 통과 step 부족이면 1회 재생성, 더 많은 쪽 채택
        all_ok = len(content.get('steps', [])) == raw_count and raw_count >= self.PATH_TRACE_MIN_STEPS
        if not all_ok:
            retry, _ = self._gen_and_validate(prompt)
            attempts.append(retry.get('_audit', {}))
            if len(retry.get('steps', [])) > len(content.get('steps', [])):
                content = retry
        # 채택하지 않은 시도의 탈락 사유도 남긴다. 버리면 발동률을 잴 수 없다.
        content['_audit'] = {'attempts': attempts}

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
        raw step 수도 같이 반환해 호출자가 "환각 발생 여부"(통과 < raw)를 판정할 수 있게 한다.
        """
        try:
            raw = self._call_mistral_json(prompt)
        except (json.JSONDecodeError, ValueError):
            return {'steps': []}, 0
        raw_count = len(raw.get('steps', []))
        return self._filter_valid_steps(raw), raw_count

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

        if meta and (duplicated or set(by_index) != want):
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
            elif len(known) >= 2 and len(set(known)) < 2:
                # 오답 타입이 전부 같으면 응시자가 패턴을 학습한다
                hard.append("uniform-distractor-type")

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

    @staticmethod
    def _filter_valid_steps(content):
        """
        choices[correct_index] == correct_answer를 만족하는 step만 남긴다.
        결정적 규칙 검증(LLM 판정이 아닌 코드 비교)이라 환각이 통과할 여지가 없다.
        """
        raw = content.get('steps', [])
        valid, dropped = [], []
        for step in raw:
            ci = step.get('correct_index')
            ca = step.get('correct_answer')
            cs = step.get('choices', [])
            if isinstance(ci, int) and 0 <= ci < len(cs) and ca is not None and cs[ci] == ca:
                valid.append(step)
            else:
                dropped.append({'reason': 'correct-index-mismatch'})
        content['steps'] = valid
        content['_audit'] = {'raw_count': len(raw), 'dropped': dropped}
        return ExerciseService._apply_quality_gates(content)

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
