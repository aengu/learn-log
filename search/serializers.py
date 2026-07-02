from rest_framework import serializers
from .models import LearningLog


class LearningLogUpdateSerializer(serializers.ModelSerializer):
    """
    학습로그 부분 수정용 (북마크 등)
    """
    class Meta:
        model = LearningLog
        fields = ['is_bookmarked']
