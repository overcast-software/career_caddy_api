from rest_framework import viewsets
from django.http import JsonResponse
from .models import Task
from .serializers import TaskSerializer

def ping(request):
    return JsonResponse({'response': 'PONG'})

class TaskViewSet(viewsets.ModelViewSet):
    """
    A simple ViewSet for viewing and editing tasks.
    """
    queryset = Task.objects.all()
    serializer_class = TaskSerializer
