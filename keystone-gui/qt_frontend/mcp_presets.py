"""Official Streamable HTTP endpoints with documented direct authentication."""

from .mcp_china import HEADERS, SERVICES, SETUP
from .mcp_local import LOCAL
from .mcp_meeting import AUTHORIZE as MEETING_AUTHORIZE
from .mcp_oauth import BAIDU_AUTHORIZE, DISCOVERED_OAUTH, OAUTH
from .mcp_us import GOOGLE_CLOUD, US_SERVICES

# id: (name, description, endpoint, credential label, header, prefix, docs)
PRESETS = {
    'github': ('GitHub MCP', '仓库、议题与拉取请求', 'https://api.githubcopilot.com/mcp/',
               'GitHub Personal Access Token', 'Authorization', 'Bearer ',
               'https://github.com/github/github-mcp-server'),
    'linear': ('Linear', '议题、项目与评论', 'https://mcp.linear.app/mcp',
               'Linear API Key 或访问令牌', 'Authorization', 'Bearer ', 'https://linear.app/docs/mcp'),
    'stripe': ('Stripe', '支付、客户与账单', 'https://mcp.stripe.com',
               'Stripe Agent API Key', 'Authorization', 'Bearer ', 'https://docs.stripe.com/mcp'),
    'sentry': ('Sentry', '错误监控与问题排查', 'https://mcp.sentry.dev/mcp',
               'Sentry API Token', 'Authorization', 'Sentry-Bearer ', 'https://github.com/getsentry/toolkit'),
    'firecrawl': ('Firecrawl', '网页搜索与内容提取', 'https://mcp.firecrawl.dev/v2/mcp',
                  'Firecrawl API Key', 'Authorization', 'Bearer ', 'https://docs.firecrawl.dev/mcp-server'),
    'developer-knowledge': ('Google Developer Knowledge', 'Google 官方开发文档',
                            'https://developerknowledge.googleapis.com/mcp', 'Developer Knowledge API Key',
                            'X-Goog-Api-Key', '', 'https://developers.google.com/knowledge/mcp'),
    'aws': ('AWS 文档', '官方文档与区域服务查询', 'https://aws-mcp.us-east-1.api.aws/mcp',
            '', '', '', 'https://docs.aws.amazon.com/agent-toolkit/latest/userguide/mcp-server.html'),
    'exa': ('Exa', '公开网页搜索与读取', 'https://mcp.exa.ai/mcp',
            'Exa API Key', 'X-Api-Key', '', 'https://exa.ai/docs/get-started/exa-mcp'),
    'context7': ('Context7', '开发库文档与代码示例', 'https://mcp.context7.com/mcp',
                 'Context7 API Key', 'Authorization', 'Bearer ', 'https://context7.com/docs/installation'),
    'notion': ('Notion', '工作区页面、文档和数据库', 'https://mcp.notion.com/mcp',
               '', 'Authorization', 'Bearer ', 'https://developers.notion.com/guides/mcp/get-started-with-mcp'),
    'paypal': ('PayPal 沙箱', '测试环境的订单、账单与商家工具', 'https://mcp.sandbox.paypal.com/http',
               'PayPal Sandbox Access Token', 'Authorization', 'Bearer ', 'https://developer.paypal.com/ai-tools/mcp-server'),
    'square': ('Square 沙箱', '测试环境的商品、客户和订单', '', 'Square Sandbox Access Token', '', '',
               'https://developer.squareup.com/docs/mcp'),
    'obsidian': ('Obsidian', '读取和搜索指定本地仓库', '', '', '', '',
                 'https://github.com/mrchipset/obsidian-mcp-http'),
    'musicbrainz': ('MusicBrainz', '艺人、专辑与录音资料', '', '', '', '',
                    'https://www.npmjs.com/package/@unclick/musicbrainz-mcp'),
    'discogs': ('Discogs', '唱片目录、艺人、厂牌与收藏', '', 'Discogs Personal Access Token', '', '',
                'https://github.com/cswkim/discogs-mcp-server'),
}

