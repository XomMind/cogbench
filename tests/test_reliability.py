"""Offline regressions: no game, model server, encoder, or cluster required."""
import collections
import io
import http.client
import http.server
import json
from pathlib import Path
import struct
import shutil
import subprocess
import threading
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent
import runner
import webstream


def box(kind, body=b'', extended=False):
    if extended:
        return struct.pack('>I4sQ', 1, kind, 16 + len(body)) + body
    return struct.pack('>I4s', 8 + len(body), kind) + body


class Actions(unittest.TestCase):
    def test_result_distinguishes_budget_blockage_and_ending(self):
        bot = Mock(policy='model', cheat=False, decisions=3, steps=5,
                   invalid=0, fire_misfires=0, seen_names=[], llama=None,
                   action_counts={}, script_counts={}, suppressed={})
        for end, why, status in (
                (None, 'decision budget spent', 'budget_exhausted'),
                (None, 'could not reach the base screen', 'blocked'),
                ({'source': 'scorehistory'}, 'run ended', 'ended')):
            result = agent.Agent.finish(bot, end, -10, 0, why)
            self.assertEqual(result['status'], status)
            self.assertEqual(result['policy'], 'model')
            self.assertTrue(result['fair_view'])

    def test_fire_matches_grammar_and_schema(self):
        for raw in ('fire', '{"verb":"fire"}', '<think>fire<|eot|>'):
            action = agent.Chat._to_action(raw)
            self.assertEqual(action, 'fire')
            self.assertTrue(agent.parse_action(action))
        self.assertTrue(agent.parse_action('fire n'))  # old recorded actions
        self.assertFalse(agent.parse_action('fire nowhere'))

    def test_attach_schema(self):
        chat = agent.Chat('http://localhost')
        schema = chat._payload('', 'schema', {'attach'})['response_format']['json_schema']['schema']
        self.assertIn('slot', schema['properties'])
        for slot in range(8):
            action = chat._to_action(json.dumps({'verb': 'attach', 'slot': slot}))
            self.assertEqual(action, 'attach %d' % slot)
            self.assertTrue(agent.parse_action(action))

    def test_plain_actions_respect_observation(self):
        bot = agent.Agent.__new__(agent.Agent)
        bot.policy, bot.invalid, bot.inventory_size = 'model', 0, 1
        bot.available_verbs = lambda: {'move', 'attach', 'wait'}
        bot.available_dirs = lambda: {'s'}
        for rejected in ('fire', 'move n 4', 'attach 7'):
            bot.llama = Mock()
            bot.llama.act.side_effect = [rejected, 'wait 1']
            self.assertEqual(bot.choose(''), 'wait 1')
        self.assertEqual(bot.invalid, 3)


