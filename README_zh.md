# AI API Proxy

一个高度可配置的 OpenAI 兼容 API 代理，将多个 AI 模型提供商统一到单一入口，提供实时监控、用量统计和推理性能测试能力。

> English documentation: [README.md](README.md)

## 功能特性

- **多端点代理** —— 将自定义路径前缀（如 `/bailian`、`/local-vllm`）映射到任意 OpenAI 兼容后端
- **聚合接口** —— 统一的 `/v1/models` 与 `/v1/chat/completions`，按模型名称在多个后端间自动路由
- **模型管理** —— 显示开关、按模型路由、模型名称重定向、视觉模型重定向
- **按 Key/IP 的访问控制** —— 控制每个 API Key 或客户端 IP 可见的模型（支持通配符）
- **实时监控面板** —— 通过 WebSocket 展示活跃连接、流式数据与令牌用量统计
- **推理性能测试** —— 在监控界面直接对端点压测（预填充/输出耗时与速度、并发吞吐量，支持导出 PNG/CSV/Markdown，可从 Ray 集群自动填充硬件信息）
- **ccusage 用量统计** —— 基于 [ccusage](https://github.com/ryoppippi/ccusage) 的每日 token 用量统计

## 快速开始

```bash
pip install -r requirements.txt
cp proxy_config.example.json proxy_config.json   # 然后编辑该文件
python proxy_server.py
```

- 代理服务：`http://0.0.0.0:16900`（端口可配置）
- 监控面板：`http://localhost:16900/monitor`

`proxy_config.json` 包含密钥等隐私信息，已被 `.gitignore` 排除，请只在本地编辑。

## 配置说明

代理从 `proxy_config.json` 读取配置（可通过环境变量 `CONFIG_PATH` 覆盖路径）。
带完整注释的示例见 [`proxy_config.example.json`](proxy_config.example.json)。

```jsonc
{
  "port": 16900,                              // 代理与监控面板共用端口
  "ray_dashboard": "http://127.0.0.1:8265",   // 可选：Ray 面板地址，用于性能测试自动填充硬件信息

  "endpoints": [
    {
      "proxy_path_prefix": "/bailian",        // 对外暴露的路径前缀
      "target_base_url": "https://dashscope.aliyuncs.com/v1/",
      "api_key": "sk-your-api-key",           // 默认以 "Authorization: Bearer <key>" 发送
      "api_key_header": "Authorization",      // 可选：自定义密钥头部
      "api_key_prefix": "Bearer ",            // 可选：自定义前缀（空字符串表示无前缀）
      "models": ["model-a", "model-b"]        // 可选：静态模型列表
    }
  ],

  // 模型名前缀归类：子串匹配、不区分大小写，用于在聚合视图中按提供商分组
  "prefix_map": [["grok", "Grok"], ["deepseek", "Deepseek"]],

  // 在聚合的 /v1/models 列表中显示/隐藏模型
  "model_display_settings": { "model-a": true, "model-b": false },

  // 将某个模型固定路由到指定端点前缀
  "model_routing_settings": { "model-a": "/local-vllm" },

  // 转发上游前重写请求的模型名称
  "model_redirects": { "model-alias": "model-a" },

  // 将视觉请求路由到不同的模型
  "model_vision_redirects": { "model-a": "model-a-vision" },
  "model_vision_disabled": ["model-a"],

  // 每个 Key/IP 允许调用的模型（允许列表，支持 * 通配符，不区分大小写）
  "model_access_rules": [
    {
      "name": "guest-client",                 // 可选，仅用于监控日志展示
      "api_keys": ["sk-guest-key"],           // 匹配 Authorization Bearer 或 x-api-key
      "ips": ["127.0.0.1", "10.0.0.0/8"],     // 精确 IP 或 CIDR
      "models": ["model-a", "model-*"]
    }
  ]
}
```

## API 接口

| 路由 | 说明 |
| --- | --- |
| `/v1/models` | 所有端点的聚合模型列表 |
| `/v1/chat/completions` | 统一聊天接口，按模型名称路由 |
| `/v1/embeddings` | Embeddings 代理 |
| `/v1/audio/*` | 语音合成 / 语音转写 / 翻译代理 |
| `/<prefix>/v1/...` | 直连某个具体端点 |
| `/monitor` | Web 监控面板 |
| `/ccusage` | 基于 ccusage 的每日 token 用量统计 |
| `/ray_status` | Ray 集群状态（性能测试页使用） |

## 项目文档

| 文档 | 说明 |
| --- | --- |
| [docs/FUNCTIONAL_SPEC.md](docs/FUNCTIONAL_SPEC.md) | 功能规格说明书 |
| [docs/DEPLOYMENT_GUIDE.md](docs/DEPLOYMENT_GUIDE.md) | 部署指南：systemd、screen、nginx |
| [docs/PROJECT_STRUCTURE.md](docs/PROJECT_STRUCTURE.md) | 项目结构与配置详解 |
| [docs/USAGE_EXAMPLES.md](docs/USAGE_EXAMPLES.md) | 使用示例 |
| [docs/SCREENSHOTS.md](docs/SCREENSHOTS.md) | 监控界面截图说明 |

## 架构

- `proxy_server.py` —— Flask + Flask-SocketIO 服务：路由、模型管理、监控数据推送、性能测试
- `monitor.html` —— 单文件监控面板前端
- `proxy_config.json` —— 实际运行配置（不入库）
- `proxy_config.example.json` —— 提交到仓库的配置模板
- `requirements.txt` —— Python 依赖

## 许可证

MIT License
