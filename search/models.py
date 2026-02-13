from django.contrib.postgres.search import SearchVector, SearchQuery, SearchRank
from django.db import models


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
    def get_queryset(cls, q='', sort='latest', tags=None):
        """
        tag테이블까지 조인하여 검색과 정렬한 쿼리셋 반환
        검색: 질문(1.0), 답변(0.4) 가중치 순으로 full text search
        정렬: 연관순(검색인 경우), 최신순, 오래된순, 조회수순
        """
        base = cls.objects.prefetch_related('tags')

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