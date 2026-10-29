# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Third party imports
from rest_framework import serializers

# Module imports
from .base import BaseSerializer
from plane.db.models import Page, ProjectPage
from plane.utils.content_validator import validate_html_content


def _sanitize_description_html(value):
    is_valid, error_message, sanitized_html = validate_html_content(value)
    if not is_valid:
        raise serializers.ValidationError(error_message or "html content is not valid")
    return sanitized_html if sanitized_html is not None else value


class PageSerializer(BaseSerializer):
    """
    Read serializer for project pages.

    Exposes the page body as `description_html`; the collaborative binary
    document is internal to the editor and is never returned.
    """

    projects = serializers.SerializerMethodField()

    class Meta:
        model = Page
        fields = [
            "id",
            "name",
            "description_html",
            "description_stripped",
            "owned_by",
            "access",
            "color",
            "parent",
            "is_locked",
            "archived_at",
            "view_props",
            "logo_props",
            "external_id",
            "external_source",
            "workspace",
            "projects",
            "created_at",
            "updated_at",
            "created_by",
            "updated_by",
        ]
        read_only_fields = fields

    def get_projects(self, obj):
        return [
            str(project_id)
            for project_id in ProjectPage.objects.filter(page_id=obj.id, deleted_at__isnull=True).values_list(
                "project_id", flat=True
            )
        ]


class PageCreateSerializer(BaseSerializer):
    """
    Serializer for creating a project page.

    The page is created from `description_html`; the live server builds the
    collaborative document from it the first time the page is opened.
    """

    description_html = serializers.CharField(required=False, allow_blank=True, default="<p></p>")

    class Meta:
        model = Page
        fields = [
            "name",
            "description_html",
            "access",
            "color",
            "parent",
            "is_locked",
            "view_props",
            "logo_props",
            "external_id",
            "external_source",
        ]

    def validate_description_html(self, value):
        return _sanitize_description_html(value or "<p></p>")

    def validate_parent(self, value):
        if value is None:
            return value
        if not ProjectPage.objects.filter(
            page_id=value.id,
            project_id=self.context["project_id"],
            deleted_at__isnull=True,
        ).exists():
            raise serializers.ValidationError("Parent page does not belong to this project")
        return value

    def create(self, validated_data):
        project_id = self.context["project_id"]
        workspace_id = self.context["workspace_id"]

        page = Page.objects.create(
            **validated_data,
            description_binary=None,
            owned_by_id=self.context["owned_by_id"],
            workspace_id=workspace_id,
        )
        ProjectPage.objects.create(
            workspace_id=workspace_id,
            project_id=project_id,
            page_id=page.id,
            created_by_id=page.created_by_id,
            updated_by_id=page.updated_by_id,
        )
        return page


class PageUpdateSerializer(BaseSerializer):
    """
    Serializer for updating a project page.

    Sending `description_html` replaces the whole page body.
    """

    class Meta:
        model = Page
        fields = [
            "name",
            "description_html",
            "access",
            "color",
            "parent",
            "is_locked",
            "view_props",
            "logo_props",
            "external_id",
            "external_source",
        ]

    def validate_description_html(self, value):
        return _sanitize_description_html(value or "<p></p>")

    def validate_parent(self, value):
        if value is None:
            return value
        if value.id == self.instance.id:
            raise serializers.ValidationError("A page cannot be its own parent")
        if not ProjectPage.objects.filter(
            page_id=value.id,
            project_id=self.context["project_id"],
            deleted_at__isnull=True,
        ).exists():
            raise serializers.ValidationError("Parent page does not belong to this project")
        return value

    def update(self, instance, validated_data):
        if "description_html" in validated_data:
            # Drop the stored collaborative document so the live server rebuilds
            # it from the new HTML instead of serving the old content.
            instance.description_binary = None
            instance.description_json = {}
        return super().update(instance, validated_data)
