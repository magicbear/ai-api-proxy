# 使用示例

## 1. 基本 API 调用

### 1.1 获取模型列表
```bash
curl http://your-server:16900/v1/models
```

### 1.2 聊天补全请求
```bash
curl -X POST http://your-server:16900/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-api-key" \
  -d '{
    "model": "qwen3-coder-plus",
    "messages": [
      {
        "role": "user",
        "content": "Hello, how are you?"
      }
    ],
    "stream": true
  }'
```

## 2. 特定提供商调用

### 2.1 调用百炼平台
```bash
curl -X POST http://your-server:16900/bailian/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-bailian-key" \
  -d '{
    "model": "qwen3.5-plus",
    "messages": [
      {
        "role": "user",
        "content": "Explain quantum computing in simple terms."
      }
    ]
  }'
```

### 2.2 调用本地 VLLM 服务
```bash
curl -X POST http://your-server:16900/local-vllm/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "local-model-name",
    "messages": [
      {
        "role": "user",
        "content": "Write a Python function to calculate factorial."
      }
    ]
  }'
```

## 3. 模型路由示例

### 3.1 自动路由
当使用聚合端点 `/v1/chat/completions` 时，代理会根据模型名称自动选择适当的后端：

```bash
# 这个请求会被路由到 /bailian 端点
curl -X POST http://your-server:16900/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-api-key" \
  -d '{
    "model": "qwen3-coder-plus",
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

### 3.2 模型重定向
如果设置了模型重定向，请求会被重新映射到不同的模型：

```bash
# 如果 Qwen/Qwen3.5-9B 被重定向到另一个模型
curl -X POST http://your-server:16900/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-api-key" \
  -d '{
    "model": "Qwen/Qwen3.5-9B",  # 此模型名会被重定向
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

## 4. 流式响应示例

### 4.1 流式聊天补全
```bash
curl -X POST http://your-server:16900/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-api-key" \
  -d '{
    "model": "qwen3-coder-plus",
    "messages": [
      {
        "role": "user",
        "content": "Explain how neural networks work."
      }
    ],
    "stream": true,
    "stream_options": {
      "include_usage": true
    }
  }'
```

## 5. 监控 API 示例

### 5.1 WebSocket 连接
客户端可以通过 WebSocket 连接到 `ws://your-server:16900/socket.io/` 来接收实时更新。

### 5.2 监控面板
访问 `http://your-server:16900/monitor` 查看实时监控信息。

## 6. 高级配置示例

### 6.1 静态模型配置
在配置文件中为端点指定静态模型列表：

```json
{
  "proxy_path_prefix": "/bailian",
  "target_base_url": "http://192.168.32.253/bailian/",
  "models": [
    "qwen3.5-plus",
    "qwen3-max-2026-01-23",
    "qwen3-coder-next",
    "qwen3-coder-plus",
    "glm-5",
    "glm-4.7",
    "kimi-k2.5",
    "MiniMax-M2.5"
  ]
}
```

### 6.2 模型显示控制
在配置中控制哪些模型在聚合列表中可见：

```json
{
  "model_display_settings": {
    "grok-3": false,
    "deepseek-r1-distill-llama-70b": false,
    "qwen-coder-plus": true
  }
}
```

### 6.3 自定义路由
指定特定模型应路由到的端点：

```json
{
  "model_routing_settings": {
    "qwen/qwen3.5-plus": "/bailian",
    "GLM/glm-5": "/bailian",
    "Qwen/qwen3.5-122b-a10b": "/local-sgl"
  }
}
```