class Streaming(unittest.TestCase):
    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg is not installed')
    def test_real_fragments_decode_including_late_join(self):
        source = subprocess.run([
            'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
            'testsrc2=size=160x120:rate=10', '-t', '2', '-c:v', 'libx264',
            '-g', '10', '-bf', '0', '-movflags',
            '+frag_keyframe+empty_moov+default_base_moof+omit_tfhd_offset',
            '-frag_duration', '200000', '-f', 'mp4', 'pipe:1',
        ], capture_output=True, check=True, timeout=15).stdout
        fan, fragments = webstream.Fanout(), []
        fan.publish = fragments.append
        fan._pump(io.BytesIO(source))
        self.assertGreaterEqual(len(fragments), 6)
        for start in (0, 5):
            decoded = subprocess.run(
                ['ffmpeg', '-v', 'error', '-i', 'pipe:0', '-f', 'null', '-'],
                input=fan.init + b''.join(fragments[start:]),
                capture_output=True, timeout=15)
            self.assertEqual(decoded.returncode, 0, decoded.stderr)
            self.assertEqual(decoded.stderr, b'')

    def test_http_stream_ends_cleanly_on_encoder_reset(self):
        fan = webstream.Fanout()
        with patch.object(webstream, 'FAN', fan):
            with http.server.ThreadingHTTPServer(('127.0.0.1', 0), webstream.Handler) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                connection = http.client.HTTPConnection(*server.server_address, timeout=2)
                try:
                    connection.request('GET', '/live.mp4')
                    response = connection.getresponse()
                    self.assertEqual(response.status, 503)
                    response.read()
                    fan.init = b'init'
                    connection.request('GET', '/live.mp4')
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(4), b'init')
                    fan.publish(b'fragment')
                    self.assertEqual(response.read(8), b'fragment')
                    fan.reset()
                    self.assertEqual(response.read(), b'')
                finally:
                    connection.close()
                    server.shutdown()
                    thread.join(timeout=2)

    def test_disconnect_uses_identity_not_equal_empty_queues(self):
        fan = webstream.Fanout()
        fan.init = b'init'
        a, b = collections.deque(), collections.deque()
        fan.add(a)
        fan.add(b)
        fan.drop(b)
        self.assertEqual(len(fan.clients), 1)
        self.assertIs(fan.clients[0], a)

    def test_lagging_viewer_reconnects_without_dropping_boxes(self):
        fan = webstream.Fanout()
        fan.init = b'init'
        slow, fast = collections.deque([b'x'] * 60), collections.deque()
        fan.add(slow)
        fan.add(fast)
        fan.publish(b'fragment')
        self.assertEqual(list(slow), [None])
        self.assertEqual(list(fast), [b'fragment'])
        self.assertEqual(fan.bytes_out, len(b'fragment'))

    def test_encoder_boundary_closes_old_viewers(self):
        fan = webstream.Fanout()
        fan.init = b'old init'
        q = collections.deque()
        self.assertEqual(fan.add(q), b'old init')
        fan.reset()
        self.assertEqual(list(q), [None])
        self.assertEqual(fan.init, b'')
        self.assertEqual(fan.add(collections.deque()), b'')
        self.assertFalse(fan.clients)

    def test_only_complete_fragments_are_published(self):
        fan = webstream.Fanout()
        fan.publish = Mock()
        init = box(b'ftyp') + box(b'moov')
        fragment = box(b'moof', b'metadata') + box(b'mdat', b'video', True)
        fan._pump(io.BytesIO(init + fragment))
        self.assertEqual(fan.init, init)
        fan.publish.assert_called_once_with(fragment)

    def test_truncated_and_invalid_boxes_never_publish(self):
        for data in (box(b'moof') + box(b'mdat', b'video', True)[:-1],
                     box(b'moof') + box(b'mdat', b'video')[:-1]):
            fan = webstream.Fanout()
            fan.publish = Mock()
            fan._pump(io.BytesIO(data))
            fan.publish.assert_not_called()
        for size in (0, 4, 100_000_000):
            with self.assertRaises(ValueError):
                webstream.Fanout()._pump(io.BytesIO(struct.pack('>I4s', size, b'mdat')))


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, filename in [('SPEC', 'spec.json'), ('RESULT', 'result.json'), ('LOG', 'agent.log')]:
            patcher = patch.object(runner, name, str(Path(self.tmp.name) / filename))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.run = runner.Runner()
        self.run._decisions = lambda: 3
        self.run.tail = lambda n: []

    def dead_process(self, code):
        self.run.proc = Mock(returncode=code)
        self.run.proc.poll.return_value = code

    def test_stop_during_backoff_cancels_revive(self):
        self.dead_process(1)
        with patch.object(runner.time, 'sleep', side_effect=lambda _: self.run.stop()), \
             patch.object(self.run, 'start') as start:
            self.run.reap()
        start.assert_not_called()

    def test_supervisor_does_not_replace_a_manual_start(self):
        self.dead_process(1)
        with patch.object(runner.time, 'sleep', side_effect=lambda _: setattr(self.run, 'proc', Mock())), \
             patch.object(self.run, 'start') as start:
            self.run.reap()
        start.assert_not_called()

    def test_budget_or_missing_result_does_not_discard_episode(self):
        self.run.spec['loop'] = True
        for result in ({'status': 'budget_exhausted'}, {}):
            Path(runner.RESULT).write_text(json.dumps(result))
            self.dead_process(0)
            with patch.object(runner, 'new_episode') as fresh:
                self.run.reap()
            fresh.assert_not_called()

    def test_ended_game_can_loop(self):
        self.run.spec['loop'] = True
        Path(runner.RESULT).write_text('{"status":"ended"}')
        self.dead_process(0)
        with patch.object(runner, 'new_episode', return_value=(True, 'ok')) as fresh:
            self.run.reap()
        fresh.assert_called_once()
        self.assertEqual(self.run.last['why'], 'ended')

    def test_saved_spec_is_validated(self):
        Path(runner.SPEC).write_text('{"decisions":-2,"temperature":"nan","policy":"bad"}')
        spec = runner.load_spec()
        self.assertEqual(spec['decisions'], 1)
        self.assertEqual(spec['temperature'], 0.7)
        self.assertEqual(spec['policy'], 'model')
        Path(runner.SPEC).write_text('[]')
        self.assertEqual(runner.load_spec(), runner.DEFAULT_SPEC)


if __name__ == '__main__':
    unittest.main()
