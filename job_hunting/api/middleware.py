from django.http import JsonResponse
from django.contrib.auth import get_user_model
from job_hunting.lib.models.api_key import ApiKey


class ApiKeyAuthenticationMiddleware:
    """
    Middleware to authenticate requests using API keys.
    
    Looks for API key in:
    1. Authorization header: "Bearer jh_..."
    2. X-API-Key header: "jh_..."
    3. Query parameter: "api_key=jh_..."
    """
    
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Skip API key auth for certain paths
        skip_paths = [
            '/api/v1/initialize/',
            '/api/v1/healthcheck/',
            '/admin/',
            '/static/',
            '/media/',
        ]
        
        if any(request.path.startswith(path) for path in skip_paths):
            return self.get_response(request)
        
        # Try to extract API key
        api_key = self._extract_api_key(request)
        
        if api_key:
            # Authenticate with API key
            api_key_obj = ApiKey.authenticate(api_key)
            if api_key_obj:
                # Set the user on the request
                User = get_user_model()
                try:
                    user = User.objects.get(id=api_key_obj.user_id)
                    request.user = user
                    request.api_key = api_key_obj
                except User.DoesNotExist:
                    pass
        
        return self.get_response(request)
    
    def _extract_api_key(self, request):
        """Extract API key from various sources"""
        # 1. Authorization header
        auth_header = request.META.get('HTTP_AUTHORIZATION', '')
        if auth_header.startswith('Bearer '):
            token = auth_header[7:]  # Remove 'Bearer ' prefix
            if token.startswith('jh_'):
                return token
        
        # 2. X-API-Key header
        api_key_header = request.META.get('HTTP_X_API_KEY', '')
        if api_key_header.startswith('jh_'):
            return api_key_header
        
        # 3. Query parameter
        api_key_param = request.GET.get('api_key', '')
        if api_key_param.startswith('jh_'):
            return api_key_param
        
        return None


class ApiKeyPermissionMiddleware:
    """
    Middleware to check API key scopes/permissions.
    """
    
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Only check permissions if authenticated via API key
        if hasattr(request, 'api_key') and request.api_key:
            # Check if the API key has required scopes for this endpoint
            if not self._check_permissions(request):
                return JsonResponse(
                    {"errors": [{"detail": "Insufficient API key permissions"}]},
                    status=403
                )
        
        return self.get_response(request)
    
    def _check_permissions(self, request):
        """Check if API key has required permissions for the request"""
        api_key = request.api_key
        
        # Define scope requirements for different endpoints
        scope_map = {
            'GET': ['read', '*'],
            'POST': ['write', '*'],
            'PUT': ['write', '*'],
            'PATCH': ['write', '*'],
            'DELETE': ['write', '*'],
        }
        
        required_scopes = scope_map.get(request.method, [])
        
        # Check if API key has any of the required scopes
        for scope in required_scopes:
            if api_key.has_scope(scope):
                return True
        
        return False
