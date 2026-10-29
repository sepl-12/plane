# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Python imports
import json

# Django imports
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import Q
from django.utils import timezone

# Third party imports
from rest_framework import status
from rest_framework.response import Response
from drf_spectacular.utils import OpenApiParameter, OpenApiRequest, OpenApiResponse, OpenApiTypes

# Module imports
from plane.api.serializers import PageSerializer, PageCreateSerializer, PageUpdateSerializer
from plane.app.permissions import ROLE
from plane.app.permissions.page import ProjectPagePermission
from plane.app.views.page.base import unarchive_archive_page_and_descendants
from plane.bgtasks.page_transaction_task import page_transaction
from plane.bgtasks.page_version_task import track_page_version
from plane.db.models import Page, Project, ProjectMember, UserFavorite, UserRecentVisit
from plane.utils.openapi import (
    page_docs,
    CURSOR_PARAMETER,
    PER_PAGE_PARAMETER,
    FIELDS_PARAMETER,
    create_paginated_response,
    INVALID_REQUEST_RESPONSE,
    DELETED_RESPONSE,
    ARCHIVED_RESPONSE,
    UNARCHIVED_RESPONSE,
)
from .base import BaseAPIView

ARCHIVED_PARAMETER = OpenApiParameter(
    name="archived",
    type=OpenApiTypes.BOOL,
    location=OpenApiParameter.QUERY,
    description="Return archived pages instead of active ones",
    required=False,
)

CONTENT_EDITED_IN_EDITOR_RESPONSE = OpenApiResponse(
    description=(
        "The page body cannot be replaced because the page has been opened in the editor. "
        "Edit it in Plane, or create a new page."
    )
)


def get_project_page(slug, project_id, page_id):
    return Page.objects.get(
        pk=page_id,
        workspace__slug=slug,
        project_pages__project_id=project_id,
        project_pages__deleted_at__isnull=True,
    )


def is_project_admin(user, slug, project_id):
    return ProjectMember.objects.filter(
        workspace__slug=slug,
        project_id=project_id,
        member=user,
        role=ROLE.ADMIN.value,
        is_active=True,
    ).exists()


class PageListCreateAPIEndpoint(BaseAPIView):
    """Project Page List and Create Endpoint"""

    model = Page
    serializer_class = PageSerializer
    permission_classes = [ProjectPagePermission]
    use_read_replica = True

    def get_queryset(self):
        slug = self.kwargs.get("slug")
        project_id = self.kwargs.get("project_id")
        queryset = (
            Page.objects.filter(
                workspace__slug=slug,
                project_pages__project_id=project_id,
                project_pages__deleted_at__isnull=True,
                projects__archived_at__isnull=True,
            )
            .filter(Q(owned_by=self.request.user) | Q(access=Page.PUBLIC_ACCESS))
            .select_related("workspace", "owned_by")
            .distinct()
        )

        # Guests only see their own pages unless the project opens everything to them
        project = Project.objects.get(pk=project_id, workspace__slug=slug)
        if (
            ProjectMember.objects.filter(
                workspace__slug=slug,
                project_id=project_id,
                member=self.request.user,
                role=ROLE.GUEST.value,
                is_active=True,
            ).exists()
            and not project.guest_view_all_features
        ):
            queryset = queryset.filter(owned_by=self.request.user)
        return queryset

    @page_docs(
        operation_id="list_pages",
        summary="List pages",
        description="List the pages of a project. Archived pages are returned only with `archived=true`.",
        parameters=[CURSOR_PARAMETER, PER_PAGE_PARAMETER, FIELDS_PARAMETER, ARCHIVED_PARAMETER],
        responses={
            200: create_paginated_response(
                PageSerializer,
                "PaginatedPageResponse",
                "Paginated list of pages",
                "Paginated Pages",
            ),
        },
    )
    def get(self, request, slug, project_id):
        """List pages

        List the pages of a project that the user can see.
        """
        archived = request.GET.get("archived", "false").lower() == "true"
        queryset = self.get_queryset().filter(archived_at__isnull=not archived)
        return self.paginate(
            request=request,
            queryset=queryset,
            on_results=lambda pages: PageSerializer(pages, many=True, fields=self.fields).data,
        )

    @page_docs(
        operation_id="create_page",
        summary="Create page",
        description="Create a page in a project from HTML content.",
        request=OpenApiRequest(request=PageCreateSerializer),
        responses={
            201: OpenApiResponse(description="Page created", response=PageSerializer),
            400: INVALID_REQUEST_RESPONSE,
        },
    )
    def post(self, request, slug, project_id):
        """Create page

        Create a page in a project. The body is given as `description_html`.
        """
        project = Project.objects.get(pk=project_id, workspace__slug=slug)
        serializer = PageCreateSerializer(
            data=request.data,
            context={
                "project_id": project_id,
                "workspace_id": project.workspace_id,
                "owned_by_id": request.user.id,
            },
        )
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        if (
            request.data.get("external_id")
            and request.data.get("external_source")
            and Page.objects.filter(
                workspace__slug=slug,
                project_pages__project_id=project_id,
                project_pages__deleted_at__isnull=True,
                external_source=request.data.get("external_source"),
                external_id=request.data.get("external_id"),
            ).exists()
        ):
            page = Page.objects.filter(
                workspace__slug=slug,
                project_pages__project_id=project_id,
                project_pages__deleted_at__isnull=True,
                external_source=request.data.get("external_source"),
                external_id=request.data.get("external_id"),
            ).first()
            return Response(
                {
                    "error": "Page with the same external id and external source already exists",
                    "id": str(page.id),
                },
                status=status.HTTP_409_CONFLICT,
            )

        page = serializer.save()
        page_transaction.delay(
            new_description_html=page.description_html,
            old_description_html=None,
            page_id=page.id,
        )
        return Response(PageSerializer(page).data, status=status.HTTP_201_CREATED)


