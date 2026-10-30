from django.db import models
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.utils import timezone


REVIEW_INTERVALS = [1, 3, 7, 14, 30]


class Concept(models.Model):
    """검토 후 등록한 개념 사전. 새 출제 개념 후보는 문항 JSON에만 보관한다."""

    id = models.CharField(
        primary_key=True,
        max_length=100,
        validators=[RegexValidator(r'^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$', '영문 소문자·숫자·밑줄과 점으로 작성하세요.')],
        verbose_name="개념 ID",
    )
    name = models.CharField(max_length=100, verbose_name="개념명")
    description = models.CharField(max_length=300, verbose_name="설명")
    is_active = models.BooleanField(default=True, verbose_name="출제 사용")

    class Meta:
        ordering = ['id']
        verbose_name = "출제 개념"
        verbose_name_plural = "출제 개념"

    def __str__(self):
        return f"{self.name} ({self.pk})"


class Exercise(models.Model):
    EXERCISE_TYPE_CHOICES = [
        ('generation_compare', '직접 답하기'),
        ('path_trace', '선택지 고르기'),
    ]

    learning_log = models.ForeignKey(
        'search.LearningLog',
        on_delete=models.CASCADE,
        related_name='exercises',
        verbose_name="학습 로그"
    )
    exercise_type = models.CharField(
        max_length=30,
        choices=EXERCISE_TYPE_CHOICES,
        verbose_name="유형"
    )
    content = models.JSONField(verbose_name="문제 내용")
    review_interval = models.PositiveIntegerField(default=1, verbose_name="복습 주기(일)")
    next_review_at = models.DateTimeField(null=True, blank=True, verbose_name="다음 복습일")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="생성일")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="수정일")

    class Meta:
        db_table = 'search_exercise'
        ordering = ['next_review_at', '-created_at']
        verbose_name = "연습문제"
        verbose_name_plural = "연습문제"
        indexes = [
            models.Index(fields=['next_review_at'], name='search_exer_next_re_2c3b76_idx'),
            models.Index(fields=['exercise_type'], name='search_exer_exercis_28526e_idx'),
        ]

    def __str__(self):
        return f"[{self.get_exercise_type_display()}] {self.learning_log.query[:40]}"

    def is_due(self):
        if self.next_review_at is None:
            return True
        return timezone.now() >= self.next_review_at

    def advance_interval(self):
        """마지막 성공 기준으로 다음 복습일 계산 (1→3→7→14→30일)"""
        try:
            idx = REVIEW_INTERVALS.index(self.review_interval)
            next_interval = REVIEW_INTERVALS[min(idx + 1, len(REVIEW_INTERVALS) - 1)]
        except ValueError:
            next_interval = 1
        self.review_interval = next_interval
        self.next_review_at = timezone.now() + timezone.timedelta(days=next_interval)
        self.save(update_fields=['review_interval', 'next_review_at'])

    def reset_interval(self):
        """오답 시 1일로 리셋"""
        self.review_interval = 1
        self.next_review_at = timezone.now() + timezone.timedelta(days=1)
        self.save(update_fields=['review_interval', 'next_review_at'])


class ExerciseAttempt(models.Model):
    exercise = models.ForeignKey(
        Exercise,
        on_delete=models.CASCADE,
        related_name='attempts',
        verbose_name="연습문제"
    )
    user_answer = models.JSONField(verbose_name="사용자 답변")
    is_correct = models.BooleanField(null=True, blank=True, verbose_name="정답 여부")
    ai_feedback = models.TextField(blank=True, verbose_name="AI 피드백")
    score = models.FloatField(null=True, blank=True, verbose_name="점수(0~1)")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="시도일")

    class Meta:
        db_table = 'search_exerciseattempt'
        ordering = ['-created_at']
        verbose_name = "풀이 시도"
        verbose_name_plural = "풀이 시도"

    def __str__(self):
        status = "정답" if self.is_correct else ("오답" if self.is_correct is False else "채점중")
        return f"{self.exercise} - {status}"


