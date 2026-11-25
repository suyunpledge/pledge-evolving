"""Pet chat: bounded Chat-mode rounds, pack persona/greetings, and the pet window bubble."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

os.environ.setdefault('QT_QUICK_CONTROLS_STYLE', 'Basic')
from PySide6.QtCore import QObject, QPoint, Qt, QUrl
from PySide6.QtGui import QFont
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtTest import QTest

from ._test_app import APP
from .controller import ChatController
from .pet_chat import DEFAULT_PERSONA, HISTORY_LIMIT, PetChat, greet_bucket
from .pets import BUILTIN_ROOT, PetController, scan_packs
from .test_workbench import until
from .workbench import WorkbenchController
from revon_client import ChatMessage, CompletionResult, GenerationCancelled

ROOT = Path(__file__).parent


class ScriptedClient:
    """Offline stand-in for RevonGatewayClient.stream_chat."""

    def __init__(self, pieces=('你好', '呀，主人～'), error=None, hang_after=None):
        self.pieces = pieces
        self.error = error
        self.hang_after = hang_after
        self.requests = []
        self.secret_scope = None

    def stream_chat(self, messages, model='default', temperature=0.7, reasoning_effort=None,
                    on_chunk=None, cancel_event=None, tools=None, system=None, on_progress=None):
        self.requests.append({'messages': list(messages), 'model': model, 'tools': tools,
                              'temperature': temperature})
        if self.error is not None:
            raise self.error
        for index, piece in enumerate(self.pieces):
            if cancel_event is not None and cancel_event.is_set():
                raise GenerationCancelled('已停止生成')
            if self.hang_after is not None and index >= self.hang_after:
                while cancel_event is None or not cancel_event.is_set():
                    time.sleep(0.02)
                raise GenerationCancelled('已停止生成')
            if on_chunk is not None:
                on_chunk(piece)
        return CompletionResult(text=''.join(self.pieces))


def make_controller(home, client, *, demo=False):
    controller = ChatController(home, demo=demo, client_factory=lambda _gateway: client)
    controller._connected = not demo
    return controller


class PetChatBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.home = Path(self.temp.name)
        self.client = ScriptedClient()
        self.controller = make_controller(self.home, self.client)
        self.pets = PetController(self.home)
        self.pets.select('mingyue')
        self.chat = PetChat(self.controller, self.pets)

    def tearDown(self):
        self.chat.shutdown()
        self.controller.shutdown()
        APP.processEvents()
        self.temp.cleanup()

    def wait_idle(self):
        until(lambda: not self.chat.busy, timeout=8)
        APP.processEvents()

    def test_mingyue_pack_is_builtin_and_complete(self):
        packs, errors = scan_packs(self.home)
        self.assertIn('mingyue', packs, errors)
        pack = packs['mingyue']
        self.assertTrue(pack['builtin'])
        self.assertEqual(pack['name'], '溟月·鲸鱼娘')
        for state in ('idle', 'walk', 'sleep', 'think', 'happy', 'worried'):
            self.assertTrue(Path(QUrl(pack['states'][state]['source']).toLocalFile()).is_file(), state)
        for state in ('think', 'happy', 'worried'):
            self.assertTrue(Path(QUrl(pack['faces'][state]).toLocalFile()).is_file(), state)
        lines = json.loads((BUILTIN_ROOT / 'mingyue' / 'lines.json').read_text(encoding='utf-8'))
        self.assertIn('溟月', lines['persona'])
        self.assertTrue((BUILTIN_ROOT / 'mingyue' / 'NOTICE.md').is_file())

    def test_greet_bucket_covers_the_day(self):
        self.assertEqual(greet_bucket(6), 'morning')
        self.assertEqual(greet_bucket(9), 'forenoon')
        self.assertEqual(greet_bucket(12), 'noon')
        self.assertEqual(greet_bucket(15), 'afternoon')
        self.assertEqual(greet_bucket(18), 'evening')
        self.assertEqual(greet_bucket(23), 'night')
        self.assertEqual(greet_bucket(2), 'night')

    def test_greeting_comes_from_the_pack(self):
        lines = json.loads((BUILTIN_ROOT / 'mingyue' / 'lines.json').read_text(encoding='utf-8'))
        pool = {s for bucket in lines['greetings'].values() for s in bucket} | set(lines['idle'])
        self.assertIn(self.chat.greeting, pool)

    def test_send_needs_a_connected_gateway(self):
        self.controller._connected = False
        self.chat.send('你好')
        self.assertFalse(self.chat.busy)
        self.assertIn('网关', self.chat.error)
        self.assertEqual(self.client.requests, [])

    def test_demo_mode_refuses(self):
        controller = make_controller(self.home, self.client, demo=True)
        chat = PetChat(controller, self.pets)
        chat.send('你好')
        self.assertFalse(chat.busy)
        self.assertIn('预览', chat.error)
        self.assertEqual(self.client.requests, [])
        controller.shutdown()

    def test_roundtrip_is_chat_mode_without_tools(self):
        self.chat.send('你是谁呀？')
        self.assertTrue(self.chat.busy)
        self.wait_idle()
        self.assertEqual(self.chat.reply, '你好呀，主人～')
        self.assertEqual(self.chat.error, '')
        self.assertEqual([m.role for m in self.chat._history], ['user', 'assistant'])
        self.assertEqual(len(self.client.requests), 1)
        request = self.client.requests[0]
        self.assertIsNone(request['tools'])  # 轻量聊天：零工具、零审批
        system = request['messages'][0]
        self.assertEqual(system.role, 'system')
        self.assertIn('溟月', system.content)
        self.assertIn('当前处于 Chat 模式，请直接回复。', system.content)
        self.assertNotIn('keystone_enter_work', system.content)
        self.assertEqual(request['messages'][-1].content, '你是谁呀？')

    def test_history_stays_bounded(self):
        self.chat._history = [ChatMessage('user' if i % 2 == 0 else 'assistant', f'旧消息{i}')
                              for i in range(HISTORY_LIMIT + 6)]
        self.chat.send('新的一轮')
        self.wait_idle()
        self.assertLessEqual(len(self.chat._history), HISTORY_LIMIT)
        self.assertEqual(self.chat._history[0].role, 'user')
        self.assertEqual(self.chat._history[-1].content, '你好呀，主人～')

    def test_errors_are_redacted_and_recoverable(self):
        self.client.error = RuntimeError('上游失败 sk-abcdefghijklmnopqrstuvwx')
        self.chat.send('在吗')
        self.wait_idle()
        self.assertTrue(self.chat.error)
        self.assertNotIn('sk-abcdefghijklmnopqrstuvwx', self.chat.error)
        self.client.error = None
        self.chat.send('再试一次')
        self.wait_idle()
        self.assertEqual(self.chat.error, '')
        self.assertEqual(self.chat.reply, '你好呀，主人～')

    def test_stop_keeps_the_partial_reply(self):
        self.client = ScriptedClient(pieces=('前半句', '后半句'), hang_after=1)
        self.controller._client = self.client
        self.chat.send('讲个长故事')
        until(lambda: self.chat.reply == '前半句', timeout=8)
        self.chat.stop()
        self.wait_idle()
        self.assertEqual(self.chat.reply, '前半句')
        self.assertEqual(self.chat._history[-1].role, 'assistant')
        self.assertEqual(self.chat._history[-1].content, '前半句')

    def test_clear_resets_the_conversation(self):
        self.chat.send('你好')
        self.wait_idle()
        self.chat.clear()
        self.assertEqual(self.chat._history, [])
        self.assertEqual(self.chat.reply, '')
        self.assertTrue(self.chat.greeting)

    def test_default_persona_when_pack_has_no_lines(self):
        self.pets.select('crab')
        self.chat.send('你好')
        self.wait_idle()
        system = self.client.requests[0]['messages'][0]
        self.assertIn(DEFAULT_PERSONA[:12], system.content)


class PetChatWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.client = ScriptedClient(pieces=('喵？', '我是溟月呀🐋'))
        self.controller = make_controller(self.temp.name, self.client)
        self.workbench = WorkbenchController(self.temp.name, demo=True)
        self.pets = self.workbench.pets
        self.chat = PetChat(self.controller, self.pets)
        self.engine = QQmlApplicationEngine()
        self.warnings = []
        self.engine.warnings.connect(lambda rows: self.warnings.extend(map(str, rows)))
        context = self.engine.rootContext()
        for name, value in {'controller': self.controller, 'workbench': self.workbench,
                            'smokeMode': True, 'gpuEnabled': True, 'uiFont': QFont('Segoe UI', 10),
                            'brandUrl': QUrl.fromLocalFile(str(ROOT.parent / 'assets/revon-logo-104.png')),
                            'petChatService': self.chat}.items():
            context.setContextProperty(name, value)
        self.engine.load(QUrl.fromLocalFile(str(ROOT / 'qml/Main.qml')))
        self.assertTrue(self.engine.rootObjects(), self.warnings)
        self.window = self.engine.rootObjects()[0]
        self.window.setFlag(Qt.WindowType.WindowDoesNotAcceptFocus, True)
        self.window.setPosition(-4000, -3000)
        self.window.show()
        self.loader = self.window.findChild(QObject, 'petLoader')

    def tearDown(self):
        import shiboken6
        self.chat.shutdown()
        self.pets.setEnabled(False)
        APP.processEvents()
        self.workbench.shutdown()
        self.controller.shutdown()
        if shiboken6.isValid(self.window):
            self.window.setProperty('exitConfirmed', True)
            self.window.close()
        self.engine.deleteLater()
        APP.processEvents()
        self.temp.cleanup()

    def show_pet(self):
        self.pets.setEnabled(True)
        until(lambda: self.loader.property('item') is not None)
        pet = self.loader.property('item')
        pet.setFlag(Qt.WindowType.WindowDoesNotAcceptFocus, True)
        until(pet.isExposed)
        return pet

    def test_click_opens_bubble_and_send_streams_reply(self):
        self.pets.select('mingyue')
        pet = self.show_pet()
        self.assertIsNotNone(pet.property('petChat'))
        panel = pet.findChild(QObject, 'petChatPanel')
        self.assertFalse(panel.property('visible'))
        body = pet.findChild(QObject, 'petBody')
        centre = QPoint(body.x() + body.width() // 2, body.y() + body.height() // 2)
        QTest.mouseClick(pet, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, centre)
        until(lambda: pet.property('chatOpen'))
        self.assertTrue(panel.property('visible'))
        reply = pet.findChild(QObject, 'petChatReply')
        self.assertTrue(reply.property('text'))  # greeting from the pack
        input_field = pet.findChild(QObject, 'petChatInput')
        input_field.setProperty('text', '你是谁')
        # 非 demo 控制器启动时会探测真实网关；测试里重新注入连接态与替身客户端。
        self.controller._connected = True
        self.controller._client = self.client
        send = pet.findChild(QObject, 'petChatSend')
        until(lambda: send.height() > 0 and send.width() > 0)  # 等气泡布局稳定
        APP.processEvents()
        send_at_f = send.mapToItem(pet.contentItem(), QPoint(send.width() // 2, send.height() // 2))
        send_at = QPoint(int(send_at_f.x()), int(send_at_f.y()))
        QTest.mouseClick(pet, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, send_at)
        until(lambda: not self.chat.busy and self.chat.reply, timeout=8)
        APP.processEvents()
        self.assertEqual(reply.property('text'), '喵？我是溟月呀🐋')
        self.assertEqual(len(self.client.requests), 1)
        self.assertIsNone(self.client.requests[0]['tools'])
        # 收起：窗口高度还原，位置折算回收起态。气泡展开后 body 在窗口内下移，重新取坐标。
        centre = QPoint(body.x() + body.width() // 2, body.y() + body.height() // 2)
        QTest.mouseClick(pet, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, centre)
        until(lambda: not pet.property('chatOpen'))
        self.assertFalse(panel.property('visible'))
        self.assertEqual(self.warnings, [])

    def test_double_click_pats_region_and_triple_click_celebrates(self):
        self.pets.select('mingyue')
        pet = self.show_pet()
        body = pet.findChild(QObject, 'petBody')
        head = QPoint(body.x() + body.width() // 2, body.y() + int(body.height() * 0.2))
        QTest.mouseClick(pet, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, head)
        QTest.mouseClick(pet, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, head)
        until(lambda: pet.property('actionFlash') == 'pat', timeout=4)
        self.assertIn('react-head', pet.property('shownArt').toVariant()['source'])
        for _ in range(3):
            QTest.mouseClick(pet, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, head)
        until(lambda: pet.property('actionFlash') == 'celebrate', timeout=4)
        self.assertIn('meme-heart', pet.property('shownArt').toVariant()['source'])
        self.assertFalse(pet.property('chatOpen'))  # 多击不误开聊天

    def test_feed_action_and_idle_chatter_bubble(self):
        self.pets.select('mingyue')
        pet = self.show_pet()
        from PySide6.QtCore import QMetaObject, Q_ARG
        QMetaObject.invokeMethod(pet, 'triggerAction', Qt.ConnectionType.DirectConnection, Q_ARG('QVariant', 'feed'))
        until(lambda: pet.property('actionFlash') == 'feed', timeout=4)
        self.assertIn('eat.webp', pet.property('shownArt').toVariant()['source'])
        clock = pet.findChild(QObject, 'chatterClock')
        clock.setProperty('interval', 60)
        clock.setProperty('running', True)
        until(lambda: pet.property('chatter') != '', timeout=4)
        bubble = pet.findChild(QObject, 'petChatter')
        self.assertTrue(bubble.property('visible'))
        lines = json.loads((BUILTIN_ROOT / 'mingyue' / 'lines.json').read_text(encoding='utf-8'))
        self.assertIn(pet.property('chatter'), lines['idle'])


if __name__ == '__main__':
    unittest.main()
