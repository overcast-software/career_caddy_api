from rest_framework.permissions import BasePermission, SAFE_METHODS


class IsGuestReadOnly(BasePermission):
    """Allow all methods for regular users; restrict guests to safe (read) methods only."""

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        try:
            return not request.user.profile_obj.is_guest
        except Exception:
            return True  # no profile → not a guest; let IsAuthenticated handle it
