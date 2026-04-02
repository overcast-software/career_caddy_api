class GetMixin:
    """Mixin that adds a .get(pk) classmethod to Django models."""

    @classmethod
    def get(cls, pk):
        return cls.objects.filter(pk=pk).first()
