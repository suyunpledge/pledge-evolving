"""Chinese MCP services with public provider documentation."""

from .mcp_meeting import ENDPOINT as MEETING_ENDPOINT

AMAP = 'https://lbs.amap.com/api/mcp-server/gettingstarted'
BAIDU_MAP = 'https://github.com/baidu-maps/mcp/blob/main/README_zh.md'
GLM = 'https://docs.bigmodel.cn/cn/coding-plan/mcp/'
WPS = 'https://open.wps.cn/documents/app-integration-dev/mcp-server/'
ALI = 'https://help.aliyun.com/zh/model-studio/'
KUAIDI = 'https://github.com/kuaidi100-api/kuaidi100-MCP#二streamable方式'

# id: (name, description, endpoint, credential label, docs, category, icon)
SERVICES = {
    'amap': ('高德地图', '地点、路线规划与天气', 'https://mcp.amap.com/mcp',
             '高德 MCP Key', AMAP, '地图与出行', '@alibaba'),
    'baidu-maps': ('百度地图', '地址解析、地点和驾车路线', 'https://mcp.map.baidu.com/mcp',
                   '百度地图 AK', BAIDU_MAP, '地图与出行', '@baidu'),
    'tencent-map': ('腾讯位置服务', '地点搜索、路线、距离与天气', 'https://mcp.map.qq.com/mcp',
                    '腾讯位置服务 WebService API Key',
                    'https://lbs.qq.com/service/MCPServer/MCPServerGuide/overview', '地图与出行', '@tencent'),
    'glm-search': ('智谱联网搜索', 'GLM 编程计划的实时网页搜索',
                   'https://open.bigmodel.cn/api/mcp/web_search_prime/mcp', '智谱 API Key',
                   GLM + 'search-mcp-server', '搜索与信息', '@zhipu'),
    'glm-reader': ('智谱网页读取', '提取公开网页的标题与正文',
                   'https://open.bigmodel.cn/api/mcp/web_reader/mcp', '智谱 API Key',
                   GLM + 'reader-mcp-server', '搜索与信息', '@zhipu'),
    'bailian-search': ('百炼联网搜索', '搜索公开网页与实时信息',
                       'https://dashscope.aliyuncs.com/api/v1/mcps/WebSearch/mcp', '百炼 API Key',
                       ALI + 'mcp-external-calls', '搜索与信息', '@alibaba'),
    'gitee': ('Gitee', '仓库、文件、议题与合并请求', 'https://api.gitee.com/mcp',
              'Gitee 私人令牌',
              'https://blog.gitee.com/2026/01/26/gitee-mcp-support-remote-access-no-local-deployment-ai-assistant-plug-and-play/',
              '开发与云服务', ''),
    'glm-zread': ('智谱开源仓库', '查询开源仓库结构、代码与文档',
                  'https://open.bigmodel.cn/api/mcp/zread/mcp', '智谱 API Key',
                  GLM + 'zread-mcp-server', '开发与云服务', '@zhipu'),
    'kuaidi100': ('快递100', '物流轨迹、运费与时效预估',
                  'https://api.kuaidi100.com/mcp/streamable', '快递100 API Key',
                  KUAIDI, '商业与生活', ''),
    'mcd-china': ('麦当劳中国', '官方开放工具，包括营养、餐厅与优惠查询',
                  'https://mcp.mcd.cn', '麦当劳 MCP Token',
                  'https://github.com/M-China/mcd-mcp-server', '商业与生活', ''),
    'feishu': ('飞书 OpenAPI', '使用飞书身份访问文档、日历和会话。首次连接自动安装官方 MCP。',
               '', '', 'https://github.com/larksuite/lark-openapi-mcp/blob/main/README_ZH.md',
               '办公与知识', ''),
    'wecom': ('企业微信', '扫码绑定企业微信机器人，使用官方工具访问文档、消息、微盘等。',
              '', '', 'https://github.com/WecomTeam/wecom-cli/blob/main/docs/cli-reference.md',
              '办公与知识', '@tencent'),
    'baidu-netdisk': ('百度网盘', '使用百度账号授权，获取文件、搜索与分享等网盘工具。',
                     'https://mcp-pan.baidu.com/sse', '',
                     'https://github.com/baidu-netdisk/mcp/blob/main/README.md', '办公与知识', '@baidu'),
    'tencent-meeting': ('腾讯会议', '查询会议、参会人和纪要，管理已授权的会议。',
                        MEETING_ENDPOINT, '腾讯会议 MCP Token',
                        'https://meeting.tencent.com/support/articles/14/index.html', '办公与知识', '@tencent'),
    'zhihu': ('知乎', '知乎及全网搜索、热榜和直答',
              'https://developer.zhihu.com/api/mcp/v1', '知乎 Access Secret',
              'https://developer.zhihu.com/console/docs?key=unified_mcp', '搜索与信息', ''),
    'weibo': ('微博', '通过微博官方账号授权获取内容与开放平台工具。',
              'https://cli.weibo.com/mcp', '', 'https://open.weibo.com/cli/manual/mcp', '内容与媒体', ''),
    'tencent-docs': ('腾讯文档', '文档、智能表格与目录',
                     'https://docs.qq.com/openapi/mcp', '腾讯文档个人 Token',
                     'https://developer.cloud.tencent.com/mcp/server/11803', '办公与知识', '@tencent'),
    'caiyun': ('彩云天气', '实时天气、逐小时预报和预警', 'https://mcp-weather.caiyunapp.com/mcp',
               '彩云天气 API Key', 'https://docs-preview.caiyunapp.com/weather-api/mcp.html', '搜索与信息', ''),
    'tianyancha': ('天眼查', '企业工商、股权与风险信息', 'https://mcp.tianyancha.com/v1',
                    '天眼查 API Key', 'https://support.huaweicloud.com/bestpractice-agentarts/agentarts_06_0142.html',
                    '企业与数据', ''),
    'yuque': ('语雀', '个人站点的文档、知识库与笔记。首次连接自动安装官方 MCP。',
               '', '语雀个人 Token', 'https://github.com/yuque/yuque-mcp-server', '办公与知识', ''),
    'minimax-cn': ('MiniMax 多媒体', '语音、图片与视频生成。首次连接自动安装官方 MCP。',
                   '', 'MiniMax 中国站 API Key', 'https://github.com/MiniMax-AI/MiniMax-MCP-JS', '内容与媒体', '@minimax'),
}

