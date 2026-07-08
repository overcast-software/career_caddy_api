"""Trusted `jh_` service API keys are exempt from DRF rate throttling.

Anonymous and JWT (frontend) traffic must stay throttled exactly as before.
Exercised at the throttle-class level (independent of the environment-based
`BaseViewSet.get_throttles` dev-disable), which is the reliable surface for
the exemption contract.
"""

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.tokens import AccessToken

from job_hunting.api.authentication import ApiKeyAuthentication
from job_hunting.api.throttling import (
    ApiKeyExemptAnonRateThrottle,
    ApiKeyExemptUserRateThrottle,
    is_api_key_authenticated,
)
from job_hunting.models import ApiKey

User = get_user_model()

# Mirror REST_FRAMEWORK["DEFAULT_AUTHENTICATION_CLASSES"] order: ApiKey first,
# then JWT. TokenAuthentication is irrelevant to these Bearer flows.
AUTHENTICATORS = [ApiKeyAuthentication(), JWTAuthentication()]


class ThrottleExemptBase(TestCase):
    def setUp(self):
        cache.clear()
        self.factory = APIRequestFactory()
        self.user = User.objects.create_user(username="svc", password="pass")

    def tearDown(self):
        cache.clear()

    def _request(self, auth_header=None):
        kwargs = {"HTTP_AUTHORIZATION": auth_header} if auth_header else {}
        django_request = self.factory.get("/api/v1/job-posts/", **kwargs)
        request = Request(django_request, authenticators=AUTHENTICATORS)
        # Force lazy authentication so `successful_authenticator` is populated,
        # exactly as APIView.initial() does before check_throttles().
        _ = request.user
        return request

    def _api_key_request(self):
        _, raw_key = ApiKey.generate_key(name="svc", user_id=self.user.id)
        return self._request(f"Bearer {raw_key}")

    def _jwt_request(self):
        token = str(AccessToken.for_user(self.user))
        return self._request(f"Bearer {token}")

    def _anon_request(self):
        return self._request()


class TestApiKeyRecognition(ThrottleExemptBase):
    def test_api_key_request_is_recognized(self):
        request = self._api_key_request()
        self.assertIsInstance(
            request.successful_authenticator, ApiKeyAuthentication
        )
        self.assertTrue(is_api_key_authenticated(request))

    def test_jwt_request_is_not_treated_as_api_key(self):
        request = self._jwt_request()
        self.assertIsInstance(request.successful_authenticator, JWTAuthentication)
        self.assertFalse(is_api_key_authenticated(request))

    def test_anon_request_is_not_treated_as_api_key(self):
        request = self._anon_request()
        self.assertIsNone(request.successful_authenticator)
        self.assertFalse(is_api_key_authenticated(request))


class TestApiKeyExemptFromThrottle(ThrottleExemptBase):
    def test_user_throttle_returns_no_cache_key_for_api_key(self):
        throttle = ApiKeyExemptUserRateThrottle()
        self.assertIsNone(throttle.get_cache_key(self._api_key_request(), view=None))

    def test_anon_throttle_returns_no_cache_key_for_api_key(self):
        throttle = ApiKeyExemptAnonRateThrottle()
        self.assertIsNone(throttle.get_cache_key(self._api_key_request(), view=None))

    def test_api_key_not_throttled_even_past_the_rate(self):
        request = self._api_key_request()
        throttle = ApiKeyExemptUserRateThrottle()
        # Clamp to a rate of 1 so a stock user would be blocked immediately.
        throttle.num_requests = 1
        throttle.duration = 3600
        # Every call stays allowed — the exemption short-circuits before the
        # request history is ever consulted.
        for _ in range(5):
            self.assertTrue(throttle.allow_request(request, view=None))


class TestThrottledTrafficUnchanged(ThrottleExemptBase):
    def _assert_blocked_after_one(self, throttle, request):
        throttle.num_requests = 1
        throttle.duration = 3600
        self.assertIsNotNone(throttle.get_cache_key(request, view=None))
        self.assertTrue(throttle.allow_request(request, view=None))
        self.assertFalse(throttle.allow_request(request, view=None))

    def test_jwt_request_still_throttled(self):
        self._assert_blocked_after_one(
            ApiKeyExemptUserRateThrottle(), self._jwt_request()
        )

    def test_anonymous_request_still_throttled(self):
        self._assert_blocked_after_one(
            ApiKeyExemptAnonRateThrottle(), self._anon_request()
        )
