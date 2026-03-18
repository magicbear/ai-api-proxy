# AI API Proxy - 功能规格说明书

## 1. 项目概述

AI API Proxy 是一个高度可配置的代理服务器，旨在统一多个 AI 模型提供商的 API 接口。它支持 OpenAI 兼容的接口格式，并提供统一的访问入口、实时监控和智能路由功能。

## 2. 核心功能

### 2.1 多端点代理
- **功能描述**: 将不同的 API 请求路径映射到相应的后端服务
- **实现方式**: 通过配置文件定义多个端点，每个端点对应一个后端 API 提供商
- **示例配置**:
  ```json
  {
    "endpoints": [
      {
        "proxy_path_prefix": "/xai",
        "target_base_url": "http://192.168.32.253/grok/",
        "api_key_header": "Authorization",
        "api_key_prefix": "Bearer ",
        "api_key_env": "BAILIAN_API_KEY"
      },
      {
        "proxy_path_prefix": "/bailian",
        "target_base_url": "http://192.168.32.253/bailian/",
        "api_key_header": "",
        "api_key_prefix": "",
        "api_key_env": "",
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
    ]
  }
  ```

### 2.2 统一 API 接口
- **聚合模型列表**: 通过 `/v1/models` 提供所有后端服务的模型列表
- **统一聊天接口**: 通过 `/v1/chat/completions` 自动路由到相应后端
- **自动模型重定向**: 支持将模型名称映射到实际后端模型

### 2.3 实时监控系统
- **WebSocket 实时通信**: 所有连接和流数据通过 WebSocket 实时推送
- **连接追踪**: 显示所有活跃连接的状态、来源和持续时间
- **流数据监控**: 实时显示流式响应的内容
- **令牌统计**: 按提供商分类统计令牌使用情况

### 2.4 智能路由机制
- **模型路由**: 根据模型名称自动选择合适的后端
- **自定义路由**: 支持手动配置特定模型的路由规则
- **负载均衡**: 在多个可用端点间分配请求

### 2.5 模型管理
- **静态模型配置**: 支持预定义模型列表
- **动态模型获取**: 从后端服务动态获取最新模型列表
- **模型显示控制**: 可隐藏/显示特定模型
- **模型重定向**: 将一个模型名称重定向到另一个模型

## 3. 技术架构

### 3.1 技术栈
- **后端**: Python Flask + Flask-SocketIO
- **前端**: HTML/CSS/JavaScript with Socket.IO client
- **协议**: WebSocket for real-time updates, HTTP for API calls
- **依赖库**: requests, Flask-SocketIO, eventlet

### 3.2 数据流
```
Client Request -> Proxy Router -> Backend Selection -> Request Forwarding
      ^                                                      |
      |                                                      v
      |---------------------------------- WebSocket Broadcast