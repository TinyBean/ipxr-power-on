"""Offline contract tests: all HTTP is intercepted; no real power operations."""
import contextlib
from http.cookiejar import MozillaCookieJar
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import requests
from requests.cookies import create_cookie
import ipxr_power_on as app

LOGIN = '<meta charset="utf-8"><form id="email-login-form"><input name="token" value="dynamic-test-token"></form>'
DETAIL = '<div id="serviceConsoleBoot" data-host-id="10328" data-endpoint="/service-console"></div>'
ACCEPTED = {"status": 200, "data": {"status": "accepted", "request_no": "test-request-1"}}


def reply(body, status=200, headers=None):
    response = requests.Response()
    response.status_code = status
    response.encoding = 'utf-8'
    response.headers.update(headers or {})
    if isinstance(body, (dict, list)):
        response._content = json.dumps(body, ensure_ascii=False).encode('utf-8')
        response.headers['Content-Type'] = 'application/json; charset=utf-8'
    else:
        response._content = body.encode('utf-8')
        response.headers['Content-Type'] = 'text/html; charset=utf-8'
    return response


def fresh_login():
    return [('GET', '/login', reply(LOGIN)),
            ('POST', '/login', reply('', 302, {'Location': '/clientarea'})),
            ('GET', '/servicedetail', reply(DETAIL))]


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cookie = Path(self.temp.name) / 'cookies.txt'
        self.calls = []

    def run_flow(self, steps, *, cached=False, credentials=True, query_status=False, **kwargs):
        self.cookie.unlink(missing_ok=True)
        if cached:
            jar = MozillaCookieJar(str(self.cookie))
            jar.set_cookie(create_cookie('PHPSESSID', 'test-cookie-secret', domain='www.ipxr.cn', secure=True))
            jar.save(ignore_discard=True)
        pending = list(steps)
        self.calls = []

        def transport(session, request, **options):
            self.calls.append((request, options))
            self.assertTrue(pending, 'Unexpected HTTP request (possible polling/retry)')
            method, path, outcome = pending.pop(0)
            self.assertEqual((request.method, urlsplit(request.url).path), (method, path))
            self.assertFalse(options.get('allow_redirects'))
            self.assertEqual(session.get_adapter(request.url).max_retries.total, 0)
            if isinstance(outcome, BaseException):
                raise outcome
            if request.method == 'POST' and path == '/login' and outcome.status_code == 302:
                session.cookies.set_cookie(create_cookie('PHPSESSID', 'test-cookie-secret', domain='www.ipxr.cn', secure=True))
            outcome.url = request.url
            outcome.request = request
            return outcome

        defaults = {'cookie_file': self.cookie}
        if credentials:
            defaults.update(username='test@example.invalid', password='private-test-password')
        defaults.update(kwargs)
        stdout = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), patch.object(requests.Session, 'send', autospec=True, side_effect=transport), contextlib.redirect_stdout(stdout):
            operation = app.power_status if query_status else app.power_on
            result = operation(**defaults)
        self.assertEqual(stdout.getvalue(), '')
        self.assertEqual(pending, [])
        actions = [r for r, _ in self.calls if urlsplit(r.url).path == '/service-console/action']
        self.assertLessEqual(len(actions), 1)
        endpoint = '/provision/default' if query_status else '/service-console/action'
        if query_status:
            self.assertEqual(actions, [])
            self.assertIsNone(result.idempotency_key)
            self.assertIsNone(result.request_no)
        for request, _ in self.calls:
            self.assertIn(urlsplit(request.url).path, ['/login', '/servicedetail', endpoint])
        serialized = json.dumps(result.to_dict())
        for secret in ['private-test-password', 'test-cookie-secret', 'dynamic-test-token']:
            self.assertNotIn(secret, serialized)
        return result

    def test_login_and_exact_start_contract(self):
        result = self.run_flow(fresh_login() + [('POST', '/service-console/action', reply(ACCEPTED))])
        self.assertEqual((result.status, result.exit_code), ('accepted', 0))
        login = self.calls[1][0]
        fields = parse_qs(login.body)
        self.assertEqual(fields, {'token': ['dynamic-test-token'], 'email': ['test@example.invalid'], 'password': ['private-test-password']})
        request = self.calls[-1][0]
        body = json.loads(request.body)
        self.assertEqual(body, {'id': 10328, 'host_id': 10328, 'action': 'on', 'params': {}, 'idempotency_key': result.idempotency_key})
        self.assertEqual(result.request_no, 'test-request-1')
        self.assertEqual(request.headers['Origin'], app.BASE_URL)
        self.assertIn('application/json', request.headers['Content-Type'])
        jar = MozillaCookieJar(str(self.cookie))
        jar.load(ignore_discard=True)
        self.assertEqual(len(jar), 1)
        if os.name == 'posix':
            self.assertEqual(self.cookie.stat().st_mode & 0o777, 0o600)

    def test_cookie_only_reuse(self):
        result = self.run_flow([('GET', '/servicedetail', reply(DETAIL)), ('POST', '/service-console/action', reply(ACCEPTED))], cached=True, credentials=False)
        self.assertEqual(result.exit_code, 0)
        self.assertIn('PHPSESSID=test-cookie-secret', self.calls[-1][0].headers['Cookie'])

    def test_expired_session_reauthenticates_before_submission(self):
        result = self.run_flow([('GET', '/servicedetail', reply(LOGIN))] + fresh_login() + [('POST', '/service-console/action', reply(ACCEPTED))], cached=True)
        self.assertEqual(result.exit_code, 0)

    def test_expired_session_without_credentials(self):
        result = self.run_flow([('GET', '/servicedetail', reply(LOGIN))], cached=True, credentials=False)
        self.assertEqual((result.status, result.exit_code), ('auth_required', 2))

    def test_bad_password(self):
        result = self.run_flow([('GET', '/login', reply(LOGIN)), ('POST', '/login', reply('<meta charset="utf-8">邮箱或密码错误')), ('GET', '/servicedetail', reply(LOGIN))])
        self.assertEqual((result.status, result.exit_code), ('failed', 1))

    def test_captcha(self):
        result = self.run_flow([('GET', '/login', reply(LOGIN)), ('POST', '/login', reply('<meta charset="utf-8">行为验证失败', 403))])
        self.assertEqual((result.status, result.exit_code), ('auth_required', 2))

    def test_missing_credentials_no_http(self):
        self.assertEqual(self.run_flow([], credentials=False).exit_code, 2)

    def test_task_outcomes(self):
        cases = [
            ({'status': 200, 'data': {'status': 'success'}}, 'accepted', 0),
            ({'status': 202, 'data': {'status': 'running'}}, 'accepted', 0),
            ({'status': 200, 'data': {'status': 'failed'}}, 'failed', 1),
            ({'status': 200, 'data': {'status': 'unknown'}}, 'unknown', 3),
            ({'status': 200, 'data': {'status': 'manual_review'}}, 'unknown', 3),
            ({'status': 400, 'error_code': 'ALREADY_ON'}, 'already_on', 0),
            ({'status': 400, 'msg': '服务器已开机'}, 'already_on', 0),
            ({'status': 400, 'msg': 'private-test-password'}, 'failed', 1),
            ({'status': 403, 'error_code': 'SECOND_VERIFY_REQUIRED'}, 'auth_required', 2),
            ({'status': 401}, 'auth_required', 2),
            ({'status': 429}, 'failed', 1),
            ({'status': 200, 'data': {'status': 'new-unrecognized-state'}}, 'unknown', 3),
            ({}, 'unknown', 3), ([], 'unknown', 3),
        ]
        for payload, status, code in cases:
            with self.subTest(payload=payload):
                result = self.run_flow(fresh_login() + [('POST', '/service-console/action', reply(payload))])
                self.assertEqual((result.status, result.exit_code), (status, code))

    def test_action_timeout_connection_error_and_interrupt_are_never_retried(self):
        for error in [requests.Timeout('private-test-password'), requests.ConnectionError('test-cookie-secret'), KeyboardInterrupt()]:
            with self.subTest(error=type(error).__name__):
                result = self.run_flow(fresh_login() + [('POST', '/service-console/action', error)])
                self.assertEqual((result.status, result.exit_code), ('unknown', 3))
                self.assertIsNotNone(result.idempotency_key)

    def test_action_redirects_invalid_json_and_server_errors(self):
        for response in [reply('', 307, {'Location': app.ACTION_URL}), reply('', 302, {'Location': '/login'}), reply('bad gateway', 502), reply({'status': 500}, 500), reply('not json')]:
            with self.subTest(http=response.status_code):
                self.assertEqual(self.run_flow(fresh_login() + [('POST', '/service-console/action', response)]).exit_code, 3)

    def test_action_login_html_and_401(self):
        for response in [reply(LOGIN), reply('unauthorized', 401)]:
            self.assertEqual(self.run_flow(fresh_login() + [('POST', '/service-console/action', response)]).exit_code, 2)

    def test_pre_submission_timeout_is_failure(self):
        self.assertEqual(self.run_flow([('GET', '/login', requests.Timeout())]).exit_code, 1)

    def test_same_origin_get_redirect_allowed(self):
        steps = [('GET', '/servicedetail', reply('', 302, {'Location': '/login'})), ('GET', '/login', reply(LOGIN))]
        self.assertEqual(self.run_flow(steps, cached=True, credentials=False).exit_code, 2)

    def test_cross_origin_redirect_never_followed(self):
        self.assertEqual(self.run_flow([('GET', '/login', reply('', 302, {'Location': 'https://elsewhere.invalid/login'}))]).exit_code, 1)

    def test_wrong_service_page_never_submits(self):
        steps = fresh_login()
        steps[-1] = ('GET', '/servicedetail', reply(DETAIL.replace('10328', '99999')))
        self.assertEqual(self.run_flow(steps).exit_code, 1)

    def test_cookie_write_failure_before_and_after_submission(self):
        with patch.object(app, '_save_cookies', side_effect=OSError('test-cookie-secret')):
            self.assertEqual(self.run_flow(fresh_login()).exit_code, 1)
        with patch.object(app, '_save_cookies', side_effect=[None, OSError('test-cookie-secret')]):
            with self.assertLogs(app.LOG, level='WARNING') as logs:
                self.assertEqual(self.run_flow(fresh_login() + [('POST', '/service-console/action', reply(ACCEPTED))]).exit_code, 0)
            self.assertNotIn('test-cookie-secret', ''.join(logs.output))

    def test_invalid_cookie_file_never_contacts_network(self):
        self.cookie.write_text('not a cookie jar', encoding='utf-8')
        with patch.object(requests.Session, 'send', side_effect=AssertionError('No HTTP allowed')):
            result = app.power_on(cookie_file=self.cookie)
        self.assertEqual(result.exit_code, 1)

    def test_imported_httponly_session_cookie_with_zero_expiry(self):
        self.cookie.write_text('# Netscape HTTP Cookie File\n#HttpOnly_www.ipxr.cn\tFALSE\t/\tTRUE\t0\tPHPSESSID\ttest-cookie-secret\n', encoding='utf-8')
        jar = app._load_cookies(self.cookie)
        self.assertEqual(len(jar), 1)
        cookie = next(iter(jar))
        self.assertIsNone(cookie.expires)
        self.assertTrue(cookie.discard)
        self.assertFalse(cookie.is_expired())
        app._save_cookies(jar, self.cookie)
        self.assertEqual(len(app._load_cookies(self.cookie)), 1)

    def test_expired_or_unrelated_cookies_are_not_reused(self):
        self.cookie.write_text('# Netscape HTTP Cookie File\nwww.ipxr.cn\tFALSE\t/\tTRUE\t1\tOLD\told-value\nelsewhere.invalid\tFALSE\t/\tTRUE\t0\tFOREIGN\tforeign-value\n', encoding='utf-8')
        self.assertEqual(len(app._load_cookies(self.cookie)), 0)

    def test_invalid_parameters_no_http(self):
        for params in [{'service_id': 0}, {'service_id': True}, {'timeout': 0}, {'timeout': float('nan')}]:
            self.assertEqual(self.run_flow([], **params).exit_code, 1)

    def test_cli_exit_codes_and_json_without_network(self):
        script = str(Path(app.__file__).resolve())
        env = {k: v for k, v in os.environ.items() if not k.startswith('IPXR_')}
        env['PYTHONIOENCODING'] = 'utf-8'
        for args, code in [(['--service-id', '0'], 1), (['--bad-secret', 'never-echo-me'], 1), (['--cookie-file', str(self.cookie)], 2)]:
            process = subprocess.run([sys.executable, '-B', script, *args], capture_output=True, text=True, encoding='utf-8', env=env)
            self.assertEqual(process.returncode, code)
            self.assertEqual(json.loads(process.stdout)['exit_code'], code)
            self.assertNotIn('never-echo-me', process.stdout + process.stderr)

    def test_status_query_contract_and_states(self):
        for state, expected in [('on', 'on'), ('off', 'off'), ('checking', 'process')]:
            with self.subTest(state=state):
                steps = [('GET', '/servicedetail', reply(DETAIL)),
                         ('POST', '/provision/default', reply({'status': 200, 'data': {'status': state}}))]
                result = self.run_flow(steps, cached=True, credentials=False, query_status=True)
                self.assertEqual((result.status, result.exit_code), (expected, 0))
                request, options = self.calls[-1]
                self.assertEqual(parse_qs(request.body), {'id': ['10328'], 'func': ['status']})
                self.assertEqual(request.headers['Origin'], app.BASE_URL)
                self.assertEqual(request.headers['X-Requested-With'], 'XMLHttpRequest')
                self.assertEqual(options['timeout'], (10, 45.0))

    def test_status_login_then_query(self):
        result = self.run_flow(fresh_login() + [
            ('POST', '/provision/default', reply({'status': 200, 'data': {'status': 'on'}}))
        ], query_status=True)
        self.assertEqual((result.status, result.exit_code), ('on', 0))

    def test_status_without_credentials_never_contacts_network(self):
        result = self.run_flow([], credentials=False, query_status=True)
        self.assertEqual((result.status, result.exit_code), ('auth_required', 2))

    def test_status_errors_never_submit_or_retry(self):
        cases = [
            (reply('unauthorized', 401), 'auth_required', 2),
            (reply(LOGIN), 'auth_required', 2),
            (reply('', 307, {'Location': app.ACTION_URL}), 'unknown', 3),
            (reply('bad gateway', 502), 'unknown', 3),
            (reply('not json'), 'unknown', 3),
            (reply({'status': 200, 'data': {}}), 'unknown', 3),
            (requests.Timeout('private-test-password'), 'unknown', 3),
        ]
        for response, expected, code in cases:
            with self.subTest(expected=expected, response=type(response).__name__):
                result = self.run_flow([
                    ('GET', '/servicedetail', reply(DETAIL)),
                    ('POST', '/provision/default', response),
                ], cached=True, credentials=False, query_status=True)
                self.assertEqual((result.status, result.exit_code), (expected, code))

    def test_check_status_cli_selects_read_only_operation(self):
        result = app.PowerOnResult('off', '电源状态：已关机。', 10328, 0)
        output = io.StringIO()
        with patch.object(sys, 'argv', ['ipxr_power_on.py', '--check-status']), \
                patch.object(app, 'power_status', return_value=result) as query, \
                patch.object(app, 'power_on', side_effect=AssertionError('No power action allowed')), \
                contextlib.redirect_stdout(output):
            self.assertEqual(app.main(), 0)
        query.assert_called_once()
        self.assertEqual(json.loads(output.getvalue())['status'], 'off')


if __name__ == '__main__':
    unittest.main()
