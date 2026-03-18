# 部署指南

## 1. 环境准备

### 1.1 系统要求
- Python 3.7+
- pip 包管理器
- 至少 512MB 可用内存

### 1.2 安装依赖
```bash
# 克隆项目
git clone https://github.com/magicbear/ai-api-proxy.git
cd ai-api-proxy

# 安装 Python 依赖
pip install -r requirements.txt
```

## 2. 配置设置

### 2.1 配置文件
编辑 `proxy_config.json` 文件，根据您的需求配置端点：

```json
{
  "endpoints": [
    {
      "proxy_path_prefix": "/my-provider",
      "target_base_url": "https://api.provider.com/v1/",
      "api_key_header": "Authorization",
      "api_key_prefix": "Bearer ",
      "api_key_env": "MY_PROVIDER_API_KEY"
    }
  ],
  "port": 16900,
  "model_display_settings": {},
  "model_routing_settings": {},
  "model_redirects": {}
}
```

### 2.2 环境变量
设置必要的环境变量：

```bash
export MY_PROVIDER_API_KEY="your-api-key-here"
```

## 3. 启动服务

### 3.1 直接启动
```bash
python proxy_server.py
```

### 3.2 使用 Screen 后台运行
```bash
# 创建新的 screen 会话
screen -S api-proxy

# 在 screen 会话中运行
python proxy_server.py

# 按 Ctrl+A, 然后按 D 分离会话
```

### 3.3 使用 systemd 服务（推荐生产环境）
创建服务文件 `api-proxy.service`：

```ini
[Unit]
Description=AI API Proxy Service
After=network.target

[Service]
Type=simple
User=your-user
WorkingDirectory=/path/to/ai-api-proxy
ExecStart=/usr/bin/python3 /path/to/ai-api-proxy/proxy_server.py
Restart=always
Environment=MY_PROVIDER_API_KEY=your-api-key-here

[Install]
WantedBy=multi-user.target
```

启用并启动服务：
```bash
sudo cp api-proxy.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable api-proxy
sudo systemctl start api-proxy
```

## 4. 访问服务

- API 代理: `http://your-server-ip:16900`
- 监控面板: `http://your-server-ip:16900/monitor`

## 5. 配置反向代理（可选）

### 5.1 Nginx 配置
```nginx
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://127.0.0.1:16900;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

## 6. 安全建议

- 使用防火墙限制对代理端口的访问
- 使用 HTTPS 保护数据传输
- 定期轮换 API 密钥
- 监控访问日志

## 7. 故障排除

### 7.1 检查服务状态
```bash
# 如果使用 systemd
systemctl status api-proxy

# 检查端口占用
netstat -tlnp | grep 16900

# 查看日志
journalctl -u api-proxy -f
```

### 7.2 常见问题
- 确保目标 API 服务可访问
- 检查 API 密钥是否正确
- 验证网络连接和防火墙设置