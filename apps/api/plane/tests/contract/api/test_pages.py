# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import json
from unittest import mock
from uuid import uuid4

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from plane.api.rate_limit import ApiKeyRateThrottle
from plane.db.models import Page, Project, ProjectMember, ProjectPage, User, WorkspaceMember
from plane.db.models.api import APIToken


@pytest.fixture(autouse=True)
def background_calls():
    """Keep page tests away from celery, redis, the force-close wait and the
    shared API key rate limit (other contract tests use the same token)"""
    with (
        mock.patch("plane.api.views.page.page_transaction") as page_transaction,
        mock.patch("plane.api.views.page.track_page_version") as track_page_version,
        mock.patch("plane.api.views.page.redis_instance") as redis_instance,
        mock.patch("plane.api.views.page.time.sleep"),
        mock.patch.object(ApiKeyRateThrottle, "allow_request", return_value=True),
    ):
        yield {
            "page_transaction": page_transaction,
            "track_page_version": track_page_version,
            "redis": redis_instance.return_value,
        }


@pytest.fixture
def project(db, workspace, create_user):
    project = Project.objects.create(
        name="Test Project",
        identifier="TP",
        workspace=workspace,
        created_by=create_user,
    )
    ProjectMember.objects.create(project=project, member=create_user, role=20, is_active=True)
    return project


@pytest.fixture
def other_project(db, workspace, create_user):
    project = Project.objects.create(
        name="Other Project",
        identifier="OP",
        workspace=workspace,
        created_by=create_user,
    )
    ProjectMember.objects.create(project=project, member=create_user, role=20, is_active=True)
    return project


@pytest.fixture
def member_user(db, workspace, project):
    unique_id = uuid4().hex[:8]
    user = User.objects.create(email=f"member-{unique_id}@plane.so", username=f"member_{unique_id}")
    WorkspaceMember.objects.create(workspace=workspace, member=user, role=15)
    ProjectMember.objects.create(project=project, member=user, role=15, is_active=True)
    return user


@pytest.fixture
def member_client(db, member_user):
    token = APIToken.objects.create(user=member_user, label="Member token", token="member-api-token-12345")
    client = APIClient()
    client.credentials(HTTP_X_API_KEY=token.token)
    return client


def make_page(project, owner, **kwargs):
    page = Page.objects.create(
        workspace=project.workspace,
        owned_by=owner,
        name=kwargs.pop("name", "Existing page"),
        description_html=kwargs.pop("description_html", "<p>Old body</p>"),
        **kwargs,
    )
    ProjectPage.objects.create(workspace=project.workspace, project=project, page=page)
    return page


def pages_url(slug, project_id):
    return f"/api/v1/workspaces/{slug}/projects/{project_id}/pages/"


def page_url(slug, project_id, page_id):
    return f"{pages_url(slug, project_id)}{page_id}/"


def archive_url(slug, project_id, page_id):
    return f"{page_url(slug, project_id, page_id)}archive/"


