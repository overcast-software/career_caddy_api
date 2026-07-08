"""DRF throttle classes that exempt trusted service API keys.

The operator's long-lived ``jh_`` API key (``Authorization: Bearer <jh_...>``)
authenticates as a single Django user via
:class:`job_hunting.api.authentication.ApiKeyAuthentication`. Every
service-side caller — the scrape runner, score poller, pydantic-ai agents,
the automation cron, the browser extension — shares that one credential, so
the stock ``UserRateThrottle`` collapses all of them into a single per-user
bucket and locks the operator out with a 429 once the daily cap is hit.

These subclasses short-circuit throttling for requests authenticated by
``ApiKeyAuthentication`` while leaving anonymous and JWT (frontend) traffic
throttled exactly as before. Returning ``None`` from ``get_cache_key`` is the
idiomatic DRF exemption (``SimpleRateThrottle.allow_request`` treats a ``None``
key as "not throttled") — the same mechanism ``AnonRateThrottle`` uses to skip
authenticated users.

Wired into ``REST_FRAMEWORK["DEFAULT_THROTTLE_CLASSES"]`` so the exemption
applies to every route globally, not only the ViewSets that inherit
``BaseViewSet`` (``DjangoUserViewSet`` and ``StatusViewSet`` do not).
"""

from rest_framework.throttling import AnonRateThrottle, UserRateThrottle

from job_hunting.api.authentication import ApiKeyAuthentication


def is_api_key_authenticated(request) -> bool:
    """True when the request was authenticated by ``ApiKeyAuthentication``.

    At throttle-check time DRF has already run authentication, so
    ``request.successful_authenticator`` is populated (or ``None`` for anon).
    """
    return isinstance(
        getattr(request, "successful_authenticator", None), ApiKeyAuthentication
    )


class ApiKeyExemptAnonRateThrottle(AnonRateThrottle):
    """AnonRateThrottle that never throttles trusted ``jh_`` API keys."""

    def get_cache_key(self, request, view):
        if is_api_key_authenticated(request):
            return None
        return super().get_cache_key(request, view)


class ApiKeyExemptUserRateThrottle(UserRateThrottle):
    """UserRateThrottle that never throttles trusted ``jh_`` API keys."""

    def get_cache_key(self, request, view):
        if is_api_key_authenticated(request):
            return None
        return super().get_cache_key(request, view)
