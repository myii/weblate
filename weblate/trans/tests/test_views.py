#
# Copyright © 2012 - 2020 Michal Čihař <michal@cihar.com>
#
# This file is part of Weblate <https://weblate.org/>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#

"""Test for translation views."""


from io import BytesIO
from urllib.parse import urlsplit
from xml.dom import minidom
from zipfile import ZipFile

from django.contrib.messages import get_messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.test.client import RequestFactory
from django.test.utils import override_settings
from django.urls import reverse
from PIL import Image

from weblate.accounts.models import Profile
from weblate.auth.models import Group, Permission, Role, setup_project_groups
from weblate.lang.models import Language
from weblate.trans.models import Announcement, Component, ComponentList, Project
from weblate.trans.tests.test_models import RepoTestCase
from weblate.trans.tests.utils import (
    create_another_user,
    create_test_user,
    wait_for_celery,
)
from weblate.utils.hash import hash_to_checksum


class RegistrationTestMixin:
    """Helper to share code for registration testing."""

    def assert_registration_mailbox(self, match=None):
        if match is None:
            match = "[Weblate] Your registration on Weblate"
        # Check mailbox
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, match)

        live_url = getattr(self, "live_server_url", None)

        # Parse URL
        for line in mail.outbox[0].body.splitlines():
            if "verification_code" not in line:
                continue
            if "(" in line or ")" in line or "<" in line or ">" in line:
                continue
            if live_url and line.startswith(live_url):
                return line + "&confirm=1"
            if line.startswith("http://example.com"):
                return line[18:] + "&confirm=1"

        self.fail("Confirmation URL not found")
        return ""

    def assert_notify_mailbox(self, sent_mail):
        self.assertEqual(
            sent_mail.subject, "[Weblate] Activity on your account at Weblate"
        )


