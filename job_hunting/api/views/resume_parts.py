from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response
from drf_spectacular.utils import (
    extend_schema,
    extend_schema_view,
    OpenApiResponse,
)

from .base import BaseViewSet
from ._schema import (
    _JSONAPI_LIST,
    _JSONAPI_ITEM,
    _JSONAPI_WRITE,
)
from ..serializers import (
    ExperienceSerializer,
    EducationSerializer,
    CertificationSerializer,
    DescriptionSerializer,
    ProjectSerializer,
)
from job_hunting.models import (
    Description,
    Certification,
    Education,
    Experience,
    Resume,
    ExperienceDescription,
    ResumeEducation,
    ResumeCertification,
    ResumeExperience,
    Project,
    ProjectDescription,
)


@extend_schema_view(
    list=extend_schema(tags=["Experiences"], summary="List experiences"),
    retrieve=extend_schema(tags=["Experiences"], summary="Retrieve an experience"),
    update=extend_schema(
        tags=["Experiences"],
        summary="Update an experience (also adds resume join links if relationships.resumes provided)",
    ),
    partial_update=extend_schema(
        tags=["Experiences"],
        summary="Partially update an experience (also adds resume join links if relationships.resumes provided)",
    ),
    destroy=extend_schema(tags=["Experiences"], summary="Delete an experience"),
)
class ExperienceViewSet(BaseViewSet):
    model = Experience
    serializer_class = ExperienceSerializer

    def _owned_qs(self, request):
        if request.user.is_staff:
            return Experience.objects.all()
        return Experience.objects.filter(
            resumeexperience__resume__user_id=request.user.id
        ).distinct()

    def list(self, request):
        items = list(self._owned_qs(request))
        items = self.paginate(items)
        ser = self.get_serializer()
        data = [ser.to_resource(o) for o in items]
        payload = {"data": data}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included(items, include_rels, request)
        return Response(payload)

    def retrieve(self, request, pk=None):
        obj = self._owned_qs(request).filter(pk=int(pk)).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = self.get_serializer()
        payload = {"data": ser.to_resource(obj)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([obj], include_rels, request)
        return Response(payload)

    def destroy(self, request, pk=None):
        if not self._owned_qs(request).filter(pk=int(pk)).exists():
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        Experience.objects.filter(pk=int(pk)).delete()
        return Response(status=204)

    @action(detail=True, methods=["post"], url_path="reorder-descriptions")
    def reorder_descriptions(self, request, pk=None):
        """
        Reorder descriptions under this experience in one transaction.
        Body: {"description_ids": [3, 1, 2]}.
        """
        exp = self._owned_qs(request).filter(pk=int(pk)).first()
        if not exp:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        raw = request.data.get("description_ids")
        if not isinstance(raw, list):
            return Response(
                {"errors": [{"detail": "description_ids must be a list of ints"}]},
                status=400,
            )
        try:
            ids = [int(i) for i in raw]
        except (TypeError, ValueError):
            return Response(
                {"errors": [{"detail": "description_ids must be a list of ints"}]},
                status=400,
            )

        rows = {
            ed.description_id: ed
            for ed in ExperienceDescription.objects.filter(experience_id=exp.id)
        }
        if set(ids) != set(rows.keys()):
            return Response(
                {"errors": [{"detail": "description_ids must match this experience's descriptions exactly"}]},
                status=400,
            )

        for i, did in enumerate(ids):
            row = rows[did]
            if row.order != i:
                row.order = i
                row.save(update_fields=["order"])

        return Response({"data": {"description_ids": ids}}, status=200)

    @extend_schema(
        tags=["Experiences"],
        summary="List descriptions for an experience",
        responses={200: _JSONAPI_LIST},
    )
    @action(detail=True, methods=["get"])
    def descriptions(self, request, pk=None):
        obj = self._owned_qs(request).filter(pk=int(pk)).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = DescriptionSerializer()
        ser.set_parent_context("experience", obj.id, "descriptions")
        _desc_ids = list(
            ExperienceDescription.objects.filter(experience_id=obj.id)
            .order_by("order")
            .values_list("description_id", flat=True)
        )
        _desc_map = {d.id: d for d in Description.objects.filter(pk__in=_desc_ids)}
        items = [_desc_map[did] for did in _desc_ids if did in _desc_map]
        data = [ser.to_resource(d) for d in items]
        return Response({"data": data})

    @extend_schema(
        tags=["Experiences"],
        summary="Create an experience (optionally link to one or more resumes via relationships.resumes)",
        request=_JSONAPI_WRITE,
        responses={
            201: _JSONAPI_ITEM,
            400: OpenApiResponse(description="Invalid resume IDs"),
        },
    )
    def create(self, request):
        ser = self.get_serializer()
        try:
            attrs = ser.parse_payload(request.data)
        except ValueError as e:
            return Response(
                {"errors": [{"detail": str(e)}]}, status=status.HTTP_400_BAD_REQUEST
            )

        data = request.data if isinstance(request.data, dict) else {}
        node = data.get("data") or {}
        relationships = node.get("relationships") or {}

        # Accept both "resumes" (list) and "resume" (single)
        res_rel = relationships.get("resumes") or relationships.get("resume") or {}
        resume_ids = []
        if isinstance(res_rel, dict):
            d = res_rel.get("data")
            items = d if isinstance(d, list) else ([d] if isinstance(d, dict) else [])
            for it in items:
                if not isinstance(it, dict):
                    continue
                rid = it.get("id")
                if rid is None:
                    continue
                try:
                    resume_ids.append(int(rid))
                except (TypeError, ValueError):
                    return Response(
                        {"errors": [{"detail": f"Invalid resume id: {rid}"}]},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

        # Validate referenced resumes (if provided)
        invalid = [
            rid for rid in resume_ids if not Resume.objects.filter(pk=rid).exists()
        ]
        if invalid:
            return Response(
                {
                    "errors": [
                        {
                            "detail": f"Invalid resume ID(s): {', '.join(map(str, invalid))}"
                        }
                    ]
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Use existing Experience if client supplies an id; otherwise create a new one
        provided_id = node.get("id")
        exp = None
        if provided_id is not None:
            try:
                exp = Experience.objects.filter(pk=int(provided_id)).first()
            except (TypeError, ValueError):
                exp = None
            if not exp:
                return Response(
                    {"errors": [{"detail": f"Invalid experience ID: {provided_id}"}]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            exp = Experience.objects.create(**attrs)

        # Populate join table (avoid duplicates)
        for rid in resume_ids:
            ResumeExperience.objects.get_or_create(resume_id=rid, experience_id=exp.id)

        payload = {"data": ser.to_resource(exp)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([exp], include_rels, request)
        return Response(payload, status=status.HTTP_201_CREATED)

    def _upsert(self, request, pk, partial=False):
        exp = self._owned_qs(request).filter(pk=int(pk)).first()
        if not exp:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        ser = self.get_serializer()
        try:
            attrs = ser.parse_payload(request.data)
        except ValueError as e:
            return Response(
                {"errors": [{"detail": str(e)}]}, status=status.HTTP_400_BAD_REQUEST
            )

        # Update scalar attributes (including company via relationship_fks)
        for k, v in attrs.items():
            setattr(exp, k, v)
        exp.save()

        # Parse resume relationship to add join(s)
        data = request.data if isinstance(request.data, dict) else {}
        node = data.get("data") or {}
        relationships = node.get("relationships") or {}

        res_rel = relationships.get("resumes") or relationships.get("resume") or {}
        resume_ids = []
        if isinstance(res_rel, dict):
            d = res_rel.get("data")
            items = d if isinstance(d, list) else ([d] if isinstance(d, dict) else [])
            for it in items:
                if not isinstance(it, dict):
                    continue
                rid = it.get("id")
                if rid is None:
                    continue
                try:
                    resume_ids.append(int(rid))
                except (TypeError, ValueError):
                    return Response(
                        {"errors": [{"detail": f"Invalid resume id: {rid}"}]},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

        # Validate resumes and create missing links
        invalid = [
            rid for rid in resume_ids if not Resume.objects.filter(pk=rid).exists()
        ]
        if invalid:
            return Response(
                {
                    "errors": [
                        {
                            "detail": f"Invalid resume ID(s): {', '.join(map(str, invalid))}"
                        }
                    ]
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        for rid in resume_ids:
            ResumeExperience.objects.get_or_create(resume_id=rid, experience_id=exp.id)

        payload = {"data": ser.to_resource(exp)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([exp], include_rels, request)
        return Response(payload)


@extend_schema_view(
    list=extend_schema(tags=["Educations"], summary="List educations"),
    retrieve=extend_schema(tags=["Educations"], summary="Retrieve an education"),
    update=extend_schema(tags=["Educations"], summary="Update an education"),
    partial_update=extend_schema(
        tags=["Educations"], summary="Partially update an education"
    ),
    destroy=extend_schema(tags=["Educations"], summary="Delete an education"),
)
class EducationViewSet(BaseViewSet):
    model = Education
    serializer_class = EducationSerializer

    def _owned_qs(self, request):
        if request.user.is_staff:
            return Education.objects.all()
        return Education.objects.filter(
            resumeeducation__resume__user_id=request.user.id
        ).distinct()

    def list(self, request):
        items = list(self._owned_qs(request))
        items = self.paginate(items)
        ser = self.get_serializer()
        data = [ser.to_resource(o) for o in items]
        payload = {"data": data}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included(items, include_rels, request)
        return Response(payload)

    def retrieve(self, request, pk=None):
        obj = self._owned_qs(request).filter(pk=int(pk)).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = self.get_serializer()
        payload = {"data": ser.to_resource(obj)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([obj], include_rels, request)
        return Response(payload)

    def destroy(self, request, pk=None):
        if not self._owned_qs(request).filter(pk=int(pk)).exists():
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        Education.objects.filter(pk=int(pk)).delete()
        return Response(status=204)

    @extend_schema(
        tags=["Educations"],
        summary="Create an education (optionally link to resumes via relationships.resumes)",
        request=_JSONAPI_WRITE,
        responses={
            201: _JSONAPI_ITEM,
            400: OpenApiResponse(description="Invalid resume IDs"),
        },
    )
    def create(self, request):
        ser = self.get_serializer()
        try:
            attrs = ser.parse_payload(request.data)
        except ValueError as e:
            return Response(
                {"errors": [{"detail": str(e)}]}, status=status.HTTP_400_BAD_REQUEST
            )

        data = request.data if isinstance(request.data, dict) else {}
        node = data.get("data") or {}
        relationships = node.get("relationships") or {}

        res_rel = relationships.get("resumes") or relationships.get("resume") or {}
        resume_ids = []
        if isinstance(res_rel, dict):
            d = res_rel.get("data")
            items = d if isinstance(d, list) else ([d] if isinstance(d, dict) else [])
            for it in items:
                if not isinstance(it, dict):
                    continue
                rid = it.get("id")
                if rid is None:
                    continue
                try:
                    resume_ids.append(int(rid))
                except (TypeError, ValueError):
                    return Response(
                        {"errors": [{"detail": f"Invalid resume id: {rid}"}]},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

        invalid = [
            rid for rid in resume_ids if not Resume.objects.filter(pk=rid).exists()
        ]
        if invalid:
            return Response(
                {
                    "errors": [
                        {
                            "detail": f"Invalid resume ID(s): {', '.join(map(str, invalid))}"
                        }
                    ]
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        edu = Education.objects.create(**attrs)

        for rid in resume_ids:
            ResumeEducation.objects.get_or_create(resume_id=rid, education_id=edu.id)

        payload = {"data": ser.to_resource(edu)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([edu], include_rels, request)
        return Response(payload, status=status.HTTP_201_CREATED)


@extend_schema_view(
    list=extend_schema(tags=["Certifications"], summary="List certifications"),
    retrieve=extend_schema(tags=["Certifications"], summary="Retrieve a certification"),
    update=extend_schema(tags=["Certifications"], summary="Update a certification"),
    partial_update=extend_schema(
        tags=["Certifications"], summary="Partially update a certification"
    ),
    destroy=extend_schema(tags=["Certifications"], summary="Delete a certification"),
)
class CertificationViewSet(BaseViewSet):
    model = Certification
    serializer_class = CertificationSerializer

    def _owned_qs(self, request):
        if request.user.is_staff:
            return Certification.objects.all()
        return Certification.objects.filter(
            resumecertification__resume__user_id=request.user.id
        ).distinct()

    def list(self, request):
        items = list(self._owned_qs(request))
        items = self.paginate(items)
        ser = self.get_serializer()
        data = [ser.to_resource(o) for o in items]
        payload = {"data": data}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included(items, include_rels, request)
        return Response(payload)

    def retrieve(self, request, pk=None):
        obj = self._owned_qs(request).filter(pk=int(pk)).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = self.get_serializer()
        payload = {"data": ser.to_resource(obj)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([obj], include_rels, request)
        return Response(payload)

    def destroy(self, request, pk=None):
        if not self._owned_qs(request).filter(pk=int(pk)).exists():
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        Certification.objects.filter(pk=int(pk)).delete()
        return Response(status=204)

    @extend_schema(
        tags=["Certifications"],
        summary="Create a certification (optionally link to resumes via relationships.resumes)",
        request=_JSONAPI_WRITE,
        responses={
            201: _JSONAPI_ITEM,
            400: OpenApiResponse(description="Invalid resume IDs"),
        },
    )
    def create(self, request):
        ser = self.get_serializer()
        try:
            attrs = ser.parse_payload(request.data)
        except ValueError as e:
            return Response(
                {"errors": [{"detail": str(e)}]}, status=status.HTTP_400_BAD_REQUEST
            )

        data = request.data if isinstance(request.data, dict) else {}
        node = data.get("data") or {}
        relationships = node.get("relationships") or {}

        # Accept both "resumes" (list) and "resume" (single)
        res_rel = relationships.get("resumes") or relationships.get("resume") or {}
        resume_ids = []
        if isinstance(res_rel, dict):
            d = res_rel.get("data")
            items = d if isinstance(d, list) else ([d] if isinstance(d, dict) else [])
            for it in items:
                if not isinstance(it, dict):
                    continue
                rid = it.get("id")
                if rid is None:
                    continue
                try:
                    resume_ids.append(int(rid))
                except (TypeError, ValueError):
                    return Response(
                        {"errors": [{"detail": f"Invalid resume id: {rid}"}]},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

        # Validate referenced resumes (if provided)
        invalid = [
            rid for rid in resume_ids if not Resume.objects.filter(pk=rid).exists()
        ]
        if invalid:
            return Response(
                {
                    "errors": [
                        {
                            "detail": f"Invalid resume ID(s): {', '.join(map(str, invalid))}"
                        }
                    ]
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Create Certification
        cert = Certification.objects.create(**attrs)

        # Populate join table
        for rid in resume_ids:
            ResumeCertification.objects.get_or_create(
                resume_id=rid, certification_id=cert.id
            )

        payload = {"data": ser.to_resource(cert)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([cert], include_rels, request)
        return Response(payload, status=status.HTTP_201_CREATED)


@extend_schema_view(
    list=extend_schema(tags=["Descriptions"], summary="List descriptions"),
    retrieve=extend_schema(tags=["Descriptions"], summary="Retrieve a description"),
    update=extend_schema(
        tags=["Descriptions"],
        summary="Update a description (also upserts experience join links with ordering)",
    ),
    partial_update=extend_schema(
        tags=["Descriptions"],
        summary="Partially update a description (also upserts experience join links with ordering)",
    ),
    destroy=extend_schema(tags=["Descriptions"], summary="Delete a description"),
)
class DescriptionViewSet(BaseViewSet):
    model = Description
    serializer_class = DescriptionSerializer

    def _owned_qs(self, request):
        if request.user.is_staff:
            return Description.objects.all()
        via_experience = Description.objects.filter(
            experiencedescription__experience__resumeexperience__resume__user_id=request.user.id
        )
        via_project = Description.objects.filter(
            projectdescription__project__user_id=request.user.id
        )
        return (via_experience | via_project).distinct()

    def list(self, request):
        items = list(self._owned_qs(request))
        items = self.paginate(items)
        ser = self.get_serializer()
        data = [ser.to_resource(o) for o in items]
        payload = {"data": data}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included(items, include_rels, request)
        return Response(payload)

    def retrieve(self, request, pk=None):
        obj = self._owned_qs(request).filter(pk=int(pk)).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = self.get_serializer()
        payload = {"data": ser.to_resource(obj)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([obj], include_rels, request)
        return Response(payload)

    def destroy(self, request, pk=None):
        if not self._owned_qs(request).filter(pk=int(pk)).exists():
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        Description.objects.filter(pk=int(pk)).delete()
        return Response(status=204)

    @extend_schema(
        tags=["Descriptions"],
        summary="Create a description (optionally link to experiences via relationships.experiences; support per-link order via meta.order)",
        request=_JSONAPI_WRITE,
        responses={
            201: _JSONAPI_ITEM,
            400: OpenApiResponse(description="Invalid experience IDs"),
        },
    )
    def create(self, request):
        ser = self.get_serializer()
        try:
            attrs = ser.parse_payload(request.data)
        except ValueError as e:
            return Response(
                {"errors": [{"detail": str(e)}]}, status=status.HTTP_400_BAD_REQUEST
            )

        data = request.data if isinstance(request.data, dict) else {}
        node = data.get("data") or {}
        relationships = node.get("relationships") or {}
        attrs_node = node.get("attributes") or {}
        global_order = attrs_node.get("order")

        exp_rel = (
            relationships.get("experiences") or relationships.get("experience") or {}
        )
        exp_items = []  # list of tuples (experience_id, order)
        if isinstance(exp_rel, dict):
            d = exp_rel.get("data")
            # Accept list or single object
            items = d if isinstance(d, list) else ([d] if isinstance(d, dict) else [])
            for it in items:
                if not isinstance(it, dict):
                    continue
                rid = it.get("id")
                if rid is None:
                    continue
                try:
                    eid = int(rid)
                except (TypeError, ValueError):
                    return Response(
                        {"errors": [{"detail": f"Invalid experience id: {rid}"}]},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                order = None
                meta = it.get("meta")
                if isinstance(meta, dict) and "order" in meta:
                    try:
                        order = int(meta.get("order"))
                    except (TypeError, ValueError):
                        order = None
                if order is None and global_order is not None:
                    try:
                        order = int(global_order)
                    except (TypeError, ValueError):
                        order = None
                exp_items.append((eid, order))

        # Validate referenced experiences before creating description
        invalid_ids = [
            eid
            for eid, _ in exp_items
            if not Experience.objects.filter(pk=eid).exists()
        ]
        if invalid_ids:
            return Response(
                {
                    "errors": [
                        {
                            "detail": f"Invalid experience ID(s): {', '.join(map(str, invalid_ids))}"
                        }
                    ]
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Create description
        desc = Description.objects.create(**attrs)

        # Populate join table with optional per-link order
        for eid, order in exp_items:
            ExperienceDescription.objects.get_or_create(
                experience_id=eid,
                description_id=desc.id,
                defaults={"order": (order if order is not None else 0)},
            )

        payload = {"data": ser.to_resource(desc)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([desc], include_rels, request)
        return Response(payload, status=status.HTTP_201_CREATED)

    def _upsert(self, request, pk, partial=False):
        desc = self._owned_qs(request).filter(pk=int(pk)).first()
        if not desc:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        ser = self.get_serializer()
        try:
            attrs = ser.parse_payload(request.data)  # handles 'content'
        except ValueError as e:
            return Response(
                {"errors": [{"detail": str(e)}]}, status=status.HTTP_400_BAD_REQUEST
            )

        # Update scalar attributes
        for k, v in attrs.items():
            setattr(desc, k, v)
        desc.save()

        # Handle experiences relationship and per-link 'order'
        data = request.data if isinstance(request.data, dict) else {}
        node = data.get("data") or {}
        relationships = node.get("relationships") or {}
        attrs_node = node.get("attributes") or {}
        global_order = attrs_node.get("order")

        exp_rel = (
            relationships.get("experiences") or relationships.get("experience") or {}
        )
        if isinstance(exp_rel, dict):
            d = exp_rel.get("data")
            items = d if isinstance(d, list) else ([d] if isinstance(d, dict) else [])
            for it in items:
                if not isinstance(it, dict):
                    continue
                rid = it.get("id")
                if rid is None:
                    continue
                try:
                    eid = int(rid)
                except (TypeError, ValueError):
                    return Response(
                        {"errors": [{"detail": f"Invalid experience id: {rid}"}]},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                if not Experience.objects.filter(pk=eid).exists():
                    return Response(
                        {"errors": [{"detail": f"Invalid experience ID: {eid}"}]},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                # Determine order: meta.order takes precedence; fallback to attributes.order
                order_val = None
                meta = it.get("meta")
                if isinstance(meta, dict) and "order" in meta:
                    try:
                        order_val = int(meta.get("order"))
                    except (TypeError, ValueError):
                        order_val = None
                if order_val is None and global_order is not None:
                    try:
                        order_val = int(global_order)
                    except (TypeError, ValueError):
                        order_val = None

                link, created = ExperienceDescription.objects.get_or_create(
                    experience_id=eid,
                    description_id=desc.id,
                    defaults={"order": (order_val if order_val is not None else 0)},
                )
                if not created and order_val is not None:
                    link.order = order_val
                    link.save()

        payload = {"data": ser.to_resource(desc)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([desc], include_rels, request)
        return Response(payload)

    @extend_schema(
        tags=["Descriptions"],
        summary="List experiences linked to a description",
        responses={200: _JSONAPI_LIST},
    )
    @action(detail=True, methods=["get"])
    def experiences(self, request, pk=None):
        obj = self._owned_qs(request).filter(pk=int(pk)).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        exp_ids = list(
            ExperienceDescription.objects.filter(description_id=obj.id).values_list(
                "experience_id", flat=True
            )
        )
        experiences = list(Experience.objects.filter(pk__in=exp_ids))
        data = [ExperienceSerializer().to_resource(e) for e in experiences]
        return Response({"data": data})


@extend_schema_view(
    list=extend_schema(tags=["Projects"], summary="List projects"),
    retrieve=extend_schema(tags=["Projects"], summary="Retrieve a project"),
    create=extend_schema(tags=["Projects"], summary="Create a project"),
    update=extend_schema(tags=["Projects"], summary="Update a project"),
    partial_update=extend_schema(tags=["Projects"], summary="Partial update a project"),
    destroy=extend_schema(tags=["Projects"], summary="Delete a project"),
)
class ProjectViewSet(BaseViewSet):
    model = Project
    serializer_class = ProjectSerializer

    def _owned_qs(self, request):
        if request.user.is_staff:
            return Project.objects.all()
        return Project.objects.filter(user_id=request.user.id)

    def list(self, request):
        items = list(self._owned_qs(request))
        items = self.paginate(items)
        ser = self.get_serializer()
        data = [ser.to_resource(o) for o in items]
        payload = {"data": data}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included(items, include_rels, request)
        return Response(payload)

    def retrieve(self, request, pk=None):
        obj = self._owned_qs(request).filter(pk=int(pk)).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = self.get_serializer()
        payload = {"data": ser.to_resource(obj)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([obj], include_rels, request)
        return Response(payload)

    def destroy(self, request, pk=None):
        if not self._owned_qs(request).filter(pk=int(pk)).exists():
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        Project.objects.filter(pk=int(pk)).delete()
        return Response(status=204)

    @extend_schema(
        tags=["Projects"],
        summary="List descriptions for a project",
        responses={200: _JSONAPI_LIST},
    )
    @action(detail=True, methods=["get"])
    def descriptions(self, request, pk=None):
        obj = self._owned_qs(request).filter(pk=int(pk)).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = DescriptionSerializer()
        ser.set_parent_context("project", obj.id, "descriptions")
        desc_ids = list(
            ProjectDescription.objects.filter(project_id=obj.id)
            .order_by("order")
            .values_list("description_id", flat=True)
        )
        desc_map = {d.id: d for d in Description.objects.filter(pk__in=desc_ids)}
        items = [desc_map[did] for did in desc_ids if did in desc_map]
        data = [ser.to_resource(d) for d in items]
        return Response({"data": data})

