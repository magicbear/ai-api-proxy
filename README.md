# OpenAI-Compatible API Proxy

A flexible proxy server that maps custom endpoints to OpenAI-compatible APIs (like Alibaba Cloud's DashScope) with real-time monitoring capabilities.

## Features

- Map custom paths to target API endpoints (e.g., `/bailian/chat/completions` → `https://dashscope.aliyuncs.com/v1/chat/completions`)
- Support for multiple endpoints
- Streaming response handling with real-time monitoring
- Web interface to monitor active connections and stream data
- WebSocket-based live updates
- Automatic modification of requests to include `stream_options.include_usage`

## Installation

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Configure the proxy by editing `proxy_config.json`:
```json
{
  "endpoints": [
    {
      "proxy_path_prefix": "/bailian/chat/completions",
      "target_base_url": "https://dashscope.aliyuncs.com/v1/chat/completions",
      "api_key_header": "Authorization",
      "api_key_prefix": "Bearer ",
      "api_key_env": "BAILIAN_API_KEY"
    }
  ],
  "port": 16900
}
```

3. Set your API keys as environment variables:
```bash
export BAILIAN_API_KEY="your-bailian-key"
export QWEN_API_KEY="your-qwen-key"
export OPENAI_API_KEY="your-openai-key"
```

4. Start the server:
```bash
python proxy_server.py
```

## Usage

- Proxy server runs on port 16900 (configurable), bound to 0.0.0.0
- Monitor interface available at `http://YOUR_SERVER_IP:16900/monitor` (same port)
- Make requests to your mapped endpoints (e.g., `POST http://YOUR_SERVER_IP:16900/bailian/chat/completions`)

## Configuration Options

The proxy reads from `proxy_config.json` with the following structure:

- `endpoints`: Array of endpoint mappings
  - `proxy_path_prefix`: Base path on the proxy server (e.g., `/bailian`)
  - `target_base_url`: Base URL of the target API (e.g., `https://dashscope.aliyuncs.com/v1/`)
  - `api_key_header`: Header name for API key (default: Authorization)
  - `api_key_prefix`: Prefix for API key value (default: Bearer )
  - `api_key_env`: Name of environment variable containing the API key
  - `models`: Optional static models array for the endpoint
- `port`: Port for both the proxy server and monitoring interface

## How It Works

1. The proxy receives requests at configured paths
2. It forwards requests to the target API, adding authentication headers
3. For streaming requests, it modifies the request body to include `stream_options.include_usage: true`
4. Real-time information is broadcast via WebSocket to monitoring clients
5. The web interface displays active connections and streaming data
6. Supports aggregated endpoints at `/v1/chat/completions` and `/v1/models` that route to appropriate backends

## Environment Variables

- `BAILIAN_API_KEY`: Authentication key for Bailian API
- `QWEN_API_KEY`: Authentication key for Qwen API
- `OPENAI_API_KEY`: Authentication key for OpenAI API
- `CONFIG_PATH`: Path to config file (default: ./proxy_config.json)

## Architecture

- `proxy_server.py`: Main proxy server with WebSocket broadcasting
- `proxy_config.json`: Endpoint configuration
- `monitor.html`: Web interface for monitoring
- `requirements.txt`: Dependencies

## Advanced Features

### Model Routing
The proxy supports dynamic model routing, allowing you to:
- View all available models from all configured endpoints
- Route specific models to specific backends
- Hide/show models in the aggregated `/v1/models` endpoint
- Set up model redirects to map one model name to another

### Monitoring Dashboard
Access the monitoring dashboard at `http://YOUR_SERVER_IP:16900/monitor` to:
- View active connections and their details
- Monitor streaming requests in real-time
- Track token usage by provider
- Configure model routing and display settings

### Aggregated Endpoints
The proxy provides unified endpoints:
- `/v1/models`: Returns models from all configured endpoints
- `/v1/chat/completions`: Routes to the appropriate backend based on the model name