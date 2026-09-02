from django.apps import AppConfig
import os
import threading
import urllib.request

from django.apps import AppConfig


class SearchConfig(AppConfig):
    name = 'search'

    def ready(self):
        import search.signals  # noqa: F401

        # 자가 핑은 기본으로 꺼둔다.
        # 프로세스가 살아 있는 동안만 도는 구조라 "재우는 것"은 되지만
        # "다시 깨우는 것"은 안 된다(잠들면 이 스레드도 같이 멈춤).
        # 무료 플랜은 계정당 월 750시간이라 두 서비스를 24시간 깨워두면 한도를 넘겨서,
        # 깨우는 일은 외부 스케줄러(quartz-learnlog 레포의 keep-awake 워크플로)가
        # 평일 업무시간에만 하도록 옮겼다. 되살리려면 SELF_PING=true 를 주면 된다.
        if os.environ.get('SELF_PING', '').lower() != 'true':
            return

        render_url = os.environ.get('RENDER_EXTERNAL_URL')
        if not render_url:
            return

        def keep_alive():
            import time
            while True:
                time.sleep(600)  # 10분
                try:
                    urllib.request.urlopen(render_url)
                except Exception:
                    pass

        thread = threading.Thread(target=keep_alive, daemon=True)
        thread.start()