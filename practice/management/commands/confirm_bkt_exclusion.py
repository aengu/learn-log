"""미해결 step을 계산에서 제외하기로 확정한다. 해소는 rebuild 적용 시 수행한다."""
from django.core.management.base import BaseCommand, CommandError

from practice.models import BKTFailureStep
from practice.services.bkt_lock import confirm_exclusion


class Command(BaseCommand):
    help = 'BKT 실패 step의 제외 사유를 기록합니다. 이후 rebuild_bkt --apply가 필요합니다.'

    def add_arguments(self, parser):
        parser.add_argument('step_id', type=int, help='rebuild 미리보기의 failure_step_id')
        parser.add_argument('--reason', required=True)

    def handle(self, *args, **options):
        try:
            confirm_exclusion(options['step_id'], options['reason'])
        except (ValueError, BKTFailureStep.DoesNotExist) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write('제외 의사를 기록했습니다. 문항을 보류한 후 rebuild_bkt 미리보기와 --apply를 실행하세요.')
