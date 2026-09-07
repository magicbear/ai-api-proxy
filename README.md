# AI API Proxy

A highly configurable, OpenAI-compatible API proxy that unifies multiple AI model
providers behind a single entry point, with real-time monitoring, usage statistics,
and inference performance testing.

> 中文文档：[README_zh.md](README_zh.md)

## Features

- **Multi-endpoint proxying** — map custom path prefixes (e.g. `/bailian`,
  `/local-vllm`) to any OpenAI-compatible backend
- **Aggregated endpoints** — unified `/v1/models` and `/v1/chat/completions`
  that route by model name across all backends
- **Model management** — display toggles, per-model routing, name redirects,
  and vision-model redirects
- **Per-key / per-IP access rules** — control which models each API key or
  client IP can see (wildcard support)
- **Real-time monitoring dashboard** — active connections, live stream data,
  and token usage stats via WebSocket
- **Inference performance testing** — benchmark endpoints directly from the
  monitor UI (prefill/output latency & throughput, PNG/CSV/Markdown export,
  optional Ray cluster hardware auto-fill)
- **ccusage token statistics** — daily usage breakdown powered by
  [ccusage](https://github.com/ryoppippi/ccusage)

## Quick Start

```bash
pip install -r requirements.txt
cp proxy_config.example.json proxy_config.json   # then edit it
python proxy_server.py
```

- Proxy: `http://0.0.0.0:16900` (port configurable)
- Monitor dashboard: `http://localhost:16900/monitor`

`proxy_config.json` contains your keys and is excluded via `.gitignore` —
always edit it locally.

## Configuration

The proxy reads `proxy_config.json` (path overridable via the `CONFIG_PATH`
environment variable). Full annotated example: [`proxy_config.example.json`](proxy_config.example.json).

```jsonc
{
  "port": 16900,                              // proxy + monitor port
  "ray_dashboard": "http://127.0.0.1:8265",   // optional: Ray dashboard for perf-test hardware info

  "endpoints": [
    {
      "proxy_path_prefix": "/bailian",        // exposed path prefix
      "target_base_url": "https://dashscope.aliyuncs.com/v1/",
      "api_key": "sk-your-api-key",           // sent as "Authorization: Bearer <key>" by default
      "api_key_header": "Authorization",      // optional override
      "api_key_prefix": "Bearer ",            // optional override ("" = no prefix)
      "models": ["model-a", "model-b"]        // optional static model list
    }
  ],

  // Model-name prefix classification: substring match, case-insensitive.
  // Used to group models by provider in the aggregated views.
  "prefix_map": [["grok", "Grok"], ["deepseek", "Deepseek"]],

  // Hide/show models in the aggregated /v1/models list
  "model_display_settings": { "model-a": true, "model-b": false },

  // Pin a model to a specific endpoint prefix
  "model_routing_settings": { "model-a": "/local-vllm" },

  // Rewrite a requested model name before forwarding upstream
  "model_redirects": { "model-alias": "model-a" },

  // Route vision-capable requests to a different model
  "model_vision_redirects": { "model-a": "model-a-vision" },
  "model_vision_disabled": ["model-a"],

  // Which models each key/IP may call (allowlist, * wildcards, case-insensitive)
  "model_access_rules": [
    {
      "name": "guest-client",                 // optional, shown in monitor logs only
      "api_keys": ["sk-guest-key"],           // matches Authorization Bearer or x-api-key
      "ips": ["127.0.0.1", "10.0.0.0/8"],     // exact IP or CIDR
      "models": ["model-a", "model-*"]
    }
  ]
}
```

## API Endpoints

| Route | Description |
| --- | --- |
| `/v1/models` | Aggregated model list from all endpoints |
| `/v1/chat/completions` | Unified chat completions, routed by model name |
| `/v1/embeddings` | Embeddings proxy |
| `/v1/audio/*` | Speech / transcriptions / translations proxy |
| `/<prefix>/v1/...` | Direct access to a specific endpoint |
| `/monitor` | Web monitoring dashboard |
| `/ccusage` | ccusage-based daily token usage statistics |
| `/ray_status` | Ray cluster status (used by the performance test page) |

## Documentation

| Document | Description |
| --- | --- |
| [docs/FUNCTIONAL_SPEC.md](docs/FUNCTIONAL_SPEC.md) | Functional specification (Chinese) |
| [docs/DEPLOYMENT_GUIDE.md](docs/DEPLOYMENT_GUIDE.md) | Deployment guide: systemd, screen, nginx (Chinese) |
| [docs/PROJECT_STRUCTURE.md](docs/PROJECT_STRUCTURE.md) | Project layout & config reference (Chinese) |
| [docs/USAGE_EXAMPLES.md](docs/USAGE_EXAMPLES.md) | Usage examples (Chinese) |
| [docs/SCREENSHOTS.md](docs/SCREENSHOTS.md) | Monitor UI screenshots (Chinese) |

## Architecture

- `proxy_server.py` — Flask + Flask-SocketIO server: routing, model management,
  monitor broadcast, performance testing
- `monitor.html` — single-file monitoring dashboard UI
- `proxy_config.json` — live configuration (not committed)
- `proxy_config.example.json` — committed config template
- `requirements.txt` — Python dependencies

## License

MIT License
