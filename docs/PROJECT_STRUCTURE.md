# AI API Proxy - 项目结构

## 项目概述
AI API Proxy 是一个灵活的代理服务器，用于将自定义端点映射到 OpenAI 兼容的 API，并提供实时监控功能。

## 目录结构
```
proj/api-proxy/
├── proxy_server.py              # 主代理服务器 (Python Flask + SocketIO)
├── proxy_config.example.json    # 配置文件模板（提交到仓库）
├── proxy_config.json            # 实际配置文件（.gitignore 排除，不入库）
├── monitor.html                 # 监控面板 Web 界面
├── requirements.txt             # Python 依赖
├── README.md                    # 英文文档
├── README_zh.md                 # 中文文档
├── docs/                        # 项目文档目录
│   ├── FUNCTIONAL_SPEC.md       # 功能规格说明
│   ├── DEPLOYMENT_GUIDE.md      # 部署指南
│   ├── PROJECT_STRUCTURE.md     # 项目结构（本文件）
│   ├── USAGE_EXAMPLES.md        # 使用示例
│   └── SCREENSHOTS.md           # 界面截图说明
├── screenshots/                 # 截图存储目录
├── start_in_screen.sh           # Screen 启动脚本
├── api-proxy.service            # systemd 服务配置
├── api-proxy-screen.service     # Screen 服务配置
├── test_routing_logic.py        # 路由逻辑测试
├── merge_opencode_db.py         # ccusage 数据库合并工具
└── ccusage_day_cache.json       # ccusage 用量缓存（.gitignore 排除）
```

## 核心文件说明

### proxy_server.py
主代理服务器实现，包含：
- Flask Web 服务器
- WebSocket 实时通信
- 多端点代理逻辑
- 模型管理功能
- 请求路由机制
- 监控数据推送

### proxy_config.json
配置文件，定义：
- 代理端点映射
- 目标 API 基础 URL
- API 密钥配置
- 模型显示设置
- 模型路由设置
- 模型重定向规则

### monitor.html
实时监控面板，提供：
- 活跃连接监控
- 流数据实时显示
- 令牌使用统计
- 模型路由配置
- 模型重定向设置
- 推理性能测试（「性能测试」标签页）

## 配置详解

### 端点配置
```json
{
  "proxy_path_prefix": "/provider-name",    // 代理路径前缀
  "target_base_url": "https://api.example.com/v1/", // 目标 API 基础 URL
  "api_key": "sk-...",                      // 端点密钥，默认以 Authorization: Bearer 发送
  "api_key_header": "Authorization",        // 可选：自定义密钥头部
  "api_key_prefix": "Bearer ",              // 可选：自定义密钥前缀（空字符串表示无前缀）
  "models": [...]                           // 静态模型列表（可选）
}
```

### 全局配置
```json
{
  "port": 16900,                            // 代理与监控面板共用端口
  "ray_dashboard": "http://127.0.0.1:8265", // Ray 集群面板（性能测试自动填充硬件信息）
  "prefix_map": [["grok", "Grok"]]          // 模型名前缀归类（子串匹配，不区分大小写）
}
```

### 模型管理配置
```json
{
  "model_display_settings": {               // 模型显示设置
    "model-name": true/false               // 是否在聚合列表中显示
  },
  "model_routing_settings": {               // 模型路由设置
    "model-name": "/endpoint-path"         // 特定模型的路由规则
  },
  "model_redirects": {                      // 模型重定向
    "original-model": "target-model"       // 模型名称重定向
  },
  "model_vision_redirects": {               // 视觉模型重定向
    "original-model": "target-vision-model"
  },
  "model_vision_disabled": ["model-name"],  // 禁用视觉重定向的模型
  "model_access_rules": [                   // 按 Key/IP 的模型可见性规则
    {
      "name": "guest-client",               // 可选，仅用于监控日志展示
      "api_keys": ["sk-xxxx"],              // 匹配 Bearer Token 或 x-api-key
      "ips": ["192.0.2.23", "10.0.0.0/8"],  // 精确 IP 或 CIDR
      "models": ["GLM/*", "kimi-*"]         // 允许列表，支持 * 通配符，不区分大小写
    }
  ]
}
```

## 部署方式

### 1. 直接运行
```bash
python proxy_server.py
```

### 2. Screen 后台运行
```bash
screen -S api-proxy
python proxy_server.py
# Ctrl+A, D (分离会话)
```

### 3. Systemd 服务
```bash
sudo cp api-proxy.service /etc/systemd/system/
sudo systemctl enable api-proxy
sudo systemctl start api-proxy
```

## API 接口

### 代理接口
- `/{endpoint-prefix}/*` - 特定提供商的原始接口
- `/v1/models` - 聚合模型列表
- `/v1/chat/completions` - 统一聊天接口

### 监控接口
- `/monitor` - Web 监控面板
- WebSocket `/socket.io/` - 实时数据推送

## 环境变量
- `CONFIG_PATH` - 配置文件路径 (默认: ./proxy_config.json)

## 许可证
MIT License