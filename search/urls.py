from django.urls import path
from .views import MainPageView, QueryHTMXView, QueryAPIView, QuerySSEView

app_name = 'search'

urlpatterns = [
    # 메인 페이지
    path('', MainPageView.as_view(), name='main'),

    # HTMX 엔드포인트 (HTML 조각 반환)
    path('api/query/html', QueryHTMXView.as_view(), name='query_api_html'),
    # REST API 엔드포인트 (JSON 반환)
    path('api/query/json/', QueryAPIView.as_view(), name='query_api_json'),
    # REST API 엔드포인트 (steam 반환)
    path('api/query/stream/', QuerySSEView.as_view(), name='query_api_stream'),
]
