from django.urls import path
from .views import MainPageView, QueryHTMXView, QueryAPIView

app_name = 'search'

urlpatterns = [
    # 메인 페이지
    path('', MainPageView.as_view(), name='main'),

    # HTMX 엔드포인트 (HTML 조각 반환)
    path('api/query/', QueryHTMXView.as_view(), name='query_api'),

    # REST API 엔드포인트 (JSON 반환)
    path('api/query/json/', QueryAPIView.as_view(), name='query_api_json'),
]
