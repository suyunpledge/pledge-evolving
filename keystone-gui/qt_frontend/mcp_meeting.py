"""Load the Tencent-published MCP connection settings without executing its installer."""

from hashlib import sha256
from io import BytesIO
import json
import threading
from urllib.request import Request
from zipfile import BadZipFile, ZipFile

from keystone.http_transport import open_response
from .store import SessionStore

ENDPOINT = 'https://mcp.meeting.tencent.com/mcp/wemeet-open/v1'
AUTHORIZE = 'https://meeting.tencent.com/ai-skill.html'
VERSION = '1.0.15'
ARCHIVE = f'https://clawhub.ai/api/v1/download?slug=tencent-meeting-skill&version={VERSION}'
READ_TOOLS = frozenset({
    'get_meeting', 'get_meeting_by_code', 'get_meeting_participants', 'get_meeting_invitees',
    'get_waiting_room', 'get_user_meetings', 'get_user_ended_meetings', 'search_meetings',
    'get_job_result', 'get_records_list', 'search_records', 'get_record_addresses',
    'get_transcripts_details', 'get_transcripts_paragraphs', 'search_transcripts',
    'get_smart_minutes', 'search_minutes', 'get_minutes',
    'contact_search', 'contact_lookup_by_phone', 'contact_lookup_by_email',
})

CHECKSUM = '3c73983b1a82422d84be5cc5258f15f30997385133b3a97f1c4cf4703faf3e4e'


def meeting_config(home, cancel):
    path = home / 'gui' / 'qt' / 'mcp-local' / 'tencent-meeting' / 'config.json'
    expected = {'name': 'tencent-meeting-mcp', 'version': 'v' + VERSION, 'baseUrl': ENDPOINT}
    if SessionStore.read(path, None) == expected:
        return expected
    limit = 512 * 1024
    deadline = threading.Event()

    class Cancel:
        def is_set(self):
            return cancel.is_set() or deadline.is_set()

    timer = threading.Timer(25, deadline.set)
    timer.daemon = True
    timer.start()
    try:
        req = Request(ARCHIVE, headers={'Accept': 'application/zip'})
        with open_response(req, timeout=15, cancel_event=Cancel()) as reply:
            content = reply.read(limit + 1)
    finally:
        timer.cancel()
    if len(content) > limit or sha256(content).hexdigest() != CHECKSUM:
        raise ValueError('腾讯会议官方包校验失败。请刷新 Keystone 预设后重试。')
    try:
        with ZipFile(BytesIO(content)) as package:
            config = package.getinfo('config.json')
            if config.file_size > 4096:
                raise ValueError('腾讯会议配置超出大小限制。')
            installed = json.loads(package.read(config))
    except (BadZipFile, ValueError, KeyError):
        raise ValueError('无法读取腾讯会议官方包的连接配置。') from None
    if installed != expected:
        raise ValueError('腾讯会议官方包的连接配置与发布信息不符。')
    if cancel.is_set():
        raise PermissionError('连接已取消。')
    SessionStore.atomic(path, installed)
    return installed
