# coding=utf8
from contextlib import contextmanager
import os
import unittest
try:
    from unittest import mock
except ImportError:  # Python 2 fallback.
    import mock

from django.core.urlresolvers import reverse
import django.test
from django.test.utils import override_settings

import pydocusign
import pydocusign.test
import django_docusign

from django_docusign_demo import models, views


here = os.path.abspath(os.path.dirname(__file__))
fixtures_dir = os.path.join(here, 'fixtures')


@contextmanager
def temporary_env():
    former_environ = dict(os.environ)  # Backup.
    try:
        yield os.environ
    finally:
        for key, value in former_environ.items():
            os.environ[key] = value  # Restore.


class DocuSignSettingsTestCase(unittest.TestCase):
    """Tests around ``docusign_settings()`` utility function."""
    def test_easy(self):
        """django_docusign.views.docusign_setting reads conf in session."""
        with temporary_env():
            request = mock.Mock()
            request.session = {'root_url': 'URL',
                               'username': 'NAME',
                               'password': 'PASS',
                               'not related to docusign': 'FOO'}
            os.environ['DOCUSIGN_PASSWORD'] = 'DEFAULT_PASS'
            os.environ['DOCUSIGN_INTEGRATOR_KEY'] = 'INTEGRATOR'
            self.assertEqual(views.docusign_settings(request),
                             {'root_url': 'URL',
                              'username': 'NAME',
                              'password': 'PASS'})


class SettingsViewTestCase(django.test.TestCase):
    """Tests around ``SettingsView``."""
    def test_session_settings(self):
        """SettingsView actually stores settings in session."""
        with temporary_env():
            # 1. Make sure we are using settings from environment.
            os.environ['DOCUSIGN_ROOT_URL'] = 'ENV'
            os.environ['DOCUSIGN_PASSWORD'] = 'ENV'
            home_url = reverse('home')
            response = self.client.get(home_url)
            request = response._request
            self.assertTrue('root_url' not in request.session)
            # 2. POST settings, make sure we use settings in session.
            settings_url = reverse('settings')
            data = {
                'username': 'NAME',
                'password': 'PASS',
                'integrator_key': 'INTEGRATOR',
            }
            response = self.client.post(settings_url, data, follow=True)
            self.assertRedirects(response, home_url)
            request = response._request
            self.assertTrue('root_url' not in request.session)
            self.assertEqual(request.session['username'], 'NAME')
            self.assertEqual(request.session['password'], 'PASS')
            self.assertEqual(request.session['integrator_key'], 'INTEGRATOR')