@pytest.mark.contract
class TestPageListCreateAPIEndpoint:
    @pytest.mark.django_db
    def test_create_page(self, api_key_client, workspace, project, create_user, background_calls):
        response = api_key_client.post(
            pages_url(workspace.slug, project.id),
            {"name": "SOP", "description_html": "<h2>Deploy</h2><p>Steps</p>"},
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED
        page = Page.objects.get(pk=response.data["id"])
        assert page.name == "SOP"
        assert "Deploy" in page.description_html
        assert page.owned_by == create_user
        assert page.description_binary is None
        assert ProjectPage.objects.filter(page=page, project=project).exists()
        assert response.data["projects"] == [str(project.id)]
        background_calls["page_transaction"].delay.assert_called_once()

    @pytest.mark.django_db
    def test_create_page_sanitizes_html(self, api_key_client, workspace, project):
        response = api_key_client.post(
            pages_url(workspace.slug, project.id),
            {"name": "XSS", "description_html": "<p>ok</p><script>alert(1)</script>"},
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert "<script" not in Page.objects.get(pk=response.data["id"]).description_html

    @pytest.mark.django_db
    def test_create_page_rejects_parent_from_other_project(
        self, api_key_client, workspace, project, other_project, create_user
    ):
        foreign = make_page(other_project, create_user)

        response = api_key_client.post(
            pages_url(workspace.slug, project.id),
            {"name": "Child", "description_html": "<p>x</p>", "parent": str(foreign.id)},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    @pytest.mark.django_db
    def test_create_page_duplicate_external_id(self, api_key_client, workspace, project, create_user):
        existing = make_page(project, create_user, external_id="sop-1", external_source="vps")

        response = api_key_client.post(
            pages_url(workspace.slug, project.id),
            {"name": "Again", "description_html": "<p>x</p>", "external_id": "sop-1", "external_source": "vps"},
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["id"] == str(existing.id)

    @pytest.mark.django_db
    def test_list_pages_hides_archived_and_others_private(
        self, api_key_client, workspace, project, create_user, member_user
    ):
        active = make_page(project, create_user, name="Active")
        make_page(project, create_user, name="Archived", archived_at="2026-01-01")
        make_page(project, member_user, name="Someone else private", access=Page.PRIVATE_ACCESS)

        response = api_key_client.get(pages_url(workspace.slug, project.id))

        assert response.status_code == status.HTTP_200_OK
        assert [str(page["id"]) for page in response.data["results"]] == [str(active.id)]

    @pytest.mark.django_db
    def test_list_archived_pages(self, api_key_client, workspace, project, create_user):
        make_page(project, create_user, name="Active")
        archived = make_page(project, create_user, name="Archived", archived_at="2026-01-01")

        response = api_key_client.get(pages_url(workspace.slug, project.id), {"archived": "true"})

        assert response.status_code == status.HTTP_200_OK
        assert [str(page["id"]) for page in response.data["results"]] == [str(archived.id)]

    @pytest.mark.django_db
    def test_non_member_cannot_list(self, member_client, workspace, other_project):
        response = member_client.get(pages_url(workspace.slug, other_project.id))

        assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.contract
class TestPageDetailAPIEndpoint:
    @pytest.mark.django_db
    def test_retrieve_page(self, api_key_client, workspace, project, create_user):
        page = make_page(project, create_user)

        response = api_key_client.get(page_url(workspace.slug, project.id, page.id))

        assert response.status_code == status.HTTP_200_OK
        assert response.data["description_html"] == "<p>Old body</p>"

    @pytest.mark.django_db
    def test_page_from_other_project_is_not_reachable(
        self, api_key_client, workspace, project, other_project, create_user
    ):
        page = make_page(other_project, create_user)

        response = api_key_client.get(page_url(workspace.slug, project.id, page.id))

        assert response.status_code == status.HTTP_403_FORBIDDEN

    @pytest.mark.django_db
    def test_update_description_resets_document_and_closes_editors(
        self, api_key_client, workspace, project, create_user, background_calls
    ):
        page = make_page(project, create_user, description_binary=b"old-yjs-state")

        response = api_key_client.patch(
            page_url(workspace.slug, project.id, page.id),
            {"description_html": "<p>New body</p>"},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        page.refresh_from_db()
        assert page.description_html == "<p>New body</p>"
        assert page.description_binary is None

        channel, payload = background_calls["redis"].publish.call_args.args
        assert channel == "hocuspocus:admin"
        assert json.loads(payload)["command"] == "force_close"
        assert json.loads(payload)["docId"] == str(page.id)
        background_calls["page_transaction"].delay.assert_called_once()
        background_calls["track_page_version"].delay.assert_called_once()

    @pytest.mark.django_db
    def test_rename_keeps_document(self, api_key_client, workspace, project, create_user, background_calls):
        page = make_page(project, create_user, description_binary=b"old-yjs-state")

        response = api_key_client.put(
            page_url(workspace.slug, project.id, page.id),
            {"name": "Renamed"},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        page.refresh_from_db()
        assert page.name == "Renamed"
        assert bytes(page.description_binary) == b"old-yjs-state"
        background_calls["redis"].publish.assert_not_called()

    @pytest.mark.django_db
    def test_locked_page_can_only_be_unlocked(self, api_key_client, workspace, project, create_user):
        page = make_page(project, create_user, is_locked=True)
        url = page_url(workspace.slug, project.id, page.id)

        response = api_key_client.patch(url, {"name": "Nope"}, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST

        response = api_key_client.patch(url, {"is_locked": False}, format="json")
        assert response.status_code == status.HTTP_200_OK
        page.refresh_from_db()
        assert page.is_locked is False

    @pytest.mark.django_db
    def test_archived_page_cannot_be_updated(self, api_key_client, workspace, project, create_user):
        page = make_page(project, create_user, archived_at="2026-01-01")

        response = api_key_client.patch(
            page_url(workspace.slug, project.id, page.id), {"name": "Nope"}, format="json"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    @pytest.mark.django_db
    def test_only_owner_can_change_access(self, member_client, workspace, project, create_user):
        page = make_page(project, create_user)

        response = member_client.patch(
            page_url(workspace.slug, project.id, page.id),
            {"access": Page.PRIVATE_ACCESS},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    @pytest.mark.django_db
    def test_delete_requires_archive(self, api_key_client, workspace, project, create_user):
        page = make_page(project, create_user)
        url = page_url(workspace.slug, project.id, page.id)

        response = api_key_client.delete(url)
        assert response.status_code == status.HTTP_400_BAD_REQUEST

        page.archived_at = "2026-01-01"
        page.save()
        response = api_key_client.delete(url)
        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert not Page.objects.filter(pk=page.id).exists()

    @pytest.mark.django_db
    def test_member_cannot_delete_others_page(self, member_client, workspace, project, create_user):
        page = make_page(project, create_user, archived_at="2026-01-01")

        response = member_client.delete(page_url(workspace.slug, project.id, page.id))

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert Page.objects.filter(pk=page.id).exists()


@pytest.mark.contract
class TestPageArchiveUnarchiveAPIEndpoint:
    @pytest.mark.django_db
    def test_archive_and_unarchive(self, api_key_client, workspace, project, create_user):
        parent = make_page(project, create_user, name="Parent")
        child = make_page(project, create_user, name="Child", parent=parent)
        url = archive_url(workspace.slug, project.id, parent.id)

        response = api_key_client.post(url)
        assert response.status_code == status.HTTP_200_OK
        parent.refresh_from_db()
        child.refresh_from_db()
        assert parent.archived_at is not None
        assert child.archived_at is not None

        response = api_key_client.delete(url)
        assert response.status_code == status.HTTP_204_NO_CONTENT
        parent.refresh_from_db()
        child.refresh_from_db()
        assert parent.archived_at is None
        assert child.archived_at is None

    @pytest.mark.django_db
    def test_member_cannot_archive_others_page(self, member_client, workspace, project, create_user):
        page = make_page(project, create_user)

        response = member_client.post(archive_url(workspace.slug, project.id, page.id))

        assert response.status_code == status.HTTP_403_FORBIDDEN
        page.refresh_from_db()
        assert page.archived_at is None
