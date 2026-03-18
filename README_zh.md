# OpenAI 兼容 API 代理

一个灵活的代理服务器，将自定义端点映射到 OpenAI 兼容的 API（如阿里云的通义千问）并提供实时监控功能。

## 功能特性

- 将自定义路径映射到目标 API 端点（例如 `/bailian/chat/completions` → `https://dashscope.aliyuncs.com/v1/chat/completions`）
- 支持多个端点
- 流式响应处理与实时监控
- 用于监控活动连接和流数据的 Web 界面
- WebSocket 实时更新
- 自动修改请求以包含 `stream_options.include_usage`

## 安装

1. 安装依赖：
```bash
pip install -r requirements.txt
```

2. 通过编辑 `proxy_config.json` 配置代理：
```json
{
  "endpoints": [
    {
      "proxy_path_prefix": "/bailian/chat/completions",
      "target_url": "https://dashscope.aliyuncs.com/v1/chat/completions",
      "api_key_header": "Authorization",
      "api_key_prefix": "Bearer "
    }
  ],
  "port": 8080,
  "monitor_port": 8081
}
```

3. 设置环境变量中的 API 密钥：
```bash
export BAILIAN_API_KEY="your-bailian-key"
export QWEN_API_KEY="your-qwen-key"
export OPENAI_API_KEY="your-openai-key"
```

4. 启动服务器：
```bash
python proxy_server.py
```

## 使用方法

- 代理服务器在端口 16900 上运行（可配置），绑定到 0.0.0.0
- 监控界面可在 `http://YOUR_SERVER_IP:16900/monitor` 访问（同端口）
- 向映射的端点发出请求（例如 `POST http://YOUR_SERVER_IP:16900/bailian/chat/completions`）

## 配置选项

代理从 `proxy_config.json` 读取配置，结构如下：

- `endpoints`: 端点映射数组
  - `proxy_path`: 代理服务器上的路径
  - `target_url`: 目标 API 的完整 URL
  - `api_key_header`: API 密钥的头部名称（默认：Authorization）
  - `api_key_prefix`: API 密钥值的前缀（默认：Bearer ）
  - `api_key_env`: 包含 API 密钥的环境变量名称
- `port`: 代理服务器和监控界面的端口

## 工作原理

1. 代理接收配置路径上的请求
2. 它将请求转发到目标 API，添加身份验证头
3. 对于流式请求，它修改请求体以包含 `stream_options.include_usage: true`
4. 通过 WebSocket 将实时信息广播给监控客户端
5. Web 界面显示活动连接和流数据

## 环境变量

- `BAILIAN_API_KEY`: 百炼 API 的身份验证密钥
- `QWEN_API_KEY`: 通义 API 的身份验证密钥
- `OPENAI_API_KEY`: OpenAI API 的身份验证密钥
- `CONFIG_PATH`: 配置文件路径（默认：./proxy_config.json）

## 架构

- `proxy_server.py`: 主代理服务器与 WebSocket 广播
- `proxy_config.json`: 端点配置
- `monitor.html`: 监控用 Web 界面
- `requirements.txt`: 依赖项