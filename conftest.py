"""transaction=True 테스트의 flush 후에도 마이그레이션의 고정 잠금 행을 복원한다."""
import pytest


@pytest.fixture(autouse=True)
def bkt_lock_row(request):
    marker = request.node.get_closest_marker('django_db')
    if marker or {'db', 'transactional_db'} & set(request.fixturenames):
        request.getfixturevalue('db')
        from practice.models import BKTLock

        BKTLock.objects.get_or_create(pk=1)
