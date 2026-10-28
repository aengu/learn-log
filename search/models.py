from django.contrib.postgres.search import SearchVector, SearchQuery, SearchRank
from django.db import models
from django.utils import timezone
from pgvector.django import VectorField


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

    parent = models.ForeignKey(
        'self',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='follow_ups',
        verbose_name="부모 질문"
    )

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
    embedding = VectorField(
        dimensions=1024,  # mistral-embed 차원
        null=True,
        blank=True,
        verbose_name="임베딩"
    )

    ANSWER_SOURCE_CHOICES = [
        ('both', '내 기록 + 웹 문서'),
        ('logs', '내 기록 기반'),
        ('web', '웹 문서 기반'),
        ('none', '모델 지식만'),
    ]
    VERIFICATION_CHOICES = [
        ('pending', '검증 대기'),
        ('passed', '컨텍스트 일치'),
        ('suspect', '컨텍스트 불일치 의심'),
    ]
    answer_source = models.CharField(
        max_length=10,
        choices=ANSWER_SOURCE_CHOICES,
        blank=True,
        default='',  # 기능 도입 전 로그는 빈 값 (배지 미표시)
        verbose_name="답변 출처"
    )
    is_truncated = models.BooleanField(
        default=False,
        verbose_name="답변 잘림(max_tokens)"
    )
    verification = models.CharField(
        max_length=10,
        choices=VERIFICATION_CHOICES,
        blank=True,
        default='',  # 빈 값 = 미검증 (컨텍스트 없음 또는 검증 실패)
        verbose_name="검증 상태"
    )
    verification_note = models.TextField(
        blank=True,
        verbose_name="검증 메모"
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

    @property
    def root(self):
        """꼬리질문 체인의 시작 질문 (부모 없으면 자기 자신)"""
        node = self
        while node.parent_id:
            node = node.parent
        return node
    
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

class DailyJournal(models.Model):
    """
    일일 학습일지 — 해당 날짜의 활동 통계 + LLM 한 줄 요약.
    cron 없이 자정 이후 첫 방문 시점에 lazy 생성된다 (JournalService.ensure_journal).
    """
    date = models.DateField(unique=True, verbose_name="날짜")
    streak_day = models.PositiveIntegerField(default=0, verbose_name="불꽃 n일차")
    question_count = models.PositiveIntegerField(default=0, verbose_name="질문 수")
    attempt_count = models.PositiveIntegerField(default=0, verbose_name="연습문제 시도 수")
    pass_count = models.PositiveIntegerField(default=0, verbose_name="통과 수")
    fail_count = models.PositiveIntegerField(default=0, verbose_name="미통과 수")
    summary = models.TextField(blank=True, verbose_name="LLM 요약")
    is_dismissed = models.BooleanField(default=False, verbose_name="팝업 다시보지않기")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="생성일")

    class Meta:
        ordering = ['-date']
        verbose_name = "일일 학습일지"
        verbose_name_plural = "일일 학습일지"

    def __str__(self):
        return f"{self.date} 학습일지 (🔥{self.streak_day}일차)"
