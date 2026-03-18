# AI API Proxy - 项目结构

## 项目概述
AI API Proxy 是一个灵活的代理服务器，用于将自定义端点映射到 OpenAI 兼容的 API，并提供实时监控功能。

## 目录结构
```
proj/api-proxy/
├── proxy_server.py              # 主代理服务器 (Python Flask + SocketIO)
├── proxy_config.json           # 代理配置文件
├── monitor.html                # 监控面板 Web 界面
├── requirements.txt            # Python 依赖
├── LICENSE                     # MIT 许可证
├── README.md                   # 英文文档
├── README_zh.md                # 中文文档
├── GITHUB_README.md            # GitHub 仓库首页
├── FUNCTIONAL_SPEC.md          # 功能规格说明
├── DEPLOYMENT_GUIDE.md         # 部署指南
├── USAGE_EXAMPLES.md           # 使用示例
├── SCREENSHOTS.md              # 界面截图说明
├── screenshots/                # 截图存储目录
├── setup.sh                    # 安装脚本
├── start_in_screen.sh          # Screen 启动脚本
├── update_and_start.sh         # 更新并启动脚本
├── api-proxy.service           # systemd 服务配置
├── api-proxy-screen.service    # Screen 服务配置
├── __pycache__/                # Python 缓存目录
├── test_*                      # 测试文件
└── *.py                        # 其他 Python 脚本
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

## 配置详解

### 端点配置
```json
{
  "proxy_path_prefix": "/provider-name",    // 代理路径前缀
  "target_base_url": "https://api.example.com/v1/", // 目标 API 基础 URL
  "api_key_header": "Authorization",        // API 密钥头部
  "api_key_prefix": "Bearer ",              // API 密钥前缀
  "api_key_env": "PROVIDER_API_KEY",        // 环境变量名
  "models": [...]                          // 静态模型列表（可选）
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
  }
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
- `{PROVIDER}_API_KEY` - 各提供商的 API 密钥

## 许可证
MIT License