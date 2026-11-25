"""Official Google Cloud and US-hosted MCP services."""

CLOUD_DOCS = 'https://docs.cloud.google.com/mcp/supported-products'
CLOUD_AUTH = 'https://docs.cloud.google.com/mcp/set-up-authentication-mcp-servers'

# key: (display name, description, official endpoint host and path, category)
GOOGLE_CLOUD = {
    'google-bigquery': ('Google BigQuery', '数据集、表与 SQL 分析', 'bigquery.googleapis.com/mcp', '企业与数据'),
    'google-cloud-storage': ('Google Cloud Storage', '存储桶、对象与元数据', 'storage.googleapis.com/storage/mcp', '开发与云服务'),
    'google-cloud-run': ('Google Cloud Run', '无服务器服务、作业与部署', 'run.googleapis.com/mcp', '开发与云服务'),
    'google-compute': ('Google Compute Engine', '虚拟机、磁盘等计算资源', 'compute.googleapis.com/mcp', '开发与云服务'),
    'google-gke': ('Google Kubernetes Engine', 'Kubernetes 集群与节点池', 'container.googleapis.com/mcp', '开发与云服务'),
    'google-cloud-sql': ('Google Cloud SQL', '托管数据库、实例与备份', 'sqladmin.googleapis.com/mcp', '企业与数据'),
    'google-firestore': ('Google Firestore', '集合、文档与查询', 'firestore.googleapis.com/mcp', '企业与数据'),
    'google-alloydb': ('Google AlloyDB', 'PostgreSQL 集群与实例', 'alloydb.googleapis.com/mcp', '企业与数据'),
    'google-spanner': ('Google Spanner', '数据库实例、架构与查询', 'spanner.googleapis.com/mcp', '企业与数据'),
    'google-bigtable': ('Google Bigtable', '实例、集群与表', 'bigtableadmin.googleapis.com/mcp', '企业与数据'),
    'google-knowledge-catalog': ('Google Knowledge Catalog', '数据目录、元数据与发现', 'dataplex.googleapis.com/mcp', '企业与数据'),
    'google-logging': ('Google Cloud Logging', '查询和组织云日志', 'logging.googleapis.com/mcp', '开发与云服务'),
    'google-monitoring': ('Google Cloud Monitoring', '指标、告警和运行状态', 'monitoring.googleapis.com/mcp', '开发与云服务'),
    'google-error-reporting': ('Google Error Reporting', '错误事件与错误分组', 'clouderrorreporting.googleapis.com/mcp', '开发与云服务'),
    'google-cloud-trace': ('Google Cloud Trace', '应用追踪与延迟分析', 'cloudtrace.googleapis.com/mcp', '开发与云服务'),
    'google-pubsub': ('Google Pub/Sub', '消息主题与订阅', 'pubsub.googleapis.com/mcp', '开发与云服务'),
    'google-firebase-sql': ('Google Firebase SQL Connect', 'Firebase SQL 数据连接（预览）', 'firebasedataconnect.googleapis.com/mcp', '企业与数据'),
    'google-resource-manager': ('Google Resource Manager', '云项目、文件夹和组织', 'cloudresourcemanager.googleapis.com/mcp', '开发与云服务'),
    'google-cloud-quotas': ('Google Cloud Quotas', '查询云服务配额', 'cloudquotas.googleapis.com/mcp', '开发与云服务'),
}

# key: (name, description, endpoint, credential label, header, prefix, docs, category, icon)
US_SERVICES = {
    'google-maps': ('Google Maps', '地点、沿途搜索、路线规划、天气和地图链接',
                    'https://mapstools.googleapis.com/mcp', 'Google Maps Platform API Key', 'X-Goog-Api-Key', '',
                    'https://developers.google.com/maps/ai/maps-experience-agent-tools', '地图与出行', 'google.svg'),
    'google-maps-code': ('Google Maps Code Assist', 'Google Maps Platform 开发文档（预览）',
                         'https://mapscodeassist.googleapis.com/mcp', '', '', '',
                         'https://developers.google.com/maps/ai/code-assist', '开发与云服务', 'google.svg'),
    'cloudflare': ('Cloudflare', 'Workers、DNS、R2 和开发文档',
                   'https://mcp.cloudflare.com/mcp', 'Cloudflare API Token', 'Authorization', 'Bearer ',
                   'https://developers.cloudflare.com/agents/model-context-protocol/cloudflare/servers-for-cloudflare/',
                   '开发与云服务', ''),
    'tavily': ('Tavily', '网页搜索、内容提取和站点研究',
               'https://mcp.tavily.com/mcp', 'Tavily API Key', 'Authorization', 'Bearer ',
               'https://github.com/tavily-ai/tavily-mcp/blob/main/README.md', '搜索与信息', ''),
    'gitlab': ('GitLab.com', '仓库、合并请求与议题',
               'https://gitlab.com/api/v4/mcp', '', 'Authorization', 'Bearer ',
               'https://docs.gitlab.com/user/model_context_protocol/mcp_server/', '开发与云服务', ''),
    'figma': ('Figma', '设计上下文、组件与设计变量',
              'https://mcp.figma.com/mcp', '', 'Authorization', 'Bearer ',
              'https://developers.figma.com/docs/figma-mcp-server/remote-server-installation/', '办公与知识', ''),
    'deepwiki': ('DeepWiki', '公开 GitHub 仓库的文档、架构和问答',
                 'https://mcp.deepwiki.com/mcp', '', '', '',
                 'https://cognition.com/blog/deepwiki-mcp-server', '开发与云服务', ''),
    'netlify': ('Netlify', '站点、部署、构建日志和项目管理',
                'https://mcp.netlify.com/mcp', '', 'Authorization', 'Bearer ',
                'https://docs.netlify.com/build/build-with-ai/agent-setup-guides/agent-setup-overview/',
                '开发与云服务', ''),
    'huggingface': ('Hugging Face', '搜索模型、数据集、Spaces 与论文',
                    'https://huggingface.co/mcp', 'Hugging Face Access Token', 'Authorization', 'Bearer ',
                    'https://github.com/huggingface/hf-mcp-server', '开发与云服务', ''),
    'posthog': ('PostHog', '产品分析、错误追踪与功能开关',
                'https://mcp.posthog.com/mcp', '', 'Authorization', 'Bearer ',
                'https://posthog.com/docs/model-context-protocol', '企业与数据', ''),
}

US_SERVICES.update({
    key: (name, detail, 'https://' + endpoint, '', 'Authorization', 'Bearer ',
          CLOUD_DOCS, category, 'google.svg')
    for key, (name, detail, endpoint, category) in GOOGLE_CLOUD.items()
})
