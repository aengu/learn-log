"""BKT 내부 메서드를 직접 검증할 때 호출자의 공통 잠금 계약을 지킨다."""
from django.db import transaction

from practice.services.bkt_lock import acquire_bkt_lock
from practice.services.bkt_service import BKTService


@transaction.atomic
def record_with_lock(attempt):
    acquire_bkt_lock()
    return BKTService.record_attempt(attempt)


@transaction.atomic
def capture_with_lock(attempt):
    acquire_bkt_lock()
    return BKTService.capture_attempt(attempt)
