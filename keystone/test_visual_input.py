import base64
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

from .loop import Agent
from .model import Provider
from .policy import Policy, Mode
from .test_project_rules import Router
from .toolwire import visual_message
from .tools import build_builtin_registry
from .visual_input import image_data, image_path, supports_vision
from .wire import anthropic_to_openai_request


def png():
    def chunk(tag, body):
        return struct.pack('>I', len(body)) + tag + body + struct.pack('>I', zlib.crc32(tag + body))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)) +
            chunk(b'tEXt', b'comment\0unrelated metadata') + chunk(b'IDAT', zlib.compress(b'\0\xff\0\0')) + chunk(b'IEND', b''))


class VisualInputTests(unittest.TestCase):
    def test_observation_bounds_and_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = folder / 'red.png'
            path.write_bytes(png())
            inside = folder / 'sub'
            inside.mkdir()
            with self.assertRaises(ValueError):
                image_path(inside, path)
            mime, encoded = image_data(image_path(folder, 'red.png'))
            self.assertNotIn(b'unrelated metadata', base64.b64decode(encoded))
            for wire in ('openai', 'anthropic'):
                msg = visual_message([(mime, encoded)], wire)
                if wire == 'anthropic':
                    msg = anthropic_to_openai_request({'messages': [msg]})['messages'][0]
                self.assertEqual(msg['content'][1]['image_url']['url'], 'data:image/png;base64,' + encoded)
            path.write_bytes(b'plain text')
            with self.assertRaises(ValueError):
                image_data(path)

    def test_real_agent_delivers_pixels_only_to_next_request(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            (root / 'red.png').write_bytes(png())
            router = Router([json.dumps({'tool': 'view_image', 'args': {'path': 'red.png'}}), 'read the image', 'later'])
            router.providers = {'p': Provider('p', 'http://localhost', '', default_model='custom', route_capabilities={'vision': True})}
            router._order = lambda _: [('p', 'custom')]
            agent = Agent(home=root / 'home', workspace=root, router=router, registry=build_builtin_registry(),
                          policy=Policy(workspace=root, mode=Mode.ACCEPT_EDITS))
            self.addCleanup(agent.secret_scope.close)
            report = agent.run('Look at the test image')
            self.assertEqual(report.text, 'read the image')
            encoded = image_data(root / 'red.png')[1]
            self.assertNotIn(encoded, str(router.calls[0]))
            self.assertIn(encoded, str(router.calls[1]))
            self.assertNotIn(encoded, str(report.events))
            agent.run('another turn')
            self.assertNotIn(encoded, str(router.calls[-1]))
            self.assertFalse(any(encoded.encode() in p.read_bytes() for p in (root / 'home').rglob('*.jsonl')))
            router.providers['p'].route_capabilities['vision'] = False
            self.assertNotIn('view_image', {s.name for s in agent._execution_specs()})

    def test_route_budget_counts_visual_tiles_independently_of_serialized_image_length(self):
        from .route_capabilities import request_budget
        def estimate(size):
            return request_budget({'messages': [visual_message([('image/png', 'A' * size)])]}, {})['estimated_input_tokens']
        self.assertEqual(estimate(1024), estimate(100000))
        self.assertGreater(estimate(100000), 4096)

    def test_unknown_model_needs_explicit_capability(self):
        self.assertFalse(supports_vision({}, 'unknown'))
        self.assertTrue(supports_vision({}, 'gpt-4o'))
        self.assertFalse(supports_vision({'vision': False}, 'gpt-4o'))
