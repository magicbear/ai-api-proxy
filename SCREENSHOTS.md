# AI API Proxy - 界面截图说明

## 1. 监控面板概览

![API Proxy Monitor Dashboard](screenshots/dashboard-overview.png)

监控面板提供了一个统一视图，显示所有活跃的连接和流数据。顶部的状态指示器显示 WebSocket 连接状态，统计数据卡片显示当前活跃连接数、流数量等关键指标。

## 2. 连接监控界面

![Connections Panel](screenshots/connections-panel.png)

连接面板实时显示所有传入的 API 请求，包括请求方法、URL、端点信息、目标 URL 和时间戳。每条记录还显示请求的持续时间和来源 IP 地址。

## 3. 流数据监控

![Stream Data Panel](screenshots/stream-data-panel.png)

流数据面板专门用于监控流式响应，显示当前活跃的流、它们的状态和实时传输的数据内容。这有助于调试流式 API 的性能和内容。

## 4. 统计信息页面

![Statistics Page](screenshots/statistics-page.png)

统计页面提供详细的令牌使用情况，按提供商分组显示提示令牌、补全令牌和总令牌的使用量。这对于成本管理和资源规划非常有用。

## 5. 模型路由配置

![Model Routing Config](screenshots/model-routing-config.png)

模型路由配置页面允许管理员查看所有可用模型，并配置它们的路由规则。可以设置特定模型应该路由到哪个后端服务，以及是否在聚合模型列表中显示。

## 6. 模型重定向功能

![Model Redirect](screenshots/model-redirect.png)

模型重定向功能允许将一个模型名称映射到另一个模型名称，这在需要标准化模型命名或进行模型迁移时非常有用。

## 7. 移动端适配

![Mobile View](screenshots/mobile-view.png)

监控面板完全响应式设计，在移动设备上也能正常显示，方便随时监控 API 代理的状态。