"""로컬 Docker DB → Render DB로 데이터 밀어넣기"""
import os
import subprocess
import tempfile

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "로컬 Docker DB를 Render 프로덕션 DB로 밀어넣습니다"

    def add_arguments(self, parser):
        parser.add_argument(
            "--no-confirm",
            action="store_true",
            help="확인 프롬프트 없이 바로 실행",
        )

    def handle(self, *args, **options):
        remote_url = os.getenv("REMOTE_DATABASE_URL")
        if not remote_url:
            raise CommandError(
                "REMOTE_DATABASE_URL 환경변수가 설정되지 않았습니다.\n"
                ".env 파일에 Render External Database URL을 추가하세요:\n"
                "REMOTE_DATABASE_URL=postgresql://user:pass@host:port/dbname"
            )

        if not options["no_confirm"]:
            self.stdout.write(
                self.style.WARNING(
                    "⚠ Render 프로덕션 DB의 모든 데이터가 로컬 데이터로 교체됩니다!\n"
                    "  이 작업은 되돌릴 수 없습니다."
                )
            )
            confirm = input("정말 계속하시겠습니까? (yes를 입력): ")
            if confirm != "yes":
                self.stdout.write("취소되었습니다.")
                return

        local = self._parse_local_db()

        with tempfile.NamedTemporaryFile(suffix=".dump", delete=False) as f:
            dump_path = f.name

        try:
            self._dump_local(local, dump_path)
            self._restore_remote(remote_url, dump_path)
        finally:
            if os.path.exists(dump_path):
                os.unlink(dump_path)

        self.stdout.write(self.style.SUCCESS("✓ dbpush 완료! 로컬 → Render 동기화 성공"))

    def _parse_local_db(self):
        """docker-compose.yml의 DB 설정을 반환"""
        return {
            "host": os.getenv("POSTGRES_HOST", "db"),
            "port": "5432",
            "name": os.getenv("POSTGRES_DB", "learnlog"),
            "user": os.getenv("POSTGRES_USER", "learnlog_user"),
            "password": os.getenv("POSTGRES_PASSWORD", "learnlog_password"),
        }

    def _dump_local(self, local, dump_path):
        """로컬 Docker DB를 pg_dump로 덤프"""
        self.stdout.write("로컬 DB 덤프 중...")
        env = os.environ.copy()
        env["PGPASSWORD"] = local["password"]
        result = subprocess.run(
            [
                "pg_dump",
                "--no-owner",
                "--no-privileges",
                "--format=custom",
                f"--file={dump_path}",
                f"--host={local['host']}",
                f"--port={local['port']}",
                f"--username={local['user']}",
                local["name"],
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode != 0:
            raise CommandError(f"pg_dump 실패:\n{result.stderr}")
        self.stdout.write(self.style.SUCCESS("  덤프 완료"))

    def _restore_remote(self, url, dump_path):
        """Render DB에 복원"""
        self.stdout.write("Render DB 복원 중...")
        result = subprocess.run(
            [
                "pg_restore",
                "--clean",
                "--if-exists",
                "--no-owner",
                "--no-privileges",
                f"--dbname={url}",
                dump_path,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 and "error" in result.stderr.lower():
            raise CommandError(f"pg_restore 실패:\n{result.stderr}")
        self.stdout.write(self.style.SUCCESS("  복원 완료"))
