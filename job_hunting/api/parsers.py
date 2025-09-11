from rest_framework.parsers import JSONParser

class VndApiJSONParser(JSONParser):
    media_type = 'application/vnd.api+json'
