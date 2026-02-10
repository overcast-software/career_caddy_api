from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from django.contrib.auth import get_user_model
from job_hunting.lib.models.api_key import ApiKey


class ApiKeyAuthentication(BaseAuthentication):
    """
    Custom authentication class for API keys in Django REST Framework.
    """
    
    def authenticate(self, request):
        """
        Authenticate the request using API key.
        Returns a tuple of (user, api_key) if successful, None otherwise.
        """
        api_key = self._extract_api_key(request)
        
        if not api_key:
            return None
            
        # Authenticate with API key
        api_key_obj = ApiKey.authenticate(api_key)
        if not api_key_obj:
            raise AuthenticationFailed('Invalid API key')
            
        # Get the user
        User = get_user_model()
        try:
            user = User.objects.get(id=api_key_obj.user_id)
        except User.DoesNotExist:
            raise AuthenticationFailed('User associated with API key not found')
            
        return (user, api_key_obj)
    
    def _extract_api_key(self, request):
        """Extract API key from various sources"""
        # 1. Authorization header
        auth_header = request.META.get("HTTP_AUTHORIZATION", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]  # Remove 'Bearer ' prefix
            if token.startswith("jh_"):
                return token

        # 2. X-API-Key header
        api_key_header = request.META.get("HTTP_X_API_KEY", "")
        if api_key_header.startswith("jh_"):
            return api_key_header

        # 3. Query parameter
        api_key_param = request.GET.get("api_key", "")
        if api_key_param.startswith("jh_"):
            return api_key_param

        return None