class ViewTestCase(RepoTestCase):
    def setUp(self):
        super().setUp()
        # Many tests needs access to the request factory.
        self.factory = RequestFactory()
        # Create user
        self.user = create_test_user()
        group = Group.objects.get(name="Users")
        self.user.groups.add(group)
        # Create another user
        self.anotheruser = create_another_user()
        self.user.groups.add(group)
        # Create project to have some test base
        self.component = self.create_component()
        self.project = self.component.project
        # Invalidate caches
        self.project.stats.invalidate()
        cache.clear()
        # Login
        self.client.login(username="testuser", password="testpassword")
        # Prepopulate kwargs
        self.kw_project = {"project": self.project.slug}
        self.kw_component = {
            "project": self.project.slug,
            "component": self.component.slug,
        }
        self.kw_translation = {
            "project": self.project.slug,
            "component": self.component.slug,
            "lang": "cs",
        }
        self.kw_lang_project = {"project": self.project.slug, "lang": "cs"}

        # Store URL for testing
        self.translation_url = self.get_translation().get_absolute_url()
        self.project_url = self.project.get_absolute_url()
        self.component_url = self.component.get_absolute_url()

    def update_fulltext_index(self):
        wait_for_celery()

    def make_manager(self):
        """Make user a Manager."""
        # Sitewide privileges
        self.user.groups.add(Group.objects.get(name="Managers"))
        # Project privileges
        self.project.add_user(self.user, "@Administration")

    def get_request(self, user=None):
        """Wrapper to get fake request object."""
        request = self.factory.get("/")
        request.user = user if user else self.user
        request.session = "session"
        messages = FallbackStorage(request)
        request._messages = messages
        return request

    def get_translation(self, language="cs"):
        return self.component.translation_set.get(language_code=language)

    def get_unit(self, source="Hello, world!\n", language="cs"):
        translation = self.get_translation(language)
        return translation.unit_set.get(source__startswith=source)

    def change_unit(self, target, source="Hello, world!\n", language="cs", user=None):
        unit = self.get_unit(source, language)
        unit.target = target
        unit.save_backend(user or self.user)

    def edit_unit(self, source, target, language="cs", **kwargs):
        """Do edit single unit using web interface."""
        unit = self.get_unit(source, language)
        params = {
            "checksum": unit.checksum,
            "contentsum": hash_to_checksum(unit.content_hash),
            "translationsum": hash_to_checksum(unit.get_target_hash()),
            "target_0": target,
            "review": "20",
        }
        params.update(kwargs)
        return self.client.post(
            self.get_translation(language).get_translate_url(), params
        )

    def assert_redirects_offset(self, response, exp_path, exp_offset):
        """Assert that offset in response matches expected one."""
        self.assertEqual(response.status_code, 302)

        # We don't use all variables
        # pylint: disable=unused-variable
        scheme, netloc, path, query, fragment = urlsplit(response["Location"])

        self.assertEqual(path, exp_path)

        exp_offset = "offset={0:d}".format(exp_offset)
        self.assertTrue(
            exp_offset in query, "Offset {0} not in {1}".format(exp_offset, query)
        )

    def assert_png(self, response):
        """Check whether response contains valid PNG image."""
        # Check response status code
        self.assertEqual(response.status_code, 200)
        self.assert_png_data(response.content)

    def assert_png_data(self, content):
        """Check whether data is PNG image."""
        # Try to load PNG with PIL
        image = Image.open(BytesIO(content))
        self.assertEqual(image.format, "PNG")

    def assert_zip(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")
        with ZipFile(BytesIO(response.content), "r") as zipfile:
            self.assertIsNone(zipfile.testzip())

    def assert_svg(self, response):
        """Check whether response is a SVG image."""
        # Check response status code
        self.assertEqual(response.status_code, 200)
        dom = minidom.parseString(response.content)
        self.assertEqual(dom.firstChild.nodeName, "svg")

    def assert_backend(self, expected_translated, language="cs"):
        """Check that backend has correct data."""
        translation = self.get_translation(language)
        translation.commit_pending("test", None)
        store = translation.component.file_format_cls(translation.get_filename(), None)
        messages = set()
        translated = 0

        for unit in store.content_units:
            id_hash = unit.id_hash
            self.assertFalse(
                id_hash in messages, "Duplicate string in in backend file!"
            )
            if unit.is_translated():
                translated += 1

        self.assertEqual(
            translated,
            expected_translated,
            "Did not found expected number of translations ({0} != {1}).".format(
                translated, expected_translated
            ),
        )

    def log_as_jane(self):
        self.client.login(username="jane", password="testpassword")


class FixtureTestCase(ViewTestCase):
    @classmethod
    def setUpTestData(cls):
        """Manually load fixture."""
        # Ensure there are no Language objects, we add
        # them in defined order in fixture
        Language.objects.all().delete()

        # Stolen from setUpClass, we just need to do it
        # after transaction checkpoint and deleting languages
        for db_name in cls._databases_names(include_mirrors=False):
            call_command(
                "loaddata", "simple-project.json", verbosity=0, database=db_name
            )
        # Apply group project/language automation
        for group in Group.objects.iterator():
            group.save()

        super().setUpTestData()

    def clone_test_repos(self):
        return

    def create_project(self):
        project = Project.objects.all()[0]
        setup_project_groups(self, project)
        return project

    def create_component(self):
        component = self.create_project().component_set.all()[0]
        component.create_path()
        return component


class TranslationManipulationTest(ViewTestCase):
    def setUp(self):
        super().setUp()
        self.component.new_lang = "add"
        self.component.save()

    def create_component(self):
        return self.create_po_new_base()

    def test_model_add(self):
        self.assertTrue(
            self.component.add_new_language(
                Language.objects.get(code="af"), self.get_request()
            )
        )
        self.assertTrue(
            self.component.translation_set.filter(language_code="af").exists()
        )

    def test_model_add_duplicate(self):
        request = self.get_request()
        self.assertFalse(get_messages(request))
        self.assertTrue(
            self.component.add_new_language(Language.objects.get(code="de"), request)
        )
        self.assertTrue(get_messages(request))

    def test_model_add_disabled(self):
        self.component.new_lang = "contact"
        self.component.save()
        self.assertFalse(
            self.component.add_new_language(
                Language.objects.get(code="af"), self.get_request()
            )
        )

    def test_model_add_superuser(self):
        self.component.new_lang = "contact"
        self.component.save()
        self.user.is_superuser = True
        self.user.save()
        self.assertTrue(
            self.component.add_new_language(
                Language.objects.get(code="af"), self.get_request()
            )
        )

    def test_remove(self):
        translation = self.component.translation_set.get(language_code="de")
        translation.remove(self.user)
        # Force scanning of the repository
        self.component.create_translations()
        self.assertFalse(
            self.component.translation_set.filter(language_code="de").exists()
        )


class NewLangTest(ViewTestCase):
    expected_lang_code = "pt_BR"

    def create_component(self):
        return self.create_po_new_base(new_lang="add")

    def test_no_permission(self):
        # Remove permission to add translations
        Role.objects.get(name="Power user").permissions.remove(
            Permission.objects.get(codename="translation.add")
        )

        # Test there is no add form
        response = self.client.get(reverse("component", kwargs=self.kw_component))
        self.assertContains(response, "Start new translation")
        self.assertContains(response, "permission to start a new translation")

        # Test adding fails
        response = self.client.post(
            reverse("new-language", kwargs=self.kw_component), {"lang": "af"}
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            self.component.translation_set.filter(language__code="af").exists()
        )

    def test_none(self):
        self.component.new_lang = "none"
        self.component.save()

        response = self.client.get(reverse("component", kwargs=self.kw_component))
        self.assertNotContains(response, "Start new translation")

    def test_url(self):
        self.component.new_lang = "url"
        self.component.save()
        self.project.instructions = "http://example.com/instructions"
        self.project.save()

        response = self.client.get(reverse("component", kwargs=self.kw_component))
        self.assertContains(response, "Start new translation")
        self.assertContains(response, "http://example.com/instructions")

    def test_contact(self):
        # Make admin to receive notifications
        self.project.add_user(self.anotheruser, "@Administration")

        self.component.new_lang = "contact"
        self.component.save()

        response = self.client.get(reverse("component", kwargs=self.kw_component))
        self.assertContains(response, "Start new translation")
        self.assertContains(response, "/new-lang/")

        response = self.client.post(
            reverse("new-language", kwargs=self.kw_component), {"lang": "af"}
        )
        self.assertRedirects(response, self.component.get_absolute_url())

        # Verify mail
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(
            mail.outbox[0].subject, "[Weblate] New language request in Test/Test"
        )

    def test_add(self):
        # Make admin to receive notifications
        self.project.add_user(self.anotheruser, "@Administration")

        self.assertFalse(
            self.component.translation_set.filter(language__code="af").exists()
        )

        response = self.client.get(reverse("component", kwargs=self.kw_component))
        self.assertContains(response, "Start new translation")
        self.assertContains(response, "/new-lang/")

        lang = {"lang": "af"}
        response = self.client.post(
            reverse("new-language", kwargs=self.kw_component), lang
        )
        lang.update(self.kw_component)
        self.assertRedirects(response, reverse("translation", kwargs=lang))
        self.assertTrue(
            self.component.translation_set.filter(language__code="af").exists()
        )

        # Verify mail
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(
            mail.outbox[0].subject, "[Weblate] New language added to Test/Test"
        )

        # Not selected language
        response = self.client.post(
            reverse("new-language", kwargs=self.kw_component), {"lang": ""}, follow=True
        )
        self.assertContains(response, "Please fix errors in the form")

        # Existing language
        response = self.client.post(
            reverse("new-language", kwargs=self.kw_component),
            {"lang": "af"},
            follow=True,
        )
        self.assertContains(response, "Please fix errors in the form")

    def test_add_owner(self):
        self.component.project.add_user(self.user, "@Administration")
        # None chosen
        response = self.client.post(
            reverse("new-language", kwargs=self.kw_component), follow=True
        )
        self.assertContains(response, "Please fix errors in the form")
        # One chosen
        response = self.client.post(
            reverse("new-language", kwargs=self.kw_component),
            {"lang": "af"},
            follow=True,
        )
        self.assertNotContains(response, "Please fix errors in the form")
        # More chosen
        response = self.client.post(
            reverse("new-language", kwargs=self.kw_component),
            {"lang": ["nl", "fr", "uk"]},
            follow=True,
        )
        self.assertNotContains(response, "Please fix errors in the form")
        self.assertEqual(
            self.component.translation_set.filter(
                language__code__in=("af", "nl", "fr", "uk")
            ).count(),
            4,
        )

    def test_add_rejected(self):
        self.component.project.add_user(self.user, "@Administration")
        self.component.language_regex = "^cs$"
        self.component.save()
        # One chosen
        response = self.client.post(
            reverse("new-language", kwargs=self.kw_component),
            {"lang": "af"},
            follow=True,
        )
        self.assertContains(
            response, "The given language is filtered by the language filter."
        )

    def test_add_code(self):
        def perform(style, code, expected):
            self.component.language_code_style = style
            self.component.save()

            self.assertFalse(
                self.component.translation_set.filter(language__code=code).exists(),
                f"Translation with code {code} already exists",
            )
            self.client.post(
                reverse("new-language", kwargs=self.kw_component), {"lang": code}
            )
            translation = self.component.translation_set.get(language__code=code)
            self.assertEqual(translation.language_code, expected)
            translation.remove(self.user)

        perform("", "pt_BR", self.expected_lang_code)
        perform("posix", "pt_BR", "pt_BR")
        perform("posix_long", "ms", "ms_MY")
        perform("bcp", "pt_BR", "pt-BR")
        perform("bcp_long", "ms", "ms-MY")
        perform("android", "pt_BR", "pt-rBR")


class AndroidNewLangTest(NewLangTest):
    expected_lang_code = "pt-rBR"

    def create_component(self):
        return self.create_android(new_lang="add")


class BasicViewTest(ViewTestCase):
    def test_view_project(self):
        response = self.client.get(reverse("project", kwargs=self.kw_project))
        self.assertContains(response, "test/test")
        self.assertNotContains(response, "Spanish")

    def test_view_project_ghost(self):
        self.user.profile.languages.add(Language.objects.get(code="es"))
        response = self.client.get(reverse("project", kwargs=self.kw_project))
        self.assertContains(response, "Spanish")

    def test_view_component(self):
        response = self.client.get(reverse("component", kwargs=self.kw_component))
        self.assertContains(response, "Test/Test")
        self.assertNotContains(response, "Spanish")

    def test_view_component_ghost(self):
        self.user.profile.languages.add(Language.objects.get(code="es"))
        response = self.client.get(reverse("component", kwargs=self.kw_component))
        self.assertContains(response, "Spanish")

    def test_view_component_guide(self):
        response = self.client.get(reverse("guide", kwargs=self.kw_component))
        self.assertContains(response, "Test/Test")

    def test_view_translation(self):
        response = self.client.get(reverse("translation", kwargs=self.kw_translation))
        self.assertContains(response, "Test/Test")

    def test_view_translation_others(self):
        other = Component.objects.create(
            name="RESX component",
            slug="resx",
            project=self.project,
            repo="weblate://test/test",
            file_format="resx",
            filemask="resx/*.resx",
            template="resx/en.resx",
            new_lang="add",
        )
        # Existing translation
        response = self.client.get(reverse("translation", kwargs=self.kw_translation))
        self.assertContains(response, other.name)
        # Ghost translation
        kwargs = {}
        kwargs.update(self.kw_translation)
        kwargs["lang"] = "it"
        response = self.client.get(reverse("translation", kwargs=kwargs))
        self.assertContains(response, other.name)

    def test_view_redirect(self):
        """Test case insentivite lookups and aliases in middleware."""
        # Non existing fails with 404
        kwargs = {"project": "invalid"}
        response = self.client.get(reverse("project", kwargs=kwargs))
        self.assertEquals(response.status_code, 404)

        # Different casing should redirect
        kwargs["project"] = self.project.slug.upper()
        response = self.client.get(reverse("project", kwargs=kwargs))
        self.assertRedirects(
            response, reverse("project", kwargs=self.kw_project), status_code=301
        )

        # Non existing fails with 404
        kwargs["component"] = "invalid"
        response = self.client.get(reverse("component", kwargs=kwargs))
        self.assertEquals(response.status_code, 404)

        # Different casing should redirect
        kwargs["component"] = self.component.slug.upper()
        response = self.client.get(reverse("component", kwargs=kwargs))
        self.assertRedirects(
            response, reverse("component", kwargs=self.kw_component), status_code=301
        )

        # Non existing fails with 404
        kwargs["lang"] = "cs-DE"
        response = self.client.get(reverse("translation", kwargs=kwargs))
        self.assertEqual(response.status_code, 404)

        # Aliased language should redirect
        kwargs["lang"] = "czech"
        response = self.client.get(reverse("translation", kwargs=kwargs))
        self.assertRedirects(
            response,
            reverse("translation", kwargs=self.kw_translation),
            status_code=301,
        )

    def test_view_unit(self):
        unit = self.get_unit()
        response = self.client.get(unit.get_absolute_url())
        self.assertContains(response, "Hello, world!")

    def test_view_component_list(self):
        clist = ComponentList.objects.create(name="TestCL", slug="testcl")
        clist.components.add(self.component)
        response = self.client.get(reverse("component-list", kwargs={"name": "testcl"}))
        self.assertContains(response, "TestCL")
        self.assertContains(response, self.component.name)


class BasicMonolingualViewTest(BasicViewTest):
    def create_component(self):
        return self.create_po_mono()


class DashboardTest(ViewTestCase):
    """Test for home/index view."""

    def setUp(self):
        super().setUp()
        self.user.profile.languages.add(Language.objects.get(code="cs"))

    def test_view_home_anonymous(self):
        self.client.logout()
        response = self.client.get(reverse("home"))
        self.assertContains(response, "Browse 1 project")

    def test_view_home(self):
        response = self.client.get(reverse("home"))
        self.assertContains(response, "test/test")

    def test_view_projects(self):
        response = self.client.get(reverse("projects"))
        self.assertContains(response, "Test")

    def test_home_with_announcement(self):
        msg = Announcement(message="test_message")
        msg.save()

        response = self.client.get(reverse("home"))
        self.assertContains(response, "announcement")
        self.assertContains(response, "test_message")

    def test_home_without_announcement(self):
        response = self.client.get(reverse("home"))
        self.assertNotContains(response, "announcement")

    def test_component_list(self):
        clist = ComponentList.objects.create(name="TestCL", slug="testcl")
        clist.components.add(self.component)

        response = self.client.get(reverse("home"))
        self.assertContains(response, "TestCL")
        self.assertContains(
            response, reverse("component-list", kwargs={"name": "testcl"})
        )
        self.assertEqual(len(response.context["componentlists"]), 1)

    def test_component_list_ghost(self):
        clist = ComponentList.objects.create(name="TestCL", slug="testcl")
        clist.components.add(self.component)

        self.user.profile.languages.add(Language.objects.get(code="es"))

        response = self.client.get(reverse("home"))

        self.assertContains(response, "Spanish")

    def test_user_component_list(self):
        clist = ComponentList.objects.create(name="TestCL", slug="testcl")
        clist.components.add(self.component)

        self.user.profile.dashboard_view = Profile.DASHBOARD_COMPONENT_LIST
        self.user.profile.dashboard_component_list = clist
        self.user.profile.save()

        response = self.client.get(reverse("home"))
        self.assertContains(response, "TestCL")
        self.assertEqual(response.context["active_tab_slug"], "list-testcl")

    def test_subscriptions(self):
        # no subscribed projects at first
        response = self.client.get(reverse("home"))
        self.assertFalse(len(response.context["watched_projects"]))

        # subscribe a project
        self.user.profile.watched.add(self.project)
        response = self.client.get(reverse("home"))
        self.assertEqual(len(response.context["watched_projects"]), 1)

    def test_language_filters(self):
        # check language filters
        response = self.client.get(reverse("home"))
        self.assertFalse(response.context["usersubscriptions"])

        # add a language
        response = self.client.get(reverse("home"))
        self.assertFalse(response.context["usersubscriptions"])

        # add a subscription
        self.user.profile.watched.add(self.project)
        response = self.client.get(reverse("home"))
        self.assertEqual(len(response.context["usersubscriptions"]), 1)

    def test_user_nolang(self):
        self.user.profile.languages.clear()
        # This picks up random language
        self.client.get(reverse("home"), HTTP_ACCEPT_LANGUAGE="en")
        self.client.get(reverse("home"))

        # Pick language from request
        response = self.client.get(reverse("home"), HTTP_ACCEPT_LANGUAGE="cs")
        self.assertTrue(response.context["suggestions"])

    def test_user_hide_completed(self):
        self.user.profile.hide_completed = True
        self.user.profile.save()

        response = self.client.get(reverse("home"))
        self.assertContains(response, "test/test")

    @override_settings(SINGLE_PROJECT=True)
    def test_single_project(self):
        response = self.client.get(reverse("home"))
        self.assertRedirects(response, reverse("component", kwargs=self.kw_component))

    @override_settings(SINGLE_PROJECT="test")
    def test_single_project_slug(self):
        response = self.client.get(reverse("home"))
        self.assertRedirects(response, reverse("project", kwargs=self.kw_project))


class SourceStringsTest(ViewTestCase):
    def test_edit_priority(self):
        # Need extra power
        self.user.is_superuser = True
        self.user.save()

        source = self.get_unit().source_info
        response = self.client.post(
            reverse("edit_context", kwargs={"pk": source.pk}),
            {"extra_flags": "priority:60"},
        )
        self.assertRedirects(response, source.get_absolute_url())

        unit = self.get_unit()
        self.assertEqual(unit.priority, 60)

    def test_edit_readonly(self):
        # Need extra power
        self.user.is_superuser = True
        self.user.save()

        unit = self.get_unit()
        old_state = unit.state
        source = unit.source_info
        response = self.client.post(
            reverse("edit_context", kwargs={"pk": source.pk}),
            {"extra_flags": "read-only"},
        )
        self.assertRedirects(response, source.get_absolute_url())

        unit = self.get_unit()
        self.assertTrue(unit.readonly)
        self.assertNotEqual(unit.state, old_state)

        response = self.client.post(
            reverse("edit_context", kwargs={"pk": source.pk}), {"extra_flags": ""}
        )
        self.assertRedirects(response, source.get_absolute_url())

        unit = self.get_unit()
        self.assertFalse(unit.readonly)
        self.assertEqual(unit.state, old_state)

    def test_edit_context(self):
        # Need extra power
        self.user.is_superuser = True
        self.user.save()

        source = self.get_unit().source_info
        response = self.client.post(
            reverse("edit_context", kwargs={"pk": source.pk}),
            {"explanation": "Extra context"},
        )
        self.assertRedirects(response, source.get_absolute_url())

        unit = self.get_unit()
        self.assertEqual(unit.context, "")
        self.assertEqual(unit.explanation, "Extra context")

    def test_edit_check_flags(self):
        # Need extra power
        self.user.is_superuser = True
        self.user.save()

        source = self.get_unit().source_info
        response = self.client.post(
            reverse("edit_context", kwargs={"pk": source.pk}),
            {"extra_flags": "ignore-same"},
        )
        self.assertRedirects(response, source.get_absolute_url())

        unit = self.get_unit()
        self.assertEqual(unit.extra_flags, "ignore-same")

    def test_view_source(self):
        kwargs = {"lang": "en"}
        kwargs.update(self.kw_component)
        response = self.client.get(reverse("translation", kwargs=kwargs))
        self.assertContains(response, "Test/Test")

    def test_matrix(self):
        response = self.client.get(reverse("matrix", kwargs=self.kw_component))
        self.assertContains(response, "Czech")

    def test_matrix_load(self):
        response = self.client.get(
            reverse("matrix-load", kwargs=self.kw_component) + "?offset=0&lang=cs"
        )
        self.assertContains(response, 'lang="cs"')