class PageDetailAPIEndpoint(BaseAPIView):
    """Project Page Detail Endpoint"""

    model = Page
    serializer_class = PageSerializer
    permission_classes = [ProjectPagePermission]
    use_read_replica = True

    @page_docs(
        operation_id="retrieve_page",
        summary="Retrieve page",
        description="Retrieve a page with its HTML content.",
        responses={200: OpenApiResponse(description="Page details", response=PageSerializer)},
    )
    def get(self, request, slug, project_id, page_id):
        """Retrieve page"""
        page = get_project_page(slug, project_id, page_id)
        return Response(PageSerializer(page, fields=self.fields).data, status=status.HTTP_200_OK)

    @page_docs(
        operation_id="update_page",
        summary="Update page",
        description=(
            "Update a page. Only the fields sent are changed; `description_html` replaces the whole body "
            "and is only accepted while the page has never been opened in the editor. "
            "Locked or archived pages are refused, and only the owner can change `access`."
        ),
        request=OpenApiRequest(request=PageUpdateSerializer),
        responses={
            200: OpenApiResponse(description="Page updated", response=PageSerializer),
            400: INVALID_REQUEST_RESPONSE,
            409: CONTENT_EDITED_IN_EDITOR_RESPONSE,
        },
    )
    def patch(self, request, slug, project_id, page_id):
        """Update page"""
        page = get_project_page(slug, project_id, page_id)

        if page.archived_at:
            return Response({"error": "Page is archived"}, status=status.HTTP_400_BAD_REQUEST)

        # A locked page may only be unlocked
        if page.is_locked and set(request.data.keys()) - {"is_locked"}:
            return Response({"error": "Page is locked"}, status=status.HTTP_400_BAD_REQUEST)

        if page.access != request.data.get("access", page.access) and page.owned_by_id != request.user.id:
            return Response(
                {"error": "Access cannot be updated since this page is owned by someone else"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Once the page has been opened, the live server owns the collaborative
        # document. Browsers that still hold it keep saving their copy, which
        # overwrites or duplicates a body replaced here, so refuse instead.
        if "description_html" in request.data and page.description_binary:
            return Response(
                {
                    "error": (
                        "The page body cannot be replaced because the page has been opened in the editor. "
                        "Edit it in Plane, or create a new page."
                    ),
                    "error_code": "PAGE_CONTENT_OWNED_BY_EDITOR",
                },
                status=status.HTTP_409_CONFLICT,
            )

        serializer = PageUpdateSerializer(
            page,
            data=request.data,
            partial=True,
            context={"project_id": project_id},
        )
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        description_changed = "description_html" in serializer.validated_data
        old_description_html = page.description_html

        page = serializer.save()

        if description_changed:
            page_transaction.delay(
                new_description_html=page.description_html,
                old_description_html=old_description_html,
                page_id=page.id,
            )
            track_page_version.delay(
                page_id=page.id,
                existing_instance=json.dumps({"description_html": old_description_html}, cls=DjangoJSONEncoder),
                user_id=request.user.id,
            )

        return Response(PageSerializer(page).data, status=status.HTTP_200_OK)

    @page_docs(
        operation_id="update_page_put",
        summary="Update page (PUT)",
        description="Same as PATCH. Kept because the Plane SDK sends partial page updates with PUT.",
        request=OpenApiRequest(request=PageUpdateSerializer),
        responses={
            200: OpenApiResponse(description="Page updated", response=PageSerializer),
            400: INVALID_REQUEST_RESPONSE,
        },
    )
    def put(self, request, slug, project_id, page_id):
        """Update page (PUT)"""
        return self.patch(request, slug, project_id, page_id)

    @page_docs(
        operation_id="delete_page",
        summary="Delete page",
        description="Permanently delete an archived page. Only the owner or a project admin can delete it.",
        responses={204: DELETED_RESPONSE, 400: INVALID_REQUEST_RESPONSE},
    )
    def delete(self, request, slug, project_id, page_id):
        """Delete page"""
        page = get_project_page(slug, project_id, page_id)

        if page.archived_at is None:
            return Response(
                {"error": "The page should be archived before deleting"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if page.owned_by_id != request.user.id and not is_project_admin(request.user, slug, project_id):
            return Response(
                {"error": "Only admin or owner can delete the page"},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Detach child pages instead of deleting them
        Page.objects.filter(
            parent_id=page_id,
            workspace__slug=slug,
            project_pages__project_id=project_id,
            project_pages__deleted_at__isnull=True,
        ).update(parent=None)

        page.delete()

        UserFavorite.objects.filter(
            project=project_id,
            workspace__slug=slug,
            entity_identifier=page_id,
            entity_type="page",
        ).delete()
        UserRecentVisit.objects.filter(
            project_id=project_id,
            workspace__slug=slug,
            entity_identifier=page_id,
            entity_name="page",
        ).delete(soft=False)
        return Response(status=status.HTTP_204_NO_CONTENT)


class PageArchiveUnarchiveAPIEndpoint(BaseAPIView):
    """Project Page Archive and Unarchive Endpoint"""

    permission_classes = [ProjectPagePermission]
    use_read_replica = True

    @page_docs(
        operation_id="archive_page",
        summary="Archive page",
        description="Archive a page and its sub-pages. Only the owner or a project admin can archive it.",
        request={},
        responses={200: ARCHIVED_RESPONSE},
    )
    def post(self, request, slug, project_id, page_id):
        """Archive page"""
        page = get_project_page(slug, project_id, page_id)

        if page.owned_by_id != request.user.id and not is_project_admin(request.user, slug, project_id):
            return Response(
                {"error": "Only the owner or admin can archive the page"},
                status=status.HTTP_403_FORBIDDEN,
            )

        UserFavorite.objects.filter(
            entity_type="page",
            entity_identifier=page_id,
            project_id=project_id,
            workspace__slug=slug,
        ).delete()

        archived_at = timezone.now()
        unarchive_archive_page_and_descendants(page_id, archived_at)
        return Response({"archived_at": str(archived_at.date())}, status=status.HTTP_200_OK)

    @page_docs(
        operation_id="unarchive_page",
        summary="Unarchive page",
        description="Restore an archived page and its sub-pages. Only the owner or a project admin can restore it.",
        responses={204: UNARCHIVED_RESPONSE},
    )
    def delete(self, request, slug, project_id, page_id):
        """Unarchive page"""
        page = get_project_page(slug, project_id, page_id)

        if page.owned_by_id != request.user.id and not is_project_admin(request.user, slug, project_id):
            return Response(
                {"error": "Only the owner or admin can un archive the page"},
                status=status.HTTP_403_FORBIDDEN,
            )

        # An archived parent would hide the restored page, so detach it
        if page.parent_id and page.parent.archived_at:
            page.parent = None
            page.save(update_fields=["parent"])

        unarchive_archive_page_and_descendants(page_id, None)
        return Response(status=status.HTTP_204_NO_CONTENT)
