from rest_framework import viewsets
from .models import Task
from .serializers import TaskSerializer

class TaskViewSet(viewsets.ModelViewSet):
    """
    A simple ViewSet for viewing and editing tasks.
    """
    queryset = Task.objects.all()
    serializer_class = TaskSerializer
