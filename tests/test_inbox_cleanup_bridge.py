"""Offline contract tests for the *staged* cleanup bridge, never the live module."""
import importlib.util
import io
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

STAGED = Path(__file__).resolve().parents[1] / 'cos' / 'communication_bridge.py'
PACKAGE = '_staged_cleanup_cos'


def load_staged_bridge():
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(STAGED.parent)]
    integration = types.ModuleType(PACKAGE + '.integrations')
    integration.skill_dir = lambda name: STAGED.parent
    spec = importlib.util.spec_from_file_location(PACKAGE + '.communication_bridge', STAGED)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {PACKAGE: package, PACKAGE + '.integrations': integration, spec.name: module}):
        spec.loader.exec_module(module)
    assert Path(module.__file__).resolve() == STAGED.resolve(), module.__file__
    return module


class GraphSpy:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    def _record(self, method, *args, **kwargs):
        self.calls.append((method, args, kwargs))
        if self.fail == method:
            raise RuntimeError('Graph API 403: insufficient permission')
        if method == 'get':
            return {'id': 'owner id/+?'}
        return {'id': 'moved-message-id'}

    def get(self, *args, **kwargs): return self._record('get', *args, **kwargs)
    def post(self, *args, **kwargs): return self._record('post', *args, **kwargs)
    def patch(self, *args, **kwargs): return self._record('patch', *args, **kwargs)
    def delete(self, *args, **kwargs): return self._record('delete', *args, **kwargs)


class InboxCleanupBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bridge = load_staged_bridge()

    def test_delete_is_recoverable_move_not_graph_delete(self):
        graph = GraphSpy()
        result = self.bridge.perform(graph, 'delete_email', {'id': 'A/B +?#'})
        self.assertEqual(graph.calls, [('post', ('/me/messages/A%2FB%20%2B%3F%23/move', {'destinationId': 'deleteditems'}), {})])
        self.assertTrue(result['executed'])
        self.assertFalse(any(method == 'delete' or 'permanentDelete' in str(args) for method, args, _ in graph.calls))

    def test_email_read_only_patches_message(self):
        graph = GraphSpy()
        self.bridge.perform(graph, 'mark_email_read', {'id': 'A/B +?#'})
        self.assertEqual(graph.calls, [('patch', ('/me/messages/A%2FB%20%2B%3F%23', {'isRead': True}), {})])
        self.assertFalse(any('/move' in str(args) for _, args, _ in graph.calls))

    def test_teams_native_read_uses_graph_owner_and_encoded_chat_id(self):
        graph = GraphSpy()
        self.bridge.perform(graph, 'mark_teams_read', {'chat_id': '19:a/b+#'})
        self.assertEqual(graph.calls, [
            ('get', ('/me',), {'params': {'$select': 'id'}}),
            ('post', ('/chats/19%3Aa%2Fb%2B%23/markChatReadForUser', {'user': {'id': 'owner id/+?'}}), {}),
        ])

    def test_graph_permission_failure_propagates_without_success(self):
        for action, arguments, fail in [
            ('delete_email', {'id': 'id'}, 'post'),
            ('mark_email_read', {'id': 'id'}, 'patch'),
            ('mark_teams_read', {'chat_id': 'id'}, 'get'),
            ('mark_teams_read', {'chat_id': 'id'}, 'post'),
        ]:
            with self.subTest(action=action, fail=fail):
                graph = GraphSpy(fail=fail)
                with self.assertRaisesRegex(RuntimeError, 'Graph API 403'):
                    self.bridge.perform(graph, action, arguments)
                self.assertFalse(any(method == 'delete' for method, _, _ in graph.calls))

    def test_bridge_main_reports_failure_not_executed_instead_of_success(self):
        bridge = self.bridge
        for action, args, fail in [('delete_email', {'id': 'id'}, 'post'),
                                   ('mark_email_read', {'id': 'id'}, 'patch'),
                                   ('mark_teams_read', {'chat_id': 'id'}, 'post')]:
            with self.subTest(action=action):
                fake_monitor = types.ModuleType(PACKAGE + '.monitor_bridge')
                fake_monitor.stable_mail_client = lambda cls: GraphSpy(fail=fail)
                fake_graph_client = types.ModuleType('graph_client')
                fake_graph_client.GraphClient = type('GraphClient', (), {})
                fake_integration = types.ModuleType(PACKAGE + '.integrations')
                fake_integration.skill_dir = lambda name: STAGED.parent
                output = io.StringIO()
                with patch.dict(sys.modules, {PACKAGE: types.ModuleType(PACKAGE),
                                              PACKAGE + '.monitor_bridge': fake_monitor,
                                              PACKAGE + '.integrations': fake_integration,
                                              'graph_client': fake_graph_client}), \
                     patch.object(sys, 'stdin', io.StringIO(json.dumps({'action': action, 'arguments': args}))), \
                     patch.object(sys, 'stdout', output), patch.object(sys, 'path', sys.path[:]):
                    bridge.main()
                result = json.loads(output.getvalue())
                self.assertTrue(result['not_executed'])
                self.assertIn('Graph API 403', result['bridge_error'])
                self.assertNotIn('executed', result)

    def test_rejects_empty_and_non_string_ids_before_any_write(self):
        for action, key in [('delete_email', 'id'), ('mark_email_read', 'id'), ('mark_teams_read', 'chat_id')]:
            for bad in (None, '', ' ', 42):
                graph = GraphSpy()
                with self.subTest(action=action, bad=bad), self.assertRaises(ValueError):
                    self.bridge.perform(graph, action, {key: bad})
                self.assertFalse(any(method in ('post', 'patch', 'delete') for method, _, _ in graph.calls))


if __name__ == '__main__':
    unittest.main()
