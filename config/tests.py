from django.test import SimpleTestCase


class HealthCheckTest(SimpleTestCase):
    """Cloud Run이 컨테이너 생존을 판단하는 근거라 깨지면 배포가 실패한다."""

    def test_healthz_returns_ok(self):
        # secure=True로 보내야 한다. 테스트는 DEBUG=False로 돌고,
        # 그때 SECURE_SSL_REDIRECT가 켜져 http 요청은 301로 튕긴다 (운영과 동일한 동작).
        res = self.client.get("/healthz", secure=True)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"status": "ok"})
