"""BKT 쓰기 작업의 공통 입구. 반드시 가장 바깥 atomic에서 먼저 획득한다."""
from django.db import transaction
from django.utils import timezone

from ..models import BKTLock


def acquire_bkt_lock():
    # 실행 시 get_or_create하지 않는다. 행 누락은 잠금 없이 진행하지 않고 실패시킨다.
    return BKTLock.objects.select_for_update().get(pk=1)


def pending_concept_ids():
    from ..models import BKTFailureStep

    return set(BKTFailureStep.objects.filter(status='pending').values_list('concept_id', flat=True))


@transaction.atomic
def confirm_exclusion(step_id, reason):
    """제외 의사만 기록한다. 실제 실패 해소는 rebuild와 함께 커밋한다."""
    from ..models import BKTFailureStep

    acquire_bkt_lock()
    if not reason.strip():
        raise ValueError('제외 확정 사유가 필요합니다.')
    step = BKTFailureStep.objects.get(pk=step_id, status='pending')
    step.exclusion_reason = reason.strip()
    step.exclusion_confirmed_at = timezone.now()
    step.save(update_fields=['exclusion_reason', 'exclusion_confirmed_at'])


class BKTLockedAdminMixin:
    """관리자 변경도 공통 잠금을 먼저 획득한다. 무잠금 삭제는 제공하지 않는다."""

    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        if request.method != 'POST':
            return super().changeform_view(request, object_id, form_url, extra_context)
        with transaction.atomic():
            acquire_bkt_lock()
            return super().changeform_view(request, object_id, form_url, extra_context)

    def has_delete_permission(self, request, obj=None):
        return False
