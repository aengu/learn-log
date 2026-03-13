from django.apps import AppConfig
import os
import threading
import urllib.request

from django.apps import AppConfig


class SearchConfig(AppConfig):
    name = 'search'

    def ready(self):
        """render 환경이면 keep alive 데몬 실행"""
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