for domain, label, detail in (
    ('calendar', '日程', '日历、日程与参与人'),
    ('meeting', '会议', '会议和会议纪要'),
    ('yundoc', '云文档', '金山云文档和文件管理'),
    ('dbsheet', '多维表格', '多维表格的工作表、字段和记录'),
    ('airpage', '智能文档', 'WPS 智能文档'),
    ('todo', '个人待办', '个人任务与待办事项'),
    ('message', '协作消息', '用户授权的协作消息与会话'),
):
    SERVICES[f'wps-{domain}'] = (
        f'WPS {label}', detail + ' · 需相应 WPS 开放平台权限',
        f'https://openapi.wps.cn/mcp/v2/kso-{domain}/message', 'WPS 访问令牌',
        WPS + f'tools/mcp_kso-{domain}', '办公与知识', '',
    )

HEADERS = {
    'tencent-meeting': ('X-Tencent-Meeting-Token', ''),
    'tencent-docs': ('Authorization', ''),
    'tianyancha': ('Authorization', ''),
    'caiyun': ('X-Caiyun-API-Key', ''),
}

QUERY_KEYS = {
    'amap': ('key', {}),
    'baidu-maps': ('ak', {}),
    'tencent-map': ('key', {'format': '0'}),
    'kuaidi100': ('key', {}),
}

# These portals issue account-specific endpoints or document additional setup.
# The catalog links to each provider and opens the existing custom HTTP editor
# only when Streamable HTTP configuration is a supported route.
SETUP = {
    'baidu-search': ('百度搜索', '从千帆工具中心复制 Streamable HTTP 地址，凭据使用千帆 API Key。',
                     'https://cloud.baidu.com/doc/qianfan/s/nmweljpt3', '搜索与信息', '@baidu', 'endpoint'),
    'qcc': ('企查查', '企业工商与尽调数据。到个人中心获取所需服务的地址和 API Key。',
            'https://agent.qcc.com/guide', '企业与数据', '', 'endpoint'),
    'dingtalk': ('钉钉', '在钉钉开放平台创建应用并开通 MCP。服务由平台签发带密钥的专属地址。',
                 'https://docs.qoder.cn/qoderwork/practical-tutorial/mcp', '办公与知识', '', 'query-endpoint'),
    'didi': ('滴滴出行', '出行与叫车工具，需登录并绑定滴滴账号。',
             'https://mcp.didichuxing.com/', '地图与出行', '', 'guide'),
    'meitu': ('美图影像', '图像修复、局部重绘和画质增强。需美图开放平台凭据。',
              'https://www.miraclevision.com/tech/mcpserver', '内容与媒体', '', 'guide'),
    'volcengine': ('火山引擎 API 网关', '部署后，从网关复制 Streamable HTTP 地址。支持无鉴权或 Bearer 鉴权的部署。',
                   'https://docs.volcengine.com/docs/apig/mcp-service?lang=zh', '开发与云服务', '@volcengine', 'endpoint'),
    'aliyun-monitor': ('阿里云云监控', '指标、告警与云资源观测。官方接入使用 OAuth 或本地代理。',
                       'https://help.aliyun.com/zh/cms/cloudmonitor-2-0/cloud-monitor-mcp', '开发与云服务', '@alibaba', 'guide'),
    'tuya': ('涂鸦智能', '涂鸦 AI 平台的设备与业务工具配置。',
             'https://developer.tuya.com/cn/docs/iot/custom-mcp?id=Kety4zbdvwdn8', '智能设备', '', 'guide'),
    'modelscope': ('魔搭 MCP 广场', '打开目录，从服务页复制兼容的 Streamable HTTP 地址。',
                   'https://modelscope.cn/mcp', '服务目录', '@alibaba', 'endpoint'),
    'tencent-market': ('腾讯云 MCP 广场', '浏览腾讯及第三方服务，按服务文档开通并复制地址。',
                       'https://cloud.tencent.com/developer/mcp', '服务目录', '@tencent', 'endpoint'),
    'bailian-market': ('阿里云百炼 MCP 广场', '开通百炼托管服务，使用其 Streamable HTTP 地址和百炼 API Key。',
                      ALI + 'mcp-external-calls', '服务目录', '@alibaba', 'endpoint'),
}
