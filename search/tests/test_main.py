from django.test import TestCase, Client
from django.urls import reverse


class MainPageTest(TestCase):
    def setUp(self):
        self.client = Client()

    def test_main_page_returns_200(self):
        response = self.client.get(reverse('search:main'))
        self.assertEqual(response.status_code, 200)

    def test_main_page_uses_correct_template(self):
        response = self.client.get(reverse('search:main'))
        self.assertTemplateUsed(response, 'search/main.html')
