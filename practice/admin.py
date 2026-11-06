from django.contrib import admin

from .models import BKTFailureStep, BKTObservation, BKTUpdateFailure, Concept, ConceptMastery, Exercise, ExerciseAttempt, MateAttempt
from .services.bkt_lock import BKTLockedAdminMixin


@admin.register(Concept)
class ConceptAdmin(BKTLockedAdminMixin, admin.ModelAdmin):
    list_display = ['id', 'name', 'is_active']
    search_fields = ['id', 'name', 'description']
    list_filter = ['is_active']

@admin.register(Exercise)
class ExerciseAdmin(BKTLockedAdminMixin, admin.ModelAdmin):
    pass


admin.site.register(MateAttempt)


class ReadOnlyBKTAdmin(admin.ModelAdmin):
    """BKT는 직접 편집하지 않고 원본 풀이와 검토 상태에서 재계산한다."""

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ConceptMastery)
class ConceptMasteryAdmin(ReadOnlyBKTAdmin):
    list_display = ['concept', 'mastery', 'observation_count', 'correct_count', 'last_observed_at', 'model_version']
    list_select_related = ['concept']
    search_fields = ['concept__id', 'concept__name']


@admin.register(BKTObservation)
class BKTObservationAdmin(ReadOnlyBKTAdmin):
    list_display = ['attempt_id', 'step_index', 'concept', 'is_correct', 'mastery_before', 'mastery_after', 'observed_at']
    list_select_related = ['concept']
    list_filter = ['is_correct', 'concept']
    search_fields = ['concept__id', 'concept__name']


@admin.register(ExerciseAttempt)
class ExerciseAttemptAdmin(ReadOnlyBKTAdmin):
    """원본 풀이의 수정/삭제로 복구 근거가 사라지는 것을 관리자 경로에서 제한한다."""


@admin.register(BKTUpdateFailure)
class BKTUpdateFailureAdmin(ReadOnlyBKTAdmin):
    list_display = ['attempt_id', 'error_code', 'failed_step_index', 'created_at', 'resolved_at']


@admin.register(BKTFailureStep)
class BKTFailureStepAdmin(ReadOnlyBKTAdmin):
    list_display = ['id', 'failure_id', 'step_index', 'concept', 'status', 'exclusion_reason']
    list_filter = ['status', 'concept']
