from django.contrib.postgres.search import SearchVector, SearchQuery, SearchRank
from django.db import models
from django.utils import timezone


class Tag(models.Model):
    """태그 모델"""
    name = models.CharField(
        max_length=50, 
        unique=True, 
        verbose_name="태그명"
    )
    slug = models.SlugField(
        max_length=50, 
        unique=True, 
        verbose_name="슬러그"
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="생성일")
    
    class Meta:
        ordering = ['name']
        verbose_name = "태그"
        verbose_name_plural = "태그"
    
    def __str__(self):
        return self.name

class Reference(models.Model):
    """공식 문서 레퍼런스"""
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
        verbose_name="출처 유형"
    )
    fetched_at = models.DateTimeField(auto_now_add=True, verbose_name="수집일")
    
    class Meta:
        ordering = ['-fetched_at']
        verbose_name = "레퍼런스"
        verbose_name_plural = "레퍼런스"
    
    def __str__(self):
        return self.title


class LearningLog(models.Model):
    query = models.CharField(
        max_length=500, 
        db_index=True,
        verbose_name="질문"
    )
    ai_response = models.TextField(verbose_name="AI 답변")
    markdown_content = models.TextField(verbose_name="마크다운 내용")
    
    references = models.ManyToManyField(
        Reference,
        related_name='learning_logs',
        blank=True,
        verbose_name="참고 문서"
    )
    
    tags = models.ManyToManyField(
        Tag,
        related_name='learning_logs',
        blank=True,
        verbose_name="태그"
    )
    is_bookmarked = models.BooleanField(
        default=False,
        verbose_name="북마크"
    )
    view_count = models.PositiveIntegerField(
        default=0,
        verbose_name="조회수"
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="생성일")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="수정일")
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = "학습 로그"
        verbose_name_plural = "학습 로그"
        indexes = [
            models.Index(fields=['created_at']),
            models.Index(fields=['query']),
            models.Index(fields=['is_bookmarked']),
        ]
    
    def __str__(self):
        return f"{self.query[:50]}..."
    
    def increment_view_count(self):
        """조회수 증가"""
        self.view_count += 1
        self.save(update_fields=['view_count'])
    
    @classmethod
    def get_queryset(cls, q='', sort='latest', tags=None, bookmarked=False):  # noqa: E501
        """
        tag테이블까지 조인하여 검색과 정렬한 쿼리셋 반환
        검색: 질문(1.0), 답변(0.4) 가중치 순으로 full text search
        정렬: 연관순(검색인 경우), 최신순, 오래된순, 조회수순
        북마크
        """
        base = cls.objects.prefetch_related('tags')

        if bookmarked:
            base = base.filter(is_bookmarked=True)
        if tags:
            base = base.filter(tags__slug__in=tags).distinct()

        if q:
            vector = (
                SearchVector('query', weight='A', config='simple') +
                SearchVector('ai_response', weight='B', config='simple')
            )
            query = SearchQuery(q, config='simple')
            base = base.annotate(rank=SearchRank(vector, query)).filter(rank__gt=0)

        if sort == 'relevance' and q:
            return base.order_by('-rank')
        elif sort == 'views':
            return base.order_by('-view_count', '-created_at')
        elif sort == 'oldest':
            return base.order_by('created_at')
        return base.order_by('-created_at')


REVIEW_INTERVALS = [1, 3, 7, 14, 30]


class Exercise(models.Model):
    EXERCISE_TYPE_CHOICES = [
        ('generation_compare', '생성→비교'),
        ('path_trace', '경로추적'),
        ('retrieval_checkin', '인출 체크인'),
    ]

    learning_log = models.ForeignKey(
        LearningLog,
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
        ordering = ['next_review_at', '-created_at']
        verbose_name = "연습문제"
        verbose_name_plural = "연습문제"
        indexes = [
            models.Index(fields=['next_review_at']),
            models.Index(fields=['exercise_type']),
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
        ordering = ['-created_at']
        verbose_name = "풀이 시도"
        verbose_name_plural = "풀이 시도"

    def __str__(self):
        status = "정답" if self.is_correct else ("오답" if self.is_correct is False else "채점중")
        return f"{self.exercise} - {status}"


class Streak(models.Model):
    """
    연속 학습 기록 (싱글턴 — pk=1 하나만 사용).
    학습 로그 작성 또는 복습 정답 시 시그널을 통해 자동 갱신된다.
    """
    current_streak = models.PositiveIntegerField(default=0)
    longest_streak = models.PositiveIntegerField(default=0)
    last_active_date = models.DateField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "스트릭"
        verbose_name_plural = "스트릭"

    def __str__(self):
        return f"🔥 {self.current_streak}일 연속 (최장 {self.longest_streak}일)"

    @classmethod
    def load(cls):
        """싱글턴 인스턴스 반환 (없으면 생성)"""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def record_activity(self, date=None):
        """
        활동 기록. 연속 학습 판정 규칙:
        - 같은 날 중복 호출 → 무시
        - 어제 활동이 있었으면 → streak + 1
        - 그 외 (첫 활동 또는 하루 이상 빠짐) → streak = 1
        """
        today = date or timezone.now().date()
        if self.last_active_date == today:
            return  # 같은 날 중복 무시
        if self.last_active_date == today - timezone.timedelta(days=1):
            self.current_streak += 1  # 연속 유지
        else:
            self.current_streak = 1  # 리셋
        self.longest_streak = max(self.longest_streak, self.current_streak)
        self.last_active_date = today
        self.save(update_fields=['current_streak', 'longest_streak', 'last_active_date'])