class SignatureFunctionalTestCase(django.test.TestCase):
    """Functional test suite for signature workflow."""
    #: Class-level signature instance, in order to reduce API calls.
    _signature = None

    @property
    def signature(self):
        """Get or create signature instance."""
        if self._signature is None:
            self._signature = self.create_signature()
        return self._signature

    def create_signature(self):
        url = reverse('create_signature')
        with open(os.path.join(fixtures_dir, 'test.pdf')) as document_file:
            data = {
                'signers-TOTAL_FORMS': u'2',
                'signers-INITIAL_FORMS': u'0',
                'signers-MAX_NUM_FORMS': u'1000',
                'signers-0-name': u'John Accentué',
                'signers-0-email': u'john@example.com',
                'signers-1-name': u'Paul Doe',
                'signers-1-email': u'paul@example.com',
                'document': document_file,
                'title': u'A very simple PDF document',
                'callback_url': u'http://tech.novapost.fr',
            }
            response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('home'))
        return models.Signature.objects.order_by('-pk').first()

    def test_form_valid(self):
        """Can create a signature using 'create_signature' URL."""
        self.assertTrue(self.signature.signature_backend_id)

    def test_signer_view(self):
        """Signer view redirects to DocuSign."""
        url = reverse('anysign:signer',
                      args=[self.signature.signers.all()[0].pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 301)
        self.assertTrue(
            response['Location'].startswith('https://demo.docusign.net'))

    def send_signature_callback(self, data):
        url = reverse('anysign:signature_callback')
        request_body = pydocusign.test.generate_notification_callback_body(
            data=data)
        response = self.client.post(
            url,
            content_type='text/xml',
            data=request_body,
        )
        self.assertEqual(response.status_code, 200)
        return response

    def test_signature_callback(self):
        """Callback view handles DocuSign's 'sent' status."""
        signature = self.create_signature()
        self.assertEqual(signature.signers.get(signing_order=1).status,
                         'draft')
        self.assertEqual(signature.signers.get(signing_order=2).status,
                         'draft')
        signers = signature.signers.all().order_by('signing_order')
        data = {
            "RecipientStatuses": [
                {
                    "Email": signer.email,
                    "UserName": signer.full_name,
                    "ClientUserId": signer.pk,
                    "Status": pydocusign.Recipient.STATUS_SENT,
                    "Sent": "2014-10-06T01:10:01.000012",
                } for signer in signers

            ],
            "EnvelopeId": signature.signature_backend_id,
            "Subject": signature.document_title,
            "UserName": "Bob",
            "Created": "2014-10-06T01:10:00.000012",
            "Sent": "2014-10-06T01:10:01.000012",
        }
        # First, we receive "sent" callback.
        self.send_signature_callback(data)
        signature = models.Signature.objects.get(pk=signature.pk)
        self.assertEqual(signature.signers.get(signing_order=1).status,
                         'sent')
        self.assertEqual(signature.signers.get(signing_order=2).status,
                         'sent')
        # Then, envelope is "delivered" to recipients.
        data['RecipientStatuses'][0]['Status'] = "Delivered"
        data['RecipientStatuses'][0]['Delivered'] = "2014-10-06" \
                                                    "T01:10:02.000012"
        self.send_signature_callback(data)
        signature = models.Signature.objects.get(pk=signature.pk)
        self.assertEqual(signature.signers.get(signing_order=1).status,
                         'delivered')
        self.assertEqual(signature.signers.get(signing_order=2).status,
                         'sent')
        # A recipient signs.
        data['RecipientStatuses'][0]['Status'] = "Signed"
        data['RecipientStatuses'][0]['Signed'] = "2014-10-06" \
                                                 "T01:10:03.000012"
        self.send_signature_callback(data)
        signature = models.Signature.objects.get(pk=signature.pk)
        self.assertEqual(signature.status, 'sent')
        self.assertEqual(signature.signers.get(signing_order=1).status,
                         'completed')
        self.assertEqual(signature.signers.get(signing_order=2).status,
                         'sent')
        # Last recipient signs.
        data['RecipientStatuses'][1]['Status'] = "Signed"
        data['RecipientStatuses'][1]['Signed'] = "2014-10-06" \
                                                 "T01:10:04.000012"
        data['Status'] = "Completed"
        data['Completed'] = "2014-10-06T01:10:04.000012"
        self.send_signature_callback(data)
        signature = models.Signature.objects.get(pk=signature.pk)
        self.assertEqual(signature.status, 'completed')
        self.assertEqual(signature.signers.get(signing_order=1).status,
                         'completed')
        self.assertEqual(signature.signers.get(signing_order=2).status,
                         'completed')
        # But we could also have received "decline" callback.
        del data['Completed']
        del data['RecipientStatuses'][1]['Signed']
        data['RecipientStatuses'][1]['Status'] = "Declined"
        data['RecipientStatuses'][1]['Declined'] = "2014-10-06" \
                                                   "T01:10:05.000012"
        data['Status'] = "Declined"
        data['Declined'] = "2014-10-06T01:10:05.000012"
        self.send_signature_callback(data)
        signature = models.Signature.objects.get(pk=signature.pk)
        self.assertEqual(signature.status, 'declined')
        self.assertEqual(signature.signers.get(signing_order=1).status,
                         'completed')
        self.assertEqual(signature.signers.get(signing_order=2).status,
                         'declined')
        self.assertEqual(signature.signers.get(signing_order=2).status_details,
                         u'')
        # Make sure we handle optional "decline reason" as well.
        data['RecipientStatuses'][1]['DeclineReason'] = "Do not sign a test!"
        self.send_signature_callback(data)
        signature = models.Signature.objects.get(pk=signature.pk)
        self.assertEqual(signature.status, 'declined')
        self.assertEqual(signature.signers.get(signing_order=2).status_details,
                         u'Do not sign a test!')


class SignatureTemplateFunctionalTestCase(SignatureFunctionalTestCase):
    """Functional test suite for signature workflow."""

    def create_signature(self):
        url = reverse('create_signature_template')
        response = self.client.get(url)
        # get template_id from initial data
        # must be defined in environment variable DOCUSIGN_TEST_TEMPLATE_ID
        template_id = response.context['form'].initial['template_id']
        data = {
            'signers-TOTAL_FORMS': u'2',
            'signers-INITIAL_FORMS': u'0',
            'signers-MAX_NUM_FORMS': u'1000',
            'signers-0-name': u'John Accentué',
            'signers-0-email': u'john@example.com',
            'signers-1-name': u'Paul Doe',
            'signers-1-email': u'paul@example.com',
            'template_id': template_id,
            'title': u'A very simple PDF document',
            'callback_url': u'http://tech.novapost.fr',
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('home'))
        return models.Signature.objects.order_by('-pk').first()


def noop(*args, **kwargs):
    """Noop client factory."""
    return 'noop'


class DocuSignBackendTestCase(unittest.TestCase):
    """Tests around :class:`~django_docusign.backend.DocuSignBackend`."""
    def test_setup_explicit(self):
        """DocuSignBackend() proxies options to DocuSignClient()."""
        explicit_options = {
            'root_url': 'http://example.com',
            'username': 'johndoe',
            'password': 'secret',
            'integrator_key': 'very-secret',
            'account_id': 'some-uuid',
            'app_token': 'some-token',
            'timeout': 300.0,
        }
        backend = django_docusign.DocuSignBackend(**explicit_options)
        for key, value in explicit_options.items():
            self.assertEqual(getattr(backend.docusign_client, key), value)

    def test_setup_settings(self):
        """DocuSignBackend uses settings.DOCUSIGN_*."""
        overrides = {
            'DOCUSIGN_ROOT_URL': 'http://example.com',
            'DOCUSIGN_USERNAME': 'pierre paul ou jacques',
            'DOCUSIGN_PASSWORD': 'not-a-secret',
            'DOCUSIGN_INTEGRATOR_KEY': 'not-an-integator-key',
            'DOCUSIGN_ACCOUNT_ID': 'not-an-uuid',
            'DOCUSIGN_APP_TOKEN': 'not-a-token',
            'DOCUSIGN_TIMEOUT': 200.123,
        }
        with override_settings(**overrides):
            backend = django_docusign.DocuSignBackend()
        for key, value in overrides.items():
            key = key.lower()[len('DOCUSIGN_'):]
            self.assertEqual(getattr(backend.docusign_client, key), value)

    def test_setup_priority(self):
        """Explicit arguments have priority over settings."""
        explicit_options = {
            'root_url': 'http://example.com',
            'username': 'johndoe',
            'password': 'secret',
            'integrator_key': 'very-secret',
            'account_id': 'some-uuid',
            'app_token': 'some-token',
            'timeout': 300.0,
        }
        overrides = {
            'DOCUSIGN_ROOT_URL': 'http://another.example.com',
            'DOCUSIGN_USERNAME': 'pierre paul ou jacques',
            'DOCUSIGN_PASSWORD': 'not-a-secret',
            'DOCUSIGN_INTEGRATOR_KEY': 'not-an-integator-key',
            'DOCUSIGN_ACCOUNT_ID': 'not-an-uuid',
            'DOCUSIGN_APP_TOKEN': 'not-a-token',
            'DOCUSIGN_TIMEOUT': 200.123,
        }
        with override_settings(**overrides):
            backend = django_docusign.DocuSignBackend(**explicit_options)
        for key, value in explicit_options.items():
            self.assertEqual(getattr(backend.docusign_client, key), value)
