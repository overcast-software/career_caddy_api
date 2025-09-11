from rest_framework.renderers import JSONRenderer

class VndApiJSONRenderer(JSONRenderer):
    media_type = 'application/vnd.api+json'
    format = 'vnd.api+json'
    charset = None  # JSON:API forbids charset parameter
