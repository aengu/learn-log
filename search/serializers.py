from rest_framework import serializers
from .models import LearningLog, Tag, Reference


class TagSerializer(serializers.ModelSerializer):
    log_count = serializers.ReadOnlyField()

    class Meta:
        model = Tag
        fields = ['id', 'name', 'slug', 'log_count', 'created_at']


class ReferenceSerializer(serializers.ModelSerializer):
    source_type_display = serializers.CharField(source='get_source_type_display', read_only=True)

    class Meta:
        model = Reference
        fields = ['id', 'url', 'title', 'excerpt', 'source_type', 'source_type_display', 'created_at']


class LearningLogListSerializer(serializers.ModelSerializer):
    """
    목록 조회용 - 간략한 정보만 포함
    """
    tags = TagSerializer(many=True, read_only=True)
    reference_count = serializers.SerializerMethodField()

    class Meta:
        model = LearningLog
        fields = ['id', 'query', 'tags', 'reference_count', 'view_count', 'created_at']

    def get_reference_count(self, obj):
        return obj.references.count()


class LearningLogDetailSerializer(serializers.ModelSerializer):
    """
    상세 조회용 - 모든 정보 포함
    """
    tags = TagSerializer(many=True, read_only=True)
    references = ReferenceSerializer(many=True, read_only=True)

    class Meta:
        model = LearningLog
        fields = [
            'id', 'query', 'ai_response', 'markdown_content',
            'tags', 'references', 'view_count', 'created_at', 'updated_at'
        ]


class QueryInputSerializer(serializers.Serializer):
    """
    질문 입력용 시리얼라이저
    """
    query = serializers.CharField(
        max_length=500,
        required=True,
        help_text="개발 관련 질문을 입력하세요"
    )

    def validate_query(self, value):
        if len(value.strip()) < 5:
            raise serializers.ValidationError("질문은 최소 5자 이상이어야 합니다.")
        return value.strip()