PRESETS.update({
    key: (name, detail, url, credential, *HEADERS.get(key, ('Authorization', 'Bearer ')), docs)
    for key, (name, detail, url, credential, docs, _, _) in SERVICES.items()
})

PRESETS.update({key: row[:7] for key, row in US_SERVICES.items()})

GLOBAL_CATEGORIES = {
    'github': '开发与云服务', 'linear': '办公与知识', 'stripe': '商业与生活',
    'sentry': '开发与云服务', 'firecrawl': '搜索与信息', 'developer-knowledge': '开发与云服务',
    'aws': '开发与云服务', 'exa': '搜索与信息', 'context7': '开发与云服务',
    'notion': '办公与知识', 'paypal': '商业与生活', 'square': '商业与生活',
    'obsidian': '办公与知识', 'musicbrainz': '内容与媒体', 'discogs': '内容与媒体',
}

US_EXISTING = frozenset({
    'github', 'linear', 'stripe', 'sentry', 'firecrawl', 'developer-knowledge', 'aws', 'exa',
    'notion', 'paypal', 'square', 'discogs',
})

CATEGORIES = [
    '地图与出行', '搜索与信息', '办公与知识', '开发与云服务', '商业与生活',
    '企业与数据', '内容与媒体', '智能设备', '服务目录',
]

MANUAL_LOGIN = {
    'baidu-netdisk': (BAIDU_AUTHORIZE, '粘贴百度官方授权结果地址或 access_token。'),
    'tencent-meeting': (MEETING_AUTHORIZE, '在腾讯会议 AI Skill 专区登录，复制生成的 Token。Keystone 会自动配置官方 MCP。'),
    'zhihu': ('https://developer.zhihu.com/profile', '登录知乎数据开放平台个人中心，生成并粘贴 Access Secret。'),
}

DUAL_AUTH = frozenset({'huggingface', 'tavily'})

BROWSER_LOGIN = dict.fromkeys(OAUTH.keys() | DISCOVERED_OAUTH.keys(), 'oauth') | {
    **dict.fromkeys(MANUAL_LOGIN, 'manual-token'),
    **dict.fromkeys(GOOGLE_CLOUD, 'google-cloud'),
    'feishu': 'app-oauth', 'wecom': 'scan',
}

AWS_KNOWLEDGE_TOOLS = frozenset({
    'aws___search_documentation', 'aws___read_documentation', 'aws___get_regional_availability',
    'aws___list_regions', 'aws___retrieve_skill',
})


def _preset_row(key, preset):
    name, detail, url, credential, _, _, docs = preset
    if key in SERVICES:
        region, (category, icon) = 'china', SERVICES[key][-2:]
    elif key in US_SERVICES:
        region, (category, icon) = 'us', US_SERVICES[key][-2:]
    else:
        region = 'us' if key in US_EXISTING else 'global'
        category = GLOBAL_CATEGORIES[key]
        icon = key + ('.png' if key in {'firecrawl', 'context7'} else '.svg')
    return {'id': f'preset.{key}', 'key': key, 'name': name, 'detail': detail, 'url': url,
            'credential': credential, 'optionalCredential': key in {'exa', 'context7'}, 'docs': docs,
            'oauth': key in BROWSER_LOGIN, 'loginMode': BROWSER_LOGIN.get(key, ''), 'local': key in LOCAL,
            'loginHint': MANUAL_LOGIN.get(key, ('', ''))[1], 'dualAuth': key in DUAL_AUTH, 'region': region, 'category': category, 'icon': icon}


def preset_rows():
    return [_preset_row(key, preset) for key, preset in PRESETS.items()] + [
        {'id': f'setup.{key}', 'name': name, 'detail': detail, 'url': '', 'credential': '',
         'docs': docs, 'region': 'china', 'category': category, 'icon': icon, 'setup': setup,
         'oauth': False, 'local': False, 'optionalCredential': False}
        for key, (name, detail, docs, category, icon, setup) in SETUP.items()
    ]