class MateAttempt(models.Model):
    """러닝메이트가 path_trace 문제를 풀은 기록.

    러닝메이트는 단일 개체이므로 별도의 Mate 모델을 두지 않는다.
    user_attempt가 연결되기 전까지 답은 서버에만 보관하고, 사용자 제출 후 공개한다.
    """

    STATUS_CHOICES = [
        ('pending', '풀이 대기'),
        ('solving', '풀이 중'),
        ('ready', '풀이 완료'),
        ('failed', '풀이 실패'),
    ]

    exercise = models.ForeignKey(
        Exercise,
        on_delete=models.CASCADE,
        related_name='mate_attempts',
        verbose_name="연습문제",
    )
    user_attempt = models.OneToOneField(
        ExerciseAttempt,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='mate_attempt',
        verbose_name="비교할 사용자 풀이",
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='pending',
        db_index=True,
        verbose_name="상태",
    )
    answer = models.JSONField(default=dict, blank=True, verbose_name="러닝메이트 답")
    score = models.FloatField(null=True, blank=True, verbose_name="점수(0~1)")
    is_correct = models.BooleanField(null=True, blank=True, verbose_name="통과 여부")
    context_snapshot = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="수준 추정·샘플링 정보",
    )
    model_name = models.CharField(max_length=100, blank=True, verbose_name="풀이 모델")
    prompt_version = models.CharField(max_length=20, default='v1', verbose_name="프롬프트 버전")
    error_message = models.TextField(blank=True, verbose_name="오류 메시지")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="생성일")
    answered_at = models.DateTimeField(null=True, blank=True, verbose_name="풀이 완료일")

    class Meta:
        db_table = 'search_mateattempt'
        ordering = ['-created_at']
        verbose_name = "러닝메이트 풀이"
        verbose_name_plural = "러닝메이트 풀이"
        indexes = [
            models.Index(fields=['exercise', 'created_at'], name='search_mate_exercis_b37cba_idx'),
        ]

    def __str__(self):
        return f"러닝메이트 - {self.exercise} ({self.get_status_display()})"


class ConceptMastery(models.Model):
    """단일 사용자의 개념별 BKT 상태. 관측 이력에서 재구성 가능한 계산 결과다."""

    concept = models.OneToOneField(Concept, on_delete=models.CASCADE, related_name='user_mastery')
    mastery = models.FloatField(validators=[MinValueValidator(0), MaxValueValidator(1)])
    observation_count = models.PositiveIntegerField(default=0)
    correct_count = models.PositiveIntegerField(default=0)
    last_observed_at = models.DateTimeField(null=True, blank=True)
    model_version = models.CharField(max_length=30)
    parameters = models.JSONField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['concept_id']
        verbose_name = '사용자 개념 숙련도'
        verbose_name_plural = '사용자 개념 숙련도'
        constraints = [
            models.CheckConstraint(condition=models.Q(mastery__gte=0, mastery__lte=1), name='bkt_mastery_probability_range'),
            models.CheckConstraint(condition=models.Q(correct_count__lte=models.F('observation_count')), name='bkt_mastery_correct_count'),
        ]

    def __str__(self):
        return f'{self.concept}: {self.mastery:.1%} ({self.observation_count}문항)'


class BKTObservation(models.Model):
    """승인 문항의 정오답과 갱신 전후 상태. attempt + step_index는 한 번만 반영한다."""

    attempt = models.ForeignKey(ExerciseAttempt, on_delete=models.CASCADE, related_name='bkt_observations')
    concept = models.ForeignKey(Concept, on_delete=models.CASCADE, related_name='bkt_observations')
    step_index = models.PositiveIntegerField(help_text='세트 안의 0부터 시작하는 원본 문항 인덱스')
    is_correct = models.BooleanField()
    mastery_before = models.FloatField(validators=[MinValueValidator(0), MaxValueValidator(1)])
    predicted_correct = models.FloatField(validators=[MinValueValidator(0), MaxValueValidator(1)])
    mastery_posterior = models.FloatField(validators=[MinValueValidator(0), MaxValueValidator(1)])
    mastery_after = models.FloatField(validators=[MinValueValidator(0), MaxValueValidator(1)])
    parameters = models.JSONField()
    model_version = models.CharField(max_length=30)
    step_snapshot = models.JSONField()
    observed_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['observed_at', 'attempt_id', 'step_index']
        verbose_name = 'BKT 관측 이력'
        verbose_name_plural = 'BKT 관측 이력'
        constraints = [
            models.UniqueConstraint(fields=['attempt', 'step_index'], name='bkt_unique_attempt_step'),
            models.CheckConstraint(
                condition=models.Q(
                    mastery_before__gte=0, mastery_before__lte=1,
                    predicted_correct__gte=0, predicted_correct__lte=1,
                    mastery_posterior__gte=0, mastery_posterior__lte=1,
                    mastery_after__gte=0, mastery_after__lte=1,
                ),
                name='bkt_observation_probability_range',
            ),
        ]

