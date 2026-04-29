import os

from django.conf import settings
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.parsers import JSONParser, MultiPartParser
from drf_spectacular.utils import (
    extend_schema,
    OpenApiResponse,
)
from job_hunting.api.permissions import IsGuestReadOnly
from ..parsers import VndApiJSONParser
from ..serializers import (
    ExperienceSerializer,
    ProjectSerializer,
    TYPE_TO_SERIALIZER,
    _resource_base_path,
)
from ._schema import (
    _INCLUDE_PARAM,
    _PAGE_PARAMS,
    _JSONAPI_LIST,
    _JSONAPI_ITEM,
    _JSONAPI_WRITE,
)


class BaseViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated, IsGuestReadOnly]
    parser_classes = [MultiPartParser, VndApiJSONParser, JSONParser]
    model = None
    serializer_class = None

    def get_permissions(self):
        # Always allow OPTIONS for CORS preflight and API metadata
        if getattr(self.request, "method", "").upper() == "OPTIONS":
            return [AllowAny()]
        return super().get_permissions()

    def options(self, request, *args, **kwargs):
        # Explicitly handle CORS preflight to avoid auth and ensure proper headers
        allow_methods = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        origin = request.META.get("HTTP_ORIGIN")
        requested_headers = request.META.get("HTTP_ACCESS_CONTROL_REQUEST_HEADERS")

        resp = Response(status=200)
        resp["Allow"] = allow_methods
        resp["Access-Control-Allow-Methods"] = allow_methods
        if origin:
            resp["Access-Control-Allow-Origin"] = origin
            resp["Vary"] = "Origin"
            # If frontend sends credentials (cookies/Authorization), allow them
            resp["Access-Control-Allow-Credentials"] = "true"
        else:
            resp["Access-Control-Allow-Origin"] = "*"

        if requested_headers:
            resp["Access-Control-Allow-Headers"] = requested_headers
        else:
            resp["Access-Control-Allow-Headers"] = "Authorization, Content-Type"

        resp["Access-Control-Max-Age"] = "600"
        return resp

    def get_throttles(self):
        # Disable DRF throttling in development-like environments
        try:
            debug = bool(getattr(settings, "DEBUG", False))
        except Exception:
            debug = False
        env_val = os.environ.get("ENV", "").lower()
        disable_flag = str(os.environ.get("DISABLE_THROTTLE", "")).strip().lower() in (
            "1",
            "true",
            "yes",
        )
        settings_disable = bool(getattr(settings, "DISABLE_THROTTLE", False))
        if (
            debug
            or env_val in ("dev", "development", "local")
            or disable_flag
            or settings_disable
        ):
            return []
        return super().get_throttles()

    def _get_obj(self, pk):
        """Fetch a single object by PK."""
        return self.model.objects.filter(pk=int(pk)).first()

    def get_serializer(self, *args, slim=False, request=None, **kwargs):
        ser = self.serializer_class()
        ser.slim = slim
        # Propagate request so the serializer can honor JSON:API
        # fields[<type>] sparse-fieldsets in to_resource(). DRF ViewSets
        # always have self.request set on dispatch; included serializers
        # already get this via _build_included.
        ser.request = request if request is not None else getattr(self, "request", None)
        return ser

    def _is_slim_request(self, request) -> bool:
        return bool(request.query_params.get("slim"))

    def pre_save_payload(self, request, attrs: dict, creating: bool) -> dict:
        """Hook for subclasses to adjust/force attributes before persistence."""
        return attrs

    def _parse_include(self, request):
        raw = []
        inc = request.query_params.get("include")
        incs = request.query_params.get("includes")
        if inc:
            raw.append(str(inc))
        if incs and incs != inc:
            raw.append(str(incs))
        if not raw:
            return []
        parts = []
        for chunk in raw:
            parts.extend([s.strip() for s in chunk.split(",") if s and s.strip()])
        # de-duplicate while preserving order
        seen = set()
        out = []
        for p in parts:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return out

    def _normalize_rel_for_serializer(self, name: str, serializer) -> str:
        """
        Normalize relationship names to match serializer relationship keys.
        Frontend convention: dasherized singular model names (e.g., 'job-application')
        Serializer convention: dasherized plural relationship keys (e.g., 'job-applications')
        """
        rels = getattr(serializer, "relationships", {}) or {}
        rel_keys = set(rels.keys())

        # Direct match - return immediately if found
        if name in rel_keys:
            return name

        # Convert to dasherized form if not already
        dasherized = name.replace("_", "-")
        if dasherized in rel_keys:
            return dasherized

        # Static mapping from frontend model names (singular) to backend relationship keys
        MODEL_TO_RELATIONSHIP = {
            # Plural relationships (to-many)
            "answer": "answers",
            "api-key": "api-keys",
            "certification": "certifications",
            "cover-letter": "cover-letters",
            "description": "descriptions",
            "education": "educations",
            "experience": "experiences",
            "job-application": "applications",
            "job-post": "job-posts",
            "question": "questions",
            "resume": "resumes",
            "score": "scores",
            "scrape": "scrapes",
            "skill": "skills",
            "status": "statuses",
            "summary": "summaries",
            "user": "users",
            # Singular relationships (to-one) - map to themselves
            "company": "company",
            "career-data": "career-data",
        }

        # Look up in mapping - this is the primary normalization path
        mapped = MODEL_TO_RELATIONSHIP.get(dasherized)
        if mapped and mapped in rel_keys:
            return mapped

        # Fallback: check if the relationship type matches the requested name
        for rel_key, cfg in rels.items():
            rel_type = (cfg or {}).get("type")
            if rel_type == name or rel_type == dasherized:
                return rel_key

        # No match found - return the mapped value if we have one, otherwise original
        if mapped:
            return mapped
        return name

    def _build_included(
        self, objs, include_rels, request=None, primary_serializer=None
    ):
        included = []
        seen = set()  # (type, id)
        primary_ser = primary_serializer or self.get_serializer()

        def _include_recursive(
            objects,
            path_segments,
            current_serializer,
            parent_type=None,
            parent_id=None,
            parent_rel=None,
        ):
            if not path_segments or not objects:
                return

            segment = path_segments[0]
            remaining_segments = path_segments[1:]

            for obj in objects:
                normalized_rel = self._normalize_rel_for_serializer(
                    segment, current_serializer
                )

                # Get relationship config first
                cfg = getattr(current_serializer, "relationships", {}).get(
                    normalized_rel
                )

                rel_type, targets = current_serializer.get_related(obj, normalized_rel)
                effective_type = rel_type or (cfg and cfg.get("type"))

                # Only continue if we have no effective type and no config
                if not effective_type and not cfg:
                    continue

                # FK fallback for to-one relationships when targets is empty
                if not targets and cfg and not cfg.get("uselist", True):
                    fk_field = getattr(current_serializer, "relationship_fks", {}).get(
                        normalized_rel
                    )
                    if fk_field:
                        rel_id = getattr(obj, fk_field, None)
                        if rel_id is not None:
                            effective_type = effective_type or cfg.get("type")
                            ser_cls = TYPE_TO_SERIALIZER.get(effective_type)
                            if ser_cls:
                                rel_ser = ser_cls()
                                model_cls = rel_ser.model
                                try:
                                    fetched = model_cls.objects.filter(
                                        pk=int(rel_id)
                                    ).first()
                                    if fetched:
                                        targets = [fetched]
                                except (TypeError, ValueError, AttributeError):
                                    pass

                # Recompute effective_type if still None and cfg exists
                effective_type = effective_type or (cfg and cfg.get("type"))

                # Only proceed if we have an effective type and targets
                if not effective_type or not targets:
                    continue

                ser_cls = TYPE_TO_SERIALIZER.get(effective_type)
                if not ser_cls:
                    continue

                rel_ser = ser_cls()
                rel_ser.request = request
                # Provide parent context so serializers can customize included resources
                if hasattr(rel_ser, "set_parent_context"):
                    rel_ser.set_parent_context(
                        current_serializer.type, obj.id, normalized_rel
                    )

                for t in targets:
                    key = (effective_type, str(t.id))
                    already_seen = key in seen

                    if not already_seen:
                        # Filter user-owned resources to only include those owned by authenticated user.
                        # Skip the check when user_id is None (resource reached via relationship on
                        # an already-authorized parent, e.g. a summary linked to the user's resume).
                        t_user_id = getattr(t, "user_id", None)
                        if (
                            effective_type in ("cover-letter", "score", "summary", "job-application", "resume", "project")
                            and t_user_id is not None
                            and request
                            and hasattr(request, "user")
                            and request.user.is_authenticated
                            and t_user_id != request.user.id
                        ):
                            continue

                        seen.add(key)
                        included.append(rel_ser.to_resource(t))

                    # If there are more segments, always recurse (even if this node was already seen)
                    if remaining_segments:
                        _include_recursive([t], remaining_segments, rel_ser)

                    # Auto-include children of experience (descriptions and company) when path ends at experience
                    if effective_type == "experience" and not remaining_segments:
                        exp_child_ser = ExperienceSerializer()
                        if hasattr(exp_child_ser, "set_parent_context"):
                            exp_child_ser.set_parent_context("experience", t.id, None)
                        for child_rel in ("descriptions", "company"):
                            _include_recursive([t], [child_rel], exp_child_ser)

                    # Auto-include descriptions for projects
                    if effective_type == "project" and not remaining_segments:
                        proj_child_ser = ProjectSerializer()
                        _include_recursive([t], ["descriptions"], proj_child_ser)

        # Process each include path
        for include_path in include_rels:
            path_segments = include_path.split(".")
            _include_recursive(objs, path_segments, primary_ser)

        return included

    def _page_params(self):
        """Return (page_number, page_size) parsed from request, supporting both
        page[number]/page[size] (JSON:API) and page/per_page (simple) styles."""
        qp = self.request.query_params
        try:
            page_number = int(qp.get("page[number]") or qp.get("page") or 1)
        except Exception:
            page_number = 1
        try:
            page_size = int(qp.get("page[size]") or qp.get("per_page") or 50)
        except Exception:
            page_size = 50
        page_number = max(1, page_number)
        page_size = max(1, min(page_size, 200))
        return page_number, page_size

    def paginate(self, items):
        page_number, page_size = self._page_params()
        start = (page_number - 1) * page_size
        end = start + page_size
        return items[start:end]

    @extend_schema(
        tags=["API"],
        summary="List resources",
        parameters=_PAGE_PARAMS,
        responses={200: _JSONAPI_LIST},
    )
    def list(self, request):
        items = list(self.model.objects.all())
        items = self.paginate(items)
        ser = self.get_serializer()
        data = [ser.to_resource(o) for o in items]
        payload = {"data": data}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included(items, include_rels, request)
        return Response(payload)

    @extend_schema(
        tags=["API"],
        summary="Retrieve a resource",
        parameters=[_INCLUDE_PARAM],
        responses={200: _JSONAPI_ITEM, 404: OpenApiResponse(description="Not found")},
    )
    def retrieve(self, request, pk=None):
        obj = self._get_obj(pk)
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = self.get_serializer()
        payload = {"data": ser.to_resource(obj)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([obj], include_rels, request)
        return Response(payload)

    @extend_schema(
        tags=["API"],
        summary="Create a resource",
        request=_JSONAPI_WRITE,
        responses={
            201: _JSONAPI_ITEM,
            400: OpenApiResponse(description="Validation error"),
        },
    )
    def create(self, request):
        # Handle both JSON:API and plain JSON payloads
        data = request.data if isinstance(request.data, dict) else {}
        ser = self.get_serializer()

        # Check if this is JSON:API format (has "data" wrapper)
        if "data" in data:
            # Use JSON:API parser
            try:
                attrs = ser.parse_payload(request.data)
            except ValueError as e:
                return Response({"errors": [{"detail": str(e)}]}, status=400)
        else:
            # Handle plain JSON format
            attrs = {}
            for key in [
                "username",
                "email",
                "first_name",
                "last_name",
                "password",
                "phone",
            ]:
                if key in data:
                    attrs[key] = data[key]

        attrs = self.pre_save_payload(request, attrs, creating=True)
        # Respect model schema: only set created_by_id if the model supports it
        if hasattr(self.model, "created_by_id"):
            # Ignore any client-supplied created_by_id; set from authenticated user if available
            attrs.pop("created_by_id", None)
            if getattr(request, "user", None) and getattr(
                request.user, "is_authenticated", False
            ):
                attrs["created_by_id"] = request.user.id
        else:
            # Ensure unsupported ownership fields are not passed to the model
            attrs.pop("created_by_id", None)
            attrs.pop("created_by", None)
        obj = self.model.objects.create(**attrs)
        return Response({"data": ser.to_resource(obj)}, status=status.HTTP_201_CREATED)

    @extend_schema(
        tags=["API"],
        summary="Replace a resource (full update)",
        request=_JSONAPI_WRITE,
        responses={
            200: _JSONAPI_ITEM,
            400: OpenApiResponse(description="Validation error"),
            404: OpenApiResponse(description="Not found"),
        },
    )
    def update(self, request, pk=None):
        return self._upsert(request, pk, partial=False)

    @extend_schema(
        tags=["API"],
        summary="Partially update a resource",
        request=_JSONAPI_WRITE,
        responses={
            200: _JSONAPI_ITEM,
            400: OpenApiResponse(description="Validation error"),
            404: OpenApiResponse(description="Not found"),
        },
    )
    def partial_update(self, request, pk=None):
        return self._upsert(request, pk, partial=True)

    def _upsert(self, request, pk, partial=False):
        obj = self._get_obj(pk)
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = self.get_serializer()
        try:
            attrs = ser.parse_payload(request.data)
        except ValueError as e:
            return Response({"errors": [{"detail": str(e)}]}, status=400)
        attrs = self.pre_save_payload(request, attrs, creating=False)
        # Do not allow changing ownership
        attrs.pop("created_by_id", None)
        for k, v in attrs.items():
            setattr(obj, k, v)
        obj.save()
        return Response({"data": ser.to_resource(obj)})

    @extend_schema(
        tags=["API"],
        summary="Delete a resource",
        responses={204: OpenApiResponse(description="Deleted")},
    )
    def destroy(self, request, pk=None):
        self.model.objects.filter(pk=int(pk)).delete()
        return Response(status=204)

    # JSON:API relationships linkage endpoint:
    # GET /<type>/{id}/relationships/<rel-name>
    @extend_schema(
        tags=["API"],
        summary="Get JSON:API relationship linkage data",
        responses={
            200: OpenApiResponse(description="Relationship linkage"),
            404: OpenApiResponse(description="Not found or relationship not found"),
        },
    )
    @action(detail=True, methods=["get"], url_path=r"relationships/(?P<rel>[^/]+)")
    def relationships(self, request, pk=None, rel=None):
        obj = self._get_obj(pk)
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = self.get_serializer()

        # Normalize the relationship name
        normalized_rel = self._normalize_rel_for_serializer(rel, ser)

        cfg = (
            ser.relationships.get(normalized_rel)
            if ser and hasattr(ser, "relationships")
            else None
        )
        if not cfg:
            return Response(
                {"errors": [{"detail": "Relationship not found"}]}, status=404
            )

        rel_type = cfg["type"]
        uselist = cfg.get("uselist", True)
        target = getattr(obj, cfg["attr"], None)

        if uselist:
            if hasattr(target, "all"):
                target = target.all()
            data = [{"type": rel_type, "id": str(i.id)} for i in (target or [])]
            links = {
                "self": f"{_resource_base_path(ser.type)}/{obj.id}/relationships/{rel}",
            }
        else:
            # Try to get target_id from object or FK fallback
            target_id = None
            if target is not None and getattr(target, "id", None) is not None:
                target_id = target.id
            else:
                # FK fallback: check if we have a foreign key field for this relationship
                fk_field = getattr(ser, "relationship_fks", {}).get(normalized_rel)
                if fk_field:
                    fk_value = getattr(obj, fk_field, None)
                    if fk_value is not None:
                        target_id = fk_value

            data = (
                {"type": rel_type, "id": str(target_id)}
                if target_id is not None
                else None
            )
            links = {
                "self": f"{_resource_base_path(ser.type)}/{obj.id}/relationships/{rel}",
            }
            # Include related link if we have a target_id
            if target_id is not None:
                links["related"] = f"{_resource_base_path(rel_type)}/{target_id}"

        return Response({"data": data, "links": links})
