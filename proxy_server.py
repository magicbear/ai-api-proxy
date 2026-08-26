#!/usr/bin/env python3
"""
OpenAI-compatible API Proxy Server with Model Redirect Feature
Supports multiple endpoints with different API keys and real-time monitoring
"""

import json
import os
import requests
from flask import Flask, request, jsonify, send_from_directory, Response
from flask_socketio import SocketIO, emit
from werkzeug.serving import make_server
from threading import Thread, Lock
import time
from urllib.parse import urljoin, urlparse
from datetime import datetime
import logging

# Configure logging
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

def _fix_sse_double_data(chunk_str):
    """Aggressively remove any "data: data:" prefixes from SSE chunks."""
    if not isinstance(chunk_str, str):
        return chunk_str
    original = chunk_str
    # Simple but effective: repeatedly replace the bad pattern
    while "data: data: " in chunk_str:
        chunk_str = chunk_str.replace("data: data: ", "data: ")
    # Also catch without trailing space after second data
    while "data: data:" in chunk_str:
        chunk_str = chunk_str.replace("data: data:", "data:")
    if original != chunk_str:
        logger.warning("OpenClaw proxy: FIXED double 'data: ' prefix in SSE stream (aggressive)")
    return chunk_str

CONFIG_PATH = os.environ.get('CONFIG_PATH', './proxy_config.json')
CONFIG_FILE_LOCK = Lock()

# Timeouts for the upstream forwarding requests. Connect timeout should be
# short so a dead upstream fails fast instead of pinning a worker thread.
# Read timeout applies between chunks; for long streaming generations it may
# be left at 0 (no limit) so a slow-but-alive upstream is never cut off, or the
# caller's own read timeout is the real limit.
PROXY_CONNECT_TIMEOUT = float(os.environ.get('PROXY_CONNECT_TIMEOUT', '10') or 10)
PROXY_READ_TIMEOUT = float(os.environ.get('PROXY_READ_TIMEOUT', '0') or 0)

# State for distinguishing self-originated config writes from external edits.
# The file watcher reloads only when the on-disk content differs from both the
# config we last wrote ourselves and the config we last applied to memory.
_config_state = {
    'last_self_content': None,
    'last_loaded_content': None,
}


def read_config_file():
    """Read and parse proxy_config.json (thread-safe)."""
    with CONFIG_FILE_LOCK:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)


def write_config_file(config):
    """Atomically persist config and record it as a self-originated write.

    Writes to a temp file then os.replace()s it into place so a concurrent
    reader (config watcher, UI saves) never observes a half-written file.
    """
    tmp_path = CONFIG_PATH + '.tmp'
    with CONFIG_FILE_LOCK:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, CONFIG_PATH)
        _config_state['last_self_content'] = json.loads(json.dumps(config))


def resolve_api_key_headers(endpoint_config):
    """Build an auth header dict from a simplified endpoint api-key config.

    Just provide "api_key": "sk-..." and it is sent as "Authorization: Bearer sk-..."
    by default. The old "api_key_header"/"api_key_prefix" fields are honored as
    optional overrides (defaults: "Authorization" / "Bearer "). Returns {} when
    there is no usable key.
    """
    api_key = (endpoint_config.get('api_key') or '').strip()
    if not api_key:
        return {}
    header = (endpoint_config.get('api_key_header') or 'Authorization').strip() or 'Authorization'
    # Only default to "Bearer " when the field is absent; an explicit empty
    # string means "no prefix".
    raw_prefix = endpoint_config.get('api_key_prefix')
    prefix = 'Bearer ' if raw_prefix is None else (raw_prefix or '').strip()
    if prefix and not prefix.endswith(' '):
        prefix = prefix + ' '
    return {header: f"{prefix}{api_key}"}


# Thread-safe global variables for tracking connections and streams
active_connections_lock = Lock()
active_streams_lock = Lock()
token_stats_lock = Lock()
cached_models_lock = Lock()
model_routing_lock = Lock()
custom_model_routing_lock = Lock()
model_display_settings_lock = Lock()
model_cache_timestamps_lock = Lock()
model_redirects_lock = Lock()

# Server-side statistics tracking
server_stats_lock = Lock()
server_stats = {
    'active_connections': 0,
    'active_streams': 0,
    'total_messages': 0
}

# Global variables for tracking connections and streams
active_connections = {}
active_streams = {}
token_stats = {}  # Track token usage by provider
cached_models = {}  # Cache models from all providers
model_routing = {}  # Store routing configuration for models
custom_model_routing = {}  # Store custom routing overrides set by the UI
model_display_settings = {}  # Store model display settings
model_cache_timestamps = {}  # Store timestamps for model caches
model_redirects = {}  # Store model redirection mapping
model_vision_redirects = {}  # Store mapping of text-only model -> vision-capable model

# Initialize Flask app with SocketIO.
# static_folder is disabled: monitor.html is served via an explicit route and
# the implicit static catch-all (<path:filename>) would otherwise swallow
# generic proxy GET paths before the dynamic dispatcher sees them.
app = Flask(__name__, static_folder=None)
app.config['SECRET_KEY'] = 'your-secret-key-for-socketio'

# Set up logging for SocketIO to reduce verbosity
socketio_logger = logging.getLogger('socketio')
engineio_logger = logging.getLogger('engineio')
socketio_logger.setLevel(logging.ERROR)
engineio_logger.setLevel(logging.ERROR)

# Use threading mode for better concurrent handling
socketio = SocketIO(app, cors_allowed_origins="*", logger=False, engineio_logger=False, async_mode='threading')

# Explicitly serve Socket.IO client library
@app.route('/socket.io/socket.io.js')
def socket_io_js():
    return Response("""
        // Load Socket.IO from CDN
        (function() {
            var script = document.createElement('script');
            script.src = 'https://cdn.socket.io/4.7.2/socket.io.min.js';
            document.head.appendChild(script);
        })();
    """, mimetype='application/javascript')

class APIProxyServer:
    def __init__(self, config_path='./proxy_config.json'):
        self.load_config(config_path)
        self.setup_routes()

    def load_config(self, config_path):
        self.config = read_config_file()

        self.endpoints = self.config.get('endpoints', [])
        self.port = self.config.get('port', 16900)
        _config_state['last_loaded_content'] = json.loads(json.dumps(self.config))

    def setup_routes(self):
        # Serve monitor HTML
        @app.route('/monitor')
        def monitor():
            return send_from_directory('.', 'monitor.html')

        # Single dynamic dispatcher: matches incoming requests against the live
        # endpoint list from proxy_config.json, so adding/removing/editing
        # endpoints takes effect in real-time without a server restart.
        # Flask's router prefers more specific rules, so /monitor, /v1/models
        # and /v1/chat/completions registered elsewhere still take precedence.
        @app.route('/', defaults={'path': ''}, methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS'])
        @app.route('/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS'])
        def dynamic_proxy(path):
            matched = self.match_endpoint(path)
            if matched is None:
                logger.info(f"Dispatcher: no endpoint matched for path '{path}'")
                return jsonify({"error": "Endpoint not configured"}), 404
            endpoint, subpath = matched
            logger.info(f"Dispatcher: '{path}' -> {endpoint.get('proxy_path_prefix')} subpath='{subpath}'")
            return self.handle_proxy_request(request, endpoint, subpath)

    def match_endpoint(self, path):
        """Return (endpoint_config, subpath) for the longest matching prefix."""
        if path.startswith('/'):
            path = path[1:]
        best = None
        best_len = -1
        for ep in self.endpoints:
            prefix = ep.get('proxy_path_prefix', '')
            if not prefix:
                continue
            p = prefix.strip('/')
            if not p:
                continue
            if path == p or path.startswith(p + '/'):
                if len(p) > best_len:
                    best_len = len(p)
                    best = (ep, path[len(p):].lstrip('/'))
        return best

    def cleanup_connection(self, request_id, is_streaming=False):
        """Remove a tracked connection (and optional stream) and refresh stats.

        Used on every early-return / error path so a request can never leave a
        zombie entry in active_connections / active_streams (which would show up
        in the monitor as an impossibly long-lived connection).
        """
        with active_connections_lock:
            had_connection = request_id in active_connections
            if had_connection:
                del active_connections[request_id]
        with active_streams_lock:
            had_stream = is_streaming and request_id in active_streams
            if had_stream:
                del active_streams[request_id]

        try:
            if is_streaming and had_stream:
                socketio.emit('stream_finished', {
                    'id': request_id,
                    'timestamp': datetime.now().isoformat()
                })
            if had_connection:
                socketio.emit('connection_removed', {'id': request_id})
            socketio.emit('server_stats_update', {
                'active_connections': len(active_connections),
                'active_streams': len(active_streams),
                'total_messages': server_stats.get('total_messages', 0)
            })
        except Exception as e:
            logger.error(f"Failed to emit cleanup event for {request_id}: {e}")

    def handle_proxy_request(self, req, endpoint_config, subpath, original_model_id=None):
        request_id = f"{int(time.time())}-{hash(req.url) % 10000}"
        is_streaming = False

        # Track connection
        endpoint_prefix = endpoint_config.get('proxy_path_prefix', 'unknown')
        target_base_url = endpoint_config.get('target_base_url', 'unknown')

        # Extract model from request if it's a chat completion request
        model_name = None
        if req.is_json and ('/chat/completions' in req.full_path or '/v1/chat' in req.full_path):
            try:
                json_data = req.get_json()
                if json_data and isinstance(json_data, dict):
                    model_name = json_data.get('model', None)
            except:
                pass  # If JSON parsing fails, continue without model info

        # Calculate request size
        request_size = len(req.get_data()) if req.get_data() else 0

        connection_info = {
            'id': request_id,
            'method': req.method,
            'url': req.full_path,
            'timestamp': datetime.now().isoformat(),
            'start_time': time.time(),
            'headers': dict(req.headers),
            'remote_address': request.remote_addr,
            'endpoint': endpoint_prefix,
            'target_url': target_base_url,
            'model': model_name,  # Add model information to connection info
            'request_size': request_size  # Add request size in bytes
        }

        with active_connections_lock:
            active_connections[request_id] = connection_info

        # Update server stats
        with server_stats_lock:
            server_stats['active_connections'] = len(active_connections)
            server_stats['total_messages'] += 1

        try:
            socketio.emit('connection_added', {'connection': connection_info})
            # Broadcast updated stats to all clients
            socketio.emit('server_stats_update', {
                'active_connections': server_stats['active_connections'],
                'active_streams': server_stats['active_streams'],
                'total_messages': server_stats['total_messages']
            })
        except Exception as e:
            logger.error(f"Failed to emit connection_added event: {e}")

        # Process subpath
        processed_subpath = subpath
        if processed_subpath.startswith('v1/'):
            processed_subpath = processed_subpath[3:]
        elif processed_subpath.startswith('/v1/'):
            processed_subpath = processed_subpath[4:]

        # Determine target URL
        target_base = endpoint_config.get('target_base_url', '')

        # Check if this is a models request and if static models are configured
        is_models_request = processed_subpath == 'models' or processed_subpath == 'v1/models'
        static_models = endpoint_config.get('static_models') or endpoint_config.get('models')

        if is_models_request and static_models:
            # Add prefixes to static models for display, ensuring OpenAI-compatible format
            prefixed_static_models = []
            for model in static_models:
                if isinstance(model, str):
                    # If it's a string, add prefix if needed and convert to OpenAI format
                    model_id = model
                    if '/' not in model_id:
                        # Check if the model name matches any known prefixes
                        matched_prefix = get_model_prefix(model_id)
                        if matched_prefix:
                            prefixed_model_id = f"{matched_prefix}/{model_id}"
                        else:
                            # Use the source endpoint as a prefix if no specific prefix matches
                            source_endpoint = endpoint_config.get('proxy_path_prefix', 'unknown')
                            clean_prefix = source_endpoint.lstrip('/').replace('/', '-')
                            prefixed_model_id = f"{clean_prefix}/{model_id}"
                    else:
                        prefixed_model_id = model_id  # Already has prefix

                    # Create OpenAI-compatible model object
                    model_obj = {
                        "id": prefixed_model_id,
                        "object": "model",
                        "created": int(time.time()),
                        "owned_by": "organization-owner"  # Standard OpenAI format
                    }
                    prefixed_static_models.append(model_obj)
                else:
                    # If it's already an object, ensure it follows OpenAI format
                    model_obj = model.copy()
                    if 'id' not in model_obj:
                        # If no id field, treat the whole thing as an id
                        model_obj = {
                            "id": str(model),
                            "object": "model",
                            "created": int(time.time()),
                            "owned_by": "organization-owner"
                        }
                    else:
                        # Ensure required OpenAI fields are present
                        if 'object' not in model_obj:
                            model_obj['object'] = 'model'
                        if 'created' not in model_obj:
                            model_obj['created'] = int(time.time())
                        if 'owned_by' not in model_obj:
                            model_obj['owned_by'] = 'organization-owner'

                        # Add prefix to the id field if needed
                        model_id = model_obj['id']
                        if '/' not in model_id:
                            # Check if the model name matches any known prefixes
                            matched_prefix = get_model_prefix(model_id)
                            if matched_prefix:
                                prefixed_model_id = f"{matched_prefix}/{model_id}"
                            else:
                                # Use the source endpoint as a prefix if no specific prefix matches
                                source_endpoint = endpoint_config.get('proxy_path_prefix', 'unknown')
                                clean_prefix = source_endpoint.lstrip('/').replace('/', '-')
                                prefixed_model_id = f"{clean_prefix}/{model_id}"
                            model_obj['id'] = prefixed_model_id

                    prefixed_static_models.append(model_obj)

            self.cleanup_connection(request_id, is_streaming)
            return jsonify({
                "object": "list",
                "data": prefixed_static_models
            })

        # Check if this is a pure proxy endpoint without target_base_url
        if not target_base or target_base.strip() == '':
            # For pure proxy endpoints, we need to check if it's a models request
            # If it's not a models request, we should check for model-specific redirects
            if not is_models_request:
                # For non-models requests on pure proxy endpoints, we should check if there are model-specific redirects
                # First, get the model from the request data if it's a chat completion request
                if req.is_json and ('/chat/completions' in req.full_path or '/v1/chat/completions' in req.full_path):
                    try:
                        json_data = req.get_json()
                        if json_data and 'model' in json_data:
                            original_model = json_data['model']

                            # Check if there's a redirect for this model
                            redirect_target = check_model_redirect_for_pure_proxy(original_model)

                            if redirect_target:
                                # Redirect the request to the aggregated endpoint which will handle routing
                                # This allows the model to be handled by the appropriate backend
                                logger.info(f"Redirecting model {original_model} to {redirect_target}")
                                # Update the model in the request data
                                json_data['model'] = redirect_target
                                # Update the request data
                                data = json.dumps(json_data).encode('utf-8')
                                # Continue with the aggregated endpoint logic
                                # We'll handle this by calling the aggregated chat completions handler
                                request._cached_json = (True, json_data)  # Update request's cached JSON
                                # Now we need to route this to the appropriate backend based on the redirected model
                                # This requires a bit of manipulation to call the aggregated handler
                                # Instead, let's update the model and continue with the standard flow
                                # but note that this endpoint has no target_base_url, so we'll need to return an error
                                pass
                    except Exception as e:
                        logger.error(f"Error checking model redirect: {e}")

                # Return error for non-models requests on pure proxy endpoints without target
                self.cleanup_connection(request_id, is_streaming)
                return jsonify({
                    "error": "Pure proxy endpoint requires model-specific routing or target forwarding configuration",
                    "endpoint": endpoint_config['proxy_path_prefix'],
                    "configured_models": static_models or []
                }), 400
            else:
                # This is a models request, which is handled above
                pass

        # Only add 'v1/' prefix for specific OpenAI-compatible API endpoints
        if processed_subpath:
            if any(keyword in processed_subpath.lower() for keyword in ['chat', 'completions', 'embeddings', 'images', 'audio', 'moderations', 'models']):
                final_path = f'v1/{processed_subpath}'
            else:
                final_path = processed_subpath
        else:
            final_path = 'v1/'

        target_url = urljoin(target_base.rstrip('/') + '/', final_path.lstrip('/'))

        # Prepare headers
        headers = dict(req.headers)

        # Update Host header to match the target URL
        target_parsed = urlparse(target_url)
        headers['Host'] = target_parsed.netloc

        # Add API key if configured (simplified: just provide "api_key")
        try:
            for key_header, key_value in resolve_api_key_headers(endpoint_config).items():
                headers[key_header] = key_value
        except Exception as e:
            logger.warning(f"No API key found: {e}")

        # Prepare request data
        data = req.get_data()

        # Special handling for chat completions
        is_chat_completions = '/chat/completions' in req.full_path
        is_streaming = False

        if is_chat_completions and req.is_json:
            try:
                json_data = req.get_json()
                if json_data and isinstance(json_data, dict):
                    current_model = json_data.get('model', '')

                    # Handle model redirects
                    target_original_model_id = getattr(req, 'original_model_id', None)
                    if target_original_model_id:
                        json_data['model'] = target_original_model_id
                        logger.info(f"Using provided original model ID: {target_original_model_id}")
                    else:
                        # Handle redirects for direct endpoint requests
                        redirected_model = current_model
                        if current_model in model_redirects:
                            redirected_model = model_redirects[current_model]
                            logger.info(f"Exact redirect match: {current_model} -> {redirected_model}")
                        else:
                            # Try case-insensitive match
                            current_lower = current_model.lower()
                            for original, target in model_redirects.items():
                                if original.lower() == current_lower:
                                    redirected_model = target
                                    logger.info(f"Case-insensitive redirect match: {current_model} -> {redirected_model}")
                                    break

                        if redirected_model != current_model:
                            logger.info(f"Direct proxy request model redirect: {current_model} -> {redirected_model}")
                            if redirected_model in cached_models:
                                final_model = cached_models[redirected_model].get('original_id', redirected_model)
                                json_data['model'] = final_model
                                logger.info(f"Redirected model found in cache, using original_id: {final_model}")
                            else:
                                json_data['model'] = redirected_model
                        elif current_model and current_model in cached_models:
                            # Standard prefix-to-original restoration
                            original_model_from_cache = cached_models[current_model].get('original_id', current_model)
                            if original_model_from_cache != current_model:
                                json_data['model'] = original_model_from_cache
                                logger.info(f"Standard prefix-to-original restoration: {current_model} -> {original_model_from_cache}")

                    # Ensure data is updated with the final model selection
                    data = json.dumps(json_data).encode('utf-8')

                    if json_data and json_data.get('stream', False):
                        is_streaming = True

                        # Add stream_options.include_usage for chat completions
                        if 'stream_options' not in json_data:
                            json_data['stream_options'] = {}
                        json_data['stream_options']['include_usage'] = True

                        # Update the data
                        data = json.dumps(json_data).encode('utf-8')
            except Exception as e:
                logger.error(f"Error processing request JSON: {e}")

        if is_chat_completions and json_data and isinstance(json_data, dict) and json_data.get('model'):
            final_model = json_data['model']
            with active_connections_lock:
                if request_id in active_connections:
                    active_connections[request_id]['final_model'] = final_model

        # Track stream if applicable
        print(f"Handle proxy request -> is_streaming: {is_streaming}")
        if is_chat_completions and is_streaming:
            stream_original_model = current_model
            stream_final_model = json_data.get('model', current_model) if (json_data and isinstance(json_data, dict)) else current_model
            stream_info = {
                'id': request_id,
                'url': req.full_path,
                'timestamp': datetime.now().isoformat(),
                'start_time': time.time(),
                'status': 'started',
                'endpoint': endpoint_prefix,
                'target_url': target_base_url,
                'model': stream_original_model,
                'final_model': stream_final_model
            }
            with active_streams_lock:
                active_streams[request_id] = stream_info

            # Update server stats
            with server_stats_lock:
                server_stats['active_streams'] = len(active_streams)
                server_stats['total_messages'] += 1

            try:
                socketio.emit('stream_started', {'stream': stream_info})
                # Broadcast updated stats to all clients
                socketio.emit('server_stats_update', {
                    'active_connections': server_stats['active_connections'],
                    'active_streams': server_stats['active_streams'],
                    'total_messages': server_stats['total_messages']
                })
            except Exception as e:
                logger.error(f"Failed to emit stream_started event: {e}")

        try:
            # For streaming requests, we need to stream the response back
            headers['User-Agent'] = 'OpenAI/JS 6.26.0'

            # Upstream timeout: (connect, read). Read of 0 means no read limit,
            # so long streaming generations are never cut off by the proxy;
            # only a connection timeout is applied to fail dead upstreams fast.
            if PROXY_READ_TIMEOUT > 0:
                upstream_timeout = (PROXY_CONNECT_TIMEOUT, PROXY_READ_TIMEOUT)
            else:
                upstream_timeout = PROXY_CONNECT_TIMEOUT

            if req.method == 'GET':
                response = requests.get(target_url, headers=headers, params=req.args, stream=True, timeout=upstream_timeout)
            elif req.method == 'POST':
                response = requests.post(target_url, headers=headers, data=data, stream=True, timeout=upstream_timeout)
            elif req.method == 'PUT':
                response = requests.put(target_url, headers=headers, data=data, stream=True, timeout=upstream_timeout)
            elif req.method == 'DELETE':
                response = requests.delete(target_url, headers=headers, stream=True, timeout=upstream_timeout)
            elif req.method == 'PATCH':
                response = requests.patch(target_url, headers=headers, data=data, stream=True, timeout=upstream_timeout)
            else:
                self.cleanup_connection(request_id, is_streaming)
                return jsonify({"error": f"Method {req.method} not supported"}), 405

            # Initialize response_size variable to be accessible in on_response_close
            response_size = 0

            # Clean up tracking after response is complete
            def on_response_close():
                # Get the final response size from the container
                final_response_size = response_size_container['size']

                # Update the connection info with response size before removal
                with active_connections_lock:
                    if request_id in active_connections:
                        # Update connection info with response size before removing
                        active_connections[request_id]['response_size'] = final_response_size
                        # Also emit an update event with the final response size
                        try:
                            socketio.emit('connection_updated', {
                                'id': request_id,
                                'response_size': final_response_size,
                                'response_size_kb': round(final_response_size / 1024, 2)
                            })
                        except Exception as e:
                            logger.error(f"Failed to emit connection_updated event: {e}")

                        # Remove the connection from active list
                        del active_connections[request_id]

                # Update server stats
                with server_stats_lock:
                    server_stats['active_connections'] = len(active_connections)
                    server_stats['total_messages'] += 1

                # Emit connection removal event to frontend
                try:
                    socketio.emit('connection_removed', {'id': request_id})
                    # Broadcast updated stats to all clients
                    socketio.emit('server_stats_update', {
                        'active_connections': server_stats['active_connections'],
                        'active_streams': server_stats['active_streams'],
                        'total_messages': server_stats['total_messages']
                    })
                except Exception as e:
                    logger.error(f"Failed to emit connection_removed event: {e}")

                if is_chat_completions and is_streaming:
                    # Update stream info with response size and remove from active list
                    with active_streams_lock:
                        if request_id in active_streams:
                            # Update stream info with response size
                            active_streams[request_id]['response_size'] = final_response_size
                            del active_streams[request_id]

                    # Update server stats
                    with server_stats_lock:
                        server_stats['active_streams'] = len(active_streams)
                        server_stats['total_messages'] += 1

                    # Emit stream finished event to frontend
                    try:
                        socketio.emit('stream_finished', {
                            'id': request_id,
                            'timestamp': datetime.now().isoformat(),
                            'response_size': final_response_size,
                            'response_size_kb': round(final_response_size / 1024, 2)
                        })
                        # Broadcast updated stats to all clients
                        socketio.emit('server_stats_update', {
                            'active_connections': server_stats['active_connections'],
                            'active_streams': server_stats['active_streams'],
                            'total_messages': server_stats['total_messages']
                        })
                    except Exception as e:
                        logger.error(f"Failed to emit stream_finished event: {e}")
                else:
                    # For non-streaming requests, also emit a completion event
                    try:
                        socketio.emit('request_completed', {
                            'id': request_id,
                            'timestamp': datetime.now().isoformat(),
                            'response_size': final_response_size,
                            'response_size_kb': round(final_response_size / 1024, 2)
                        })
                    except Exception as e:
                        logger.error(f"Failed to emit request_completed event: {e}")

            # Initialize response_size variable to be accessible in on_response_close
            # We'll use a mutable container to hold the value so it can be modified by inner functions
            response_size_container = {'size': 0}

            def generate():
                try:
                    is_gzipped = response.headers.get('Content-Encoding', '') == 'gzip'

                    for chunk in response.iter_content(chunk_size=1024):
                        if chunk:
                            # Track response size
                            response_size_container['size'] += len(chunk)

                            # If the response is gzipped, decompress it before sending
                            if is_gzipped:
                                try:
                                    import gzip
                                    chunk = gzip.decompress(chunk)
                                except:
                                    pass  # If decompression fails, use the original chunk

                            # Forward the upstream bytes unchanged. A UTF-8
                            # code point may span two HTTP chunks, so decoding
                            # each chunk strictly can terminate an otherwise
                            # valid Chinese JSON/SSE response. Monitoring is
                            # best-effort and must never break proxy delivery.
                            chunk_bytes = chunk if isinstance(chunk, bytes) else chunk.encode('utf-8')
                            chunk_str = chunk_bytes.decode('utf-8', errors='replace')

                            # Check if this chunk contains usage information for token stats
                            if is_chat_completions and chunk_str.startswith('data: '):
                                try:
                                    provider = endpoint_config['proxy_path_prefix'].strip('/').split('/')[-1] or endpoint_config['proxy_path_prefix'].strip('/').split('/')[0]

                                    if chunk_str.startswith('data: ') and chunk_str != 'data: [DONE]\n':
                                        json_str = chunk_str[6:].strip()
                                        if json_str and json_str != '[DONE]':
                                            try:
                                                data_obj = json.loads(json_str)
                                                if 'usage' in data_obj and data_obj['usage'] and data_obj['usage'] != {}:
                                                    usage = data_obj['usage']
                                                    # Update token statistics
                                                    if provider not in token_stats:
                                                        token_stats[provider] = {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}

                                                    # Update stats
                                                    token_stats[provider]['prompt_tokens'] += usage.get('prompt_tokens', 0)
                                                    token_stats[provider]['completion_tokens'] += usage.get('completion_tokens', 0)
                                                    token_stats[provider]['total_tokens'] += usage.get('total_tokens', 0)

                                                    # Emit token stats update
                                                    try:
                                                        socketio.emit('token_stats_update', {'stats': token_stats})
                                                    except Exception as e:
                                                        logger.error(f"Failed to emit token_stats_update event: {e}")
                                            except json.JSONDecodeError:
                                                pass  # This chunk doesn't contain valid JSON
                                except Exception as e:
                                    logger.error(f"Error processing usage data: {e}")

                            # Emit stream data event for real-time monitoring
                            if is_chat_completions and chunk_str.startswith('data: '):
                                try:
                                    if chunk_str.startswith('data: ') and chunk_str != 'data: [DONE]\n':
                                        json_str = chunk_str[6:].strip()
                                        if json_str and json_str != '[DONE]':
                                            try:
                                                data_obj = json.loads(json_str)
                                                # Extract delta content for chat completions
                                                if 'choices' in data_obj and len(data_obj['choices']) > 0:
                                                    choice = data_obj['choices'][0]
                                                    if 'delta' in choice:
                                                        delta_data = choice['delta']
                                                        stream_data = {
                                                            'id': request_id,
                                                            'delta': delta_data,
                                                            'timestamp': datetime.now().isoformat()
                                                        }
                                                        socketio.emit('stream_chunk', {'data': stream_data})
                                                    else:
                                                        stream_data = {
                                                            'id': request_id,
                                                            'parsed_data': data_obj,
                                                            'timestamp': datetime.now().isoformat()
                                                        }
                                                        socketio.emit('stream_chunk', {'data': stream_data})
                                                else:
                                                    stream_data = {
                                                        'id': request_id,
                                                        'parsed_data': data_obj,
                                                        'timestamp': datetime.now().isoformat()
                                                    }
                                                    socketio.emit('stream_chunk', {'data': stream_data})
                                            except json.JSONDecodeError:
                                                # If it's not valid JSON, emit as general chunk
                                                stream_data = {
                                                    'id': request_id,
                                                    'chunk': chunk_str[:200] + '...' if len(chunk_str) > 200 else chunk_str,
                                                    'timestamp': datetime.now().isoformat()
                                                }
                                                socketio.emit('stream_chunk', {'data': stream_data})
                                        else:
                                            # It's [DONE] or empty, emit as special event
                                            stream_data = {
                                                'id': request_id,
                                                'chunk': chunk_str,
                                                'timestamp': datetime.now().isoformat()
                                            }
                                            socketio.emit('stream_chunk', {'data': stream_data})
                                    else:
                                        # Non-data line, emit as general chunk
                                        stream_data = {
                                            'id': request_id,
                                            'chunk': chunk_str[:200] + '...' if len(chunk_str) > 200 else chunk_str,
                                            'timestamp': datetime.now().isoformat()
                                        }
                                        socketio.emit('stream_chunk', {'data': stream_data})
                                except:
                                    pass  # Silently ignore stream chunk emission failures

                            yield chunk_bytes
                except requests.exceptions.RequestException as e:
                    # Upstream stream reset/timed out mid-stream. Stop streaming
                    # cleanly rather than let Werkzeug surface a raw 500.
                    logger.info(f"Upstream stream ended during transfer: {e}")
                finally:
                    on_response_close()

            # Return streaming response
            response_headers = dict(response.headers.items())

            # requests.iter_content() may already decode gzip, and Flask will
            # choose its own framing. Header casing is provider-dependent, so
            # strip hop-by-hop/body-encoding headers case-insensitively.
            for key in list(response_headers):
                if key.lower() in {
                    'content-encoding', 'transfer-encoding',
                    'connection', 'content-length',
                }:
                    response_headers.pop(key, None)

            # Add proper content-type if missing
            mimetype = None
            for k in response_headers.keys():
                if k.lower() == 'content-type':
                    mimetype = response_headers[k]
                    del response_headers[k]
                    break

            response_headers['Content-Type'] = 'application/json' if mimetype is None else mimetype

            return Response(
                generate(),
                status=response.status_code,
                headers=response_headers,
                mimetype=response_headers['Content-Type'],
                direct_passthrough=True
            )

        except requests.exceptions.Timeout as e:
            logger.error(f"Request to target API timed out: {e}")

            # Clean up tracking (also refreshes server stats)
            self.cleanup_connection(request_id, is_chat_completions and is_streaming)

            return jsonify({"error": "Upstream timeout (gateway timed out waiting for target API)"}), 504

        except requests.exceptions.RequestException as e:
            logger.error(f"Request to target API failed: {e}")

            # Clean up tracking (also refreshes server stats)
            self.cleanup_connection(request_id, is_chat_completions and is_streaming)

            return jsonify({"error": "Failed to connect to target API"}), 502

    def run(self, host='0.0.0.0'):
        """Run the Flask server with SocketIO."""
        logger.info(f"Starting proxy server on {host}:{self.port}")
        logger.info(f"Monitor interface available at http://{host}:{self.port}/monitor")

        # Fetch models from all endpoints on startup
        fetch_all_models()

        # Watch proxy_config.json so external edits hot-reload in real-time
        start_config_watcher()

        # Sweep zombie connections/streams that were never cleaned up
        start_zombie_sweeper()

        # Run the Flask-SocketIO app
        # Use threading mode for better concurrency
        socketio.run(app, host=host, port=self.port, debug=False, use_reloader=False, allow_unsafe_werkzeug=True)

# Cache expiration time in seconds (set to 0 for permanent cache)
MODEL_CACHE_EXPIRATION = 0

def is_model_cache_expired():
    """Check if the model cache has expired."""
    import time
    # If MODEL_CACHE_EXPIRATION is 0, cache is permanent
    if MODEL_CACHE_EXPIRATION == 0:
        return False  # Never expire if set to permanent
    current_time = time.time()
    # Check if we have any cached models and when they were last updated
    if 'last_model_refresh' in globals():
        return (current_time - last_model_refresh) > MODEL_CACHE_EXPIRATION
    else:
        return True  # If no timestamp exists, treat as expired


def get_cached_models():
    """Get models from cache, refresh if necessary."""
    # Check if cache is expired, if so refresh it
    if is_model_cache_expired():
        logger.info("Model cache expired, refreshing...")
        fetch_all_models(refresh=True)
    else:
        logger.debug("Using cached models")
    return cached_models

def fetch_all_models(refresh=True):
    global cached_models, model_routing, last_model_refresh
    import time

    # Update the refresh timestamp
    last_model_refresh = time.time()

    import requests
    from urllib.parse import urljoin

    all_models = {}
    routing = {}

    # Load config
    config_local = read_config_file()

    endpoints = config_local.get('endpoints', [])

    for endpoint in endpoints:
        try:
            # Check if static models are configured for this endpoint
            static_models = endpoint.get('static_models') or endpoint.get('models')
            if static_models:
                # Use static models configuration
                proxy_prefix = endpoint['proxy_path_prefix']
                for model in static_models:
                    # Handle both string format (like in 'models' array) and object format (like in 'static_models')
                    if isinstance(model, str):
                        model_id = model
                        model_obj = {
                            'id': model_id,
                            'object': 'model',
                            'created': int(time.time()),
                            'owned_by': 'unknown'
                        }
                    else:
                        model_id = model.get('id')
                        model_obj = model

                    if model_id:
                        # Store the original model ID without any prefix modification
                        final_model_id = model_id

                        # Check if this original model already exists
                        original_already_exists = False
                        for existing_id, existing_model in all_models.items():
                            if existing_model.get('original_id') == model_id:
                                original_already_exists = True
                                # Add this endpoint to the available endpoints for this model
                                if 'available_endpoints' not in existing_model:
                                    existing_model['available_endpoints'] = []
                                if proxy_prefix not in existing_model['available_endpoints']:
                                    existing_model['available_endpoints'].append(proxy_prefix)
                                if 'first_source_endpoint' not in existing_model:
                                    existing_model['first_source_endpoint'] = proxy_prefix
                                break

                        if not original_already_exists:
                            existing_display_setting = cached_models.get(final_model_id, {}).get('is_displayed', True)
                            all_models[final_model_id] = {
                                'id': final_model_id,
                                'original_id': model_id,  # Keep track of original ID
                                'object': model_obj.get('object', 'model'),
                                'created': model_obj.get('created', int(time.time())),
                                'owned_by': model_obj.get('owned_by', 'unknown'),
                                'source_endpoint': proxy_prefix,  # First endpoint in config order
                                'available_endpoints': [proxy_prefix],  # All endpoints that provide this model
                                'is_static': True,  # Mark as static model
                                'is_displayed': model_display_settings.get(final_model_id, existing_display_setting),  # Apply saved display setting
                                'redirect_to': model_redirects.get(final_model_id)  # Include redirect info
                            }

                            # For routing, prefer local endpoints (starting with /local)
                            if final_model_id not in routing or proxy_prefix.startswith('/local'):
                                if final_model_id not in routing:
                                    routing[final_model_id] = []

                                # Add to routing list, prioritizing local endpoints
                                if proxy_prefix.startswith('/local'):
                                    routing[final_model_id].insert(0, endpoint)
                                else:
                                    routing[final_model_id].append(endpoint)
            else:
                # Fetch models from the upstream API
                proxy_prefix = endpoint['proxy_path_prefix']
                target_base = endpoint['target_base_url']
                models_url = urljoin(target_base, 'v1/models')

                # Prepare headers with API key if configured
                headers = {}
                headers.update(resolve_api_key_headers(endpoint))

                # Set User-Agent to mimic OpenAI/JS client
                headers['User-Agent'] = 'OpenAI/JS 6.26.0'

                # Retry logic for model fetching
                max_retries = 3
                timeout = 1  # 1 second timeout
                for attempt in range(max_retries):
                    try:
                        response = requests.get(models_url, headers=headers, timeout=timeout)
                        break  # Success, exit retry loop
                    except requests.exceptions.Timeout:
                        if attempt == max_retries - 1:  # Last attempt
                            logger.error(f"Failed to fetch models from {endpoint['proxy_path_prefix']} after {max_retries} attempts: Timeout")
                            raise  # Re-raise the exception after max retries
                        else:
                            logger.warning(f"Attempt {attempt + 1} failed for {endpoint['proxy_path_prefix']}: Timeout, retrying...")
                            continue  # Continue to next attempt

                if response.status_code == 200:
                    data = response.json()
                    if 'data' in data:  # OpenAI format
                        for model in data['data']:
                            model_id = model.get('id')
                            if model_id:
                                # Store the original model ID without any prefix modification
                                final_model_id = model_id

                                # Check if this original model already exists
                                original_already_exists = False
                                for existing_id, existing_model in all_models.items():
                                    if existing_model.get('original_id') == model_id:
                                        original_already_exists = True
                                        # Add this endpoint to the available endpoints for this model
                                        if 'available_endpoints' not in existing_model:
                                            existing_model['available_endpoints'] = []
                                        if proxy_prefix not in existing_model['available_endpoints']:
                                            existing_model['available_endpoints'].append(proxy_prefix)
                                        if 'first_source_endpoint' not in existing_model:
                                            existing_model['first_source_endpoint'] = proxy_prefix
                                        break

                                if not original_already_exists:
                                    existing_display_setting = cached_models.get(final_model_id, {}).get('is_displayed', True)
                                    all_models[final_model_id] = {
                                        'id': final_model_id,
                                        'original_id': model_id,  # Keep track of original ID
                                        'object': model.get('object', 'model'),
                                        'created': model.get('created', int(time.time())),
                                        'owned_by': model.get('owned_by', 'unknown'),
                                        'source_endpoint': proxy_prefix,  # First endpoint in config order
                                        'available_endpoints': [proxy_prefix],  # All endpoints that provide this model
                                        'is_static': False,  # Mark as dynamic model
                                        'is_displayed': model_display_settings.get(final_model_id, existing_display_setting),  # Apply saved display setting
                                        'redirect_to': model_redirects.get(final_model_id)  # Include redirect info
                                    }

                                    # For routing, prefer local endpoints (starting with /local)
                                    if final_model_id not in routing or proxy_prefix.startswith('/local'):
                                        if final_model_id not in routing:
                                            routing[final_model_id] = []

                                        # Add to routing list, prioritizing local endpoints
                                        if proxy_prefix.startswith('/local'):
                                            routing[final_model_id].insert(0, endpoint)
                                        else:
                                            routing[final_model_id].append(endpoint)
        except Exception as e:
            logger.error(f"Error fetching models from {endpoint['proxy_path_prefix']}: {e}")

    cached_models = all_models

    # Update cached models with current routing info
    for model_id in cached_models:
        if model_id in globals().get('custom_model_routing', {}):
            cached_models[model_id]['current_route'] = custom_model_routing[model_id]
        else:
            # Use default routing
            default_endpoints = model_routing.get(model_id, [])
            if default_endpoints:
                cached_models[model_id]['current_route'] = default_endpoints[0].get('proxy_path_prefix', 'default')

    model_routing = routing


@socketio.on('connect')
def handle_connect():
    """Handle new WebSocket connections."""
    logger.info("Monitor client connected via WebSocket")
    # Send initial data to the newly connected client
    with active_connections_lock:
        connections_data = list(active_connections.values())
    with active_streams_lock:
        streams_data = list(active_streams.values())
    with token_stats_lock:
        stats_data = token_stats.copy()
    with cached_models_lock:
        models_data = list(cached_models.values())
    with model_redirects_lock:
        redirects_data = model_redirects.copy()

    # Update server stats before sending initial data
    with server_stats_lock:
        current_server_stats = server_stats.copy()

    emit('initial_data', {
        'connections': connections_data,
        'streams': streams_data,
        'token_stats': stats_data,
        'models': models_data,
        'redirects': redirects_data,
        'server_stats': current_server_stats
    })


# Add routes for the aggregated endpoints
@app.route('/v1/models')
def aggregated_models():
    """Return all models from all configured endpoints."""
    # Get models from cache (will refresh if necessary)
    current_cached_models = get_cached_models()

    # Filter models based on display settings
    displayed_models = [
        model for model in current_cached_models.values()
        if model.get('is_displayed', True)  # Default to True if not set
    ]

    # Add prefixes to model IDs for display in the aggregated models list
    prefixed_models = []
    for model in displayed_models:
        # Create a copy of the model to avoid modifying the cached version
        model_copy = model.copy()

        # Add prefix to the model ID for display purposes
        model_id = model_copy['id']
        if '/' not in model_id:
            # Check if the model name matches any known prefixes
            matched_prefix = get_model_prefix(model_id)
            if matched_prefix:
                prefixed_id = f"{matched_prefix}/{model_id}"
            else:
                # Use the source endpoint as a prefix if no specific prefix matches
                source_endpoint = model_copy.get('source_endpoint', 'unknown')
                clean_prefix = source_endpoint.lstrip('/').replace('/', '-')
                prefixed_id = f"{clean_prefix}/{model_id}"

            model_copy['id'] = prefixed_id

        prefixed_models.append(model_copy)

    return jsonify({
        "object": "list",
        "data": prefixed_models
    })


# Global reference to the APIProxyServer instance to reuse its methods
proxy_server_instance = None

@app.route('/v1/chat/completions', methods=['POST'])
def aggregated_chat_completions():
    """Route chat completions to the appropriate endpoint based on model, reusing proxy logic for monitoring."""
    global model_routing, custom_model_routing, proxy_server_instance, proxy_server

    try:
        data = request.get_json()
        requested_model_id = data.get('model')  # This is the model ID as sent in the request

        if not requested_model_id:
            return jsonify({"error": "Model is required"}), 400

        # HANDLE IMAGE -> VISION MODEL REDIRECT (only for /__proxy__-routed aliases)
        # If a text-only alias served by the pure-proxy endpoint carries image content,
        # rewrite it to its vision-capable counterpart. Models that point to a specific
        # concrete endpoint are left untouched (their backend knows its own capability).
        def _routes_through_proxy(model_id):
            cm = cached_models.get(model_id)
            if cm and cm.get('current_route') == '/__proxy__':
                return True
            routes = model_routing.get(model_id) or []
            return bool(routes and routes[0].get('proxy_path_prefix') == '/__proxy__')

        vision_alias = None
        if _routes_through_proxy(requested_model_id):
            if requested_model_id in model_vision_redirects:
                vision_alias = model_vision_redirects[requested_model_id]
            else:
                requested_lower = str(requested_model_id).lower()
                for original, target in model_vision_redirects.items():
                    if str(original).lower() == requested_lower:
                        vision_alias = target
                        break
        if vision_alias and request_contains_image(data):
            logger.info(f"Image request -> {requested_model_id} (via /__proxy__) vision redirect to {vision_alias}")
            requested_model_id = vision_alias


        # HANDLE MODEL REDIRECT AT THE ENTRY POINT - DIRECT AND CLEAR
        final_model_name = requested_model_id  # Default to original if no redirect

        # Check for exact match first
        if requested_model_id in model_redirects:
            redirected_to = model_redirects[requested_model_id]
            logger.info(f"Exact redirect match: {requested_model_id} -> {redirected_to}")
            final_model_name = redirected_to
        else:
            # Try case-insensitive match
            requested_lower = requested_model_id.lower()
            for original, target in model_redirects.items():
                if original.lower() == requested_lower:
                    redirected_to = target
                    logger.info(f"Case-insensitive redirect match: {requested_model_id} -> {redirected_to}")
                    final_model_name = redirected_to
                    break
            else:
                # If no direct match, try removing prefix and matching
                # Extract model name without prefix (part after the last '/')
                if '/' in requested_model_id:
                    bare_model_name = requested_model_id.split('/')[-1]
                    bare_model_lower = bare_model_name.lower()

                    # Try matching the bare model name
                    for original, target in model_redirects.items():
                        original_bare = original.split('/')[-1] if '/' in original else original
                        if original_bare.lower() == bare_model_lower:
                            redirected_to = target
                            logger.info(f"Bare name redirect match: {requested_model_id} ({bare_model_name}) -> {redirected_to}")
                            final_model_name = redirected_to
                            break
                        elif original.lower() == bare_model_lower:
                            redirected_to = target
                            logger.info(f"Bare name redirect match (original without prefix): {requested_model_id} ({bare_model_name}) -> {redirected_to}")
                            final_model_name = redirected_to
                            break

        # Now determine the backend model name (original_id from cache)
        backend_model_name = final_model_name  # Default to final_model_name if not in cache
        routing_model_id = final_model_name  # Use this for routing lookup

        # If final_model_name is in cache, get its original_id for backend
        if final_model_name in cached_models:
            backend_model_name = cached_models[final_model_name].get('original_id', final_model_name)
            logger.info(f"Found in cache, using backend name: {final_model_name} -> {backend_model_name}")
        else:
            # Try to find if final_model_name matches an original_id in cache
            found_in_cache_as_original = False
            for cached_id, cached_model in cached_models.items():
                if cached_model.get('original_id') == final_model_name:
                    routing_model_id = cached_id  # Use cached_id for routing
                    backend_model_name = final_model_name  # Keep as-is for backend
                    logger.info(f"Found as original_id in cache: {final_model_name} (cached as {cached_id})")
                    found_in_cache_as_original = True
                    break

            if not found_in_cache_as_original:
                # Try case-insensitive match in cache
                final_lower = final_model_name.lower()
                for cached_id, cached_model in cached_models.items():
                    if cached_id.lower() == final_lower or cached_model.get('original_id', '').lower() == final_lower:
                        routing_model_id = cached_id
                        backend_model_name = cached_model.get('original_id', cached_id)
                        logger.info(f"Found via case-insensitive match: {final_model_name} -> {routing_model_id} -> {backend_model_name}")
                        break
                else:
                    # If still no match, try removing prefix from the requested model name
                    if '/' in final_model_name:
                        bare_model_name = final_model_name.split('/')[-1]
                        bare_model_lower = bare_model_name.lower()

                        # Look for the bare name in cache
                        for cached_id, cached_model in cached_models.items():
                            cached_bare = cached_id.split('/')[-1] if '/' in cached_id else cached_id
                            if cached_bare.lower() == bare_model_lower or cached_model.get('original_id', '').split('/')[-1].lower() == bare_model_lower:
                                routing_model_id = cached_id
                                backend_model_name = cached_model.get('original_id', cached_id)
                                logger.info(f"Found via bare name match: {final_model_name} ({bare_model_name}) -> {routing_model_id} -> {backend_model_name}")
                                break

        logger.info(f"Model processing: {requested_model_id} -> {final_model_name} -> {backend_model_name}")

        # Now find the appropriate endpoint for routing_model_id
        selected_endpoint = None

        # Check custom routing first
        if routing_model_id in custom_model_routing:
            custom_endpoint_prefix = custom_model_routing[routing_model_id]
            # Find the endpoint configuration that matches this prefix
            for endpoint in proxy_server.endpoints if 'proxy_server' in globals() and proxy_server else []:
                if endpoint['proxy_path_prefix'] == custom_endpoint_prefix:
                    selected_endpoint = endpoint
                    logger.info(f"Using custom routing for {routing_model_id} -> {custom_endpoint_prefix}")
                    break

        if not selected_endpoint:
            # Use default routing
            endpoints_for_model = model_routing.get(routing_model_id, [])

            # If no direct match, try case-insensitive match
            if not endpoints_for_model:
                for route_model in model_routing.keys():
                    if route_model.lower() == routing_model_id.lower():
                        endpoints_for_model = model_routing.get(route_model, [])
                        logger.info(f"Using case-insensitive match for routing: {routing_model_id} -> {route_model}")
                        break

            if not endpoints_for_model:
                return jsonify({"error": f"Model {routing_model_id} not found in any configured endpoint"}), 404

            # Select the first endpoint (prioritized)
            selected_endpoint = endpoints_for_model[0]

        # At this point, we have:
        # - backend_model_name: the model name to send to the backend
        # - selected_endpoint: the endpoint to route to
        logger.info(f"Routing {requested_model_id} -> {backend_model_name} via {selected_endpoint['proxy_path_prefix']}")

        # Set the final backend model name as an attribute for the proxy handler
        request.original_model_id = backend_model_name
        return proxy_server_instance.handle_proxy_request(request, selected_endpoint, 'v1/chat/completions')

    except Exception as e:
        logger.error(f"Error in aggregated chat completions: {e}")
        return jsonify({"error": str(e)}), 500


@socketio.on('disconnect')
def handle_disconnect():
    """Handle WebSocket disconnections."""
    logger.info("Monitor client disconnected")


# Socket.IO event handlers for model routing configuration
def check_endpoint_health():
    """Probe every configured endpoint and report reachability + its models.

    Returns a list of dicts, one per endpoint:
      { proxy_path_prefix, target_base_url, kind, ok, error, latency_ms,
        models: [ {id, original_id, is_static, owned_by} ... ] }
    """
    import time as _time
    endpoints = proxy_server.endpoints if 'proxy_server' in globals() else []
    result = []
    for ep in endpoints:
        prefix = ep.get('proxy_path_prefix', '')
        base = ep.get('target_base_url', '')
        # Models served by this endpoint (by available_endpoints or source_endpoint)
        ep_models = []
        for m in cached_models.values():
            avail = m.get('available_endpoints') or [m.get('source_endpoint')]
            if prefix in avail or m.get('source_endpoint') == prefix:
                ep_models.append({
                    'id': m.get('id'),
                    'original_id': m.get('original_id'),
                    'is_static': bool(m.get('is_static')),
                    'owned_by': m.get('owned_by'),
                    'is_displayed': m.get('is_displayed', True),
                })
        entry = {
            'proxy_path_prefix': prefix,
            'target_base_url': base,
            'kind': 'virtual' if not base.strip() else 'http',
            'ok': None,
            'error': None,
            'latency_ms': None,
            'model_count': len(ep_models),
            'models': ep_models,
        }
        if not base.strip():
            result.append(entry)
            continue
        # Probe the models endpoint
        import requests as _r
        from urllib.parse import urljoin as _urljoin
        url = _urljoin(base.rstrip('/') + '/', 'v1/models')
        start = _time.time()
        try:
            resp = _r.get(url, timeout=1.5)
            entry['ok'] = resp.status_code == 200
            entry['error'] = f"HTTP {resp.status_code}" if resp.status_code != 200 else None
        except _r.exceptions.RequestException as e:
            entry['ok'] = False
            # Keep only the concise reason (e.g. "Connection refused", "timed out")
            entry['error'] = _shorten_error(str(e))
        entry['latency_ms'] = round((_time.time() - start) * 1000)
        result.append(entry)
    return result


def _shorten_error(msg):
    """Reduce a long requests exception string to a short, human-readable reason."""
    import re as _re
    if not msg:
        return ''
    text = msg.strip().replace('\n', ' ')
    # Prefer the "Caused by ... [Errno ...] <reason>" tail, or the trailing phrase.
    for pat in (r"Caused by.*?\]\s*([A-Za-z][^)]*)", r"Caused by.*?\b([A-Za-z][A-Za-z ]{3,60})\b"):
        m = _re.search(pat, text)
        if m:
            cand = m.group(1).strip().strip("'\"") .rstrip('.').strip()
            if cand:
                return cand if len(cand) <= 90 else cand[:90] + '…'
    # Otherwise take the last parenthesized phrase or the tail.
    m = _re.search(r"\((.+?)\)$", text)
    cand = (m.group(1) if m else text).strip()
    cand = cand.split('(')[0].strip().rstrip(':').split(':')[-1].strip()
    return cand if len(cand) <= 90 else cand[:90] + '…'


@socketio.on('request_endpoint_health')
def handle_request_endpoint_health():
    """Probe endpoint reachability and return it, plus per-endpoint models."""
    from flask_socketio import emit
    try:
        health = check_endpoint_health()
        emit('endpoint_health_updated', {'endpoints': health})
    except Exception as e:
        logger.error(f"Failed to check endpoint health: {e}")
        emit('config_update_error', {'message': f'Endpoint health check failed: {e}'})


@socketio.on('request_initial_models')
def handle_request_initial_models():
    """Send initial models data to the client."""
    from flask_socketio import emit
    # Ensure models are fresh
    current_cached_models = get_cached_models()
    emit('models_updated', {
        'models': list(current_cached_models.values()),
        'endpoints': proxy_server.endpoints if 'proxy_server' in globals() else [],
        'redirects': model_redirects,  # NEW: Include redirects in the response
        'vision_redirects': model_vision_redirects
    })


@socketio.on('request_models_refresh')
def handle_request_models_refresh():
    """Refresh models from all endpoints."""
    from flask_socketio import emit
    logger.info("Refreshing models from all endpoints...")
    fetch_all_models(refresh=True)  # Force refresh
    emit('models_updated', {
        'models': list(cached_models.values()),
        'endpoints': proxy_server.endpoints if 'proxy_server' in globals() else [],
        'redirects': model_redirects,  # NEW: Include redirects in the response
        'vision_redirects': model_vision_redirects,
        'message': f'Models refreshed from all endpoints'
    })


@socketio.on('change_model_route')
def handle_change_model_route(data):
    """Change the routing for a specific model."""
    from flask_socketio import emit
    model_id = data.get('model_id')
    endpoint = data.get('endpoint')  # endpoint is the proxy_path_prefix

    if endpoint:
        # Set custom routing for this model
        custom_model_routing[model_id] = endpoint
    else:
        # Remove custom routing (use default)
        if model_id in custom_model_routing:
            del custom_model_routing[model_id]

    # Update the cached model with the new route
    if model_id in cached_models:
        if endpoint:
            cached_models[model_id]['current_route'] = endpoint
        else:
            # Reset to default route
            default_endpoints = model_routing.get(model_id, [])
            if default_endpoints:
                cached_models[model_id]['current_route'] = default_endpoints[0].get('proxy_path_prefix', 'default')
            else:
                cached_models[model_id]['current_route'] = 'default'

    # Save the routing settings to the config file
    save_model_routing_settings()

    # Send confirmation back to client
    socketio.emit('models_updated', {
        'models': list(cached_models.values()),
        'endpoints': proxy_server.endpoints if 'proxy_server' in globals() else [],
        'redirects': model_redirects,  # NEW: Include redirects in the response
        'message': f'Routing updated for {model_id}: {endpoint}'
    })


@socketio.on('request_provider_models_refresh')
def handle_request_provider_models_refresh(data):
    """Refresh models from a specific provider."""
    from flask_socketio import emit
    provider = data.get('provider')
    logger.info(f"Refreshing models from provider: {provider}")

    # Refresh models from the specific provider
    fetch_models_from_provider(provider)

    socketio.emit('models_updated', {
        'models': list(cached_models.values()),
        'endpoints': proxy_server.endpoints if 'proxy_server' in globals() else [],
        'redirects': model_redirects,  # NEW: Include redirects in the response
        'message': f'Models refreshed from {provider}'
    })


@socketio.on('set_model_display')
def handle_set_model_display(data):
    """Set the display setting for a specific model and save to config."""
    from flask_socketio import emit
    model_id = data.get('model_id')
    is_displayed = data.get('is_displayed', True)

    # Update the display setting
    model_display_settings[model_id] = is_displayed

    # Update the cached model with the display setting
    if model_id in cached_models:
        cached_models[model_id]['is_displayed'] = is_displayed

    # Save the display settings to the config file
    save_model_display_settings()

    socketio.emit('models_updated', {
        'models': list(cached_models.values()),
        'endpoints': proxy_server.endpoints if 'proxy_server' in globals() else [],
        'redirects': model_redirects,  # NEW: Include redirects in the response
        'message': f'Display setting updated for {model_id}: {is_displayed}'
    })


# NEW: Socket.IO event handler for setting model redirects from UI
@socketio.on('set_model_redirect')
def handle_set_model_redirect(data):
    """Set model redirect from UI."""
    from flask_socketio import emit
    original_model = data.get('original_model')
    target_model = data.get('target_model')
    if target_model == "":
        target_model = None

    if original_model:
        # Set the redirect
        set_model_redirect(original_model, target_model)

        # Send confirmation back to client
        socketio.emit('models_updated', {
            'models': list(cached_models.values()),
            'endpoints': proxy_server.endpoints if 'proxy_server' in globals() else [],
            'redirects': model_redirects,
            'message': f'Model redirect set: {original_model} -> {target_model}'
        })
    else:
        emit('error', {'message': 'Both original and target models must be specified'})


@socketio.on('save_vision_redirect')
def handle_save_vision_redirect(data):
    """Set or clear a vision redirect from the UI."""
    from flask_socketio import emit
    original_model = (data.get('original_model') or '').strip()
    target_model = (data.get('target_model') or '').strip()

    if not original_model:
        emit('error', {'message': 'Original model must be specified'})
        return

    set_vision_redirect(original_model, target_model or None)

    socketio.emit('models_updated', {
        'models': list(cached_models.values()),
        'endpoints': proxy_server.endpoints if 'proxy_server' in globals() else [],
        'redirects': model_redirects,
        'vision_redirects': model_vision_redirects,
        'message': f'Vision redirect set: {original_model}' + (f' -> {target_model}' if target_model else ' (cleared)')
    })


@socketio.on('load_vision_redirects')
def handle_load_vision_redirects():
    socketio.emit('vision_redirects_update', {'vision_redirects': model_vision_redirects})



# NEW: Socket.IO event handler for saving target model configuration
@socketio.on('save_target_model_config')
def handle_save_target_model_config(data):
    """Save target model configuration from UI. This handles all model redirects, including those for pure proxy models."""
    from flask_socketio import emit
    source_model = data.get('source_model')
    target_model = data.get('target_model')

    if source_model:
        # Save as a model redirect in the standard model_redirects section
        # This combines both regular model redirects and pure proxy model redirects
        set_model_redirect(source_model, target_model)

        # Update the cached model
        if source_model in cached_models:
            cached_models[source_model]['target_model'] = target_model

        # Send confirmation back to client
        socketio.emit('models_updated', {
            'models': list(cached_models.values()),
            'endpoints': proxy_server.endpoints if 'proxy_server' in globals() else [],
            'redirects': model_redirects,
            'message': f'Target model configuration saved: {source_model} -> {target_model}'
        })
    else:
        emit('error', {'message': 'Source model must be specified'})


# NEW: Socket.IO event handler for saving fixed models configuration
@socketio.on('save_fixed_models_config')
def handle_save_fixed_models_config(data):
    """Save fixed models configuration for an endpoint from UI."""
    from flask_socketio import emit
    endpoint_prefix = data.get('endpoint_prefix')
    fixed_models = data.get('fixed_models', [])

    if endpoint_prefix:
        # Save the fixed models configuration
        save_fixed_models_config(endpoint_prefix, fixed_models)

        # Update the endpoint configuration
        for i, endpoint in enumerate(proxy_server.endpoints):
            if endpoint['proxy_path_prefix'] == endpoint_prefix:
                proxy_server.endpoints[i]['models'] = fixed_models
                # Update the main config as well
                proxy_server.config['endpoints'][i]['models'] = fixed_models
                break

        # Refresh models cache
        fetch_all_models(refresh=True)

        # Send confirmation back to client
        socketio.emit('models_updated', {
            'models': list(cached_models.values()),
            'endpoints': proxy_server.endpoints if 'proxy_server' in globals() else [],
            'redirects': model_redirects,
            'message': f'Fixed models configuration saved for {endpoint_prefix}: {len(fixed_models)} models'
        })
    else:
        emit('error', {'message': 'Endpoint prefix must be specified'})


# NEW: Socket.IO event handler for saving target model for endpoint
@socketio.on('save_target_model_for_endpoint')
def handle_save_target_model_for_endpoint(data):
    """Save target model configuration for an endpoint from UI."""
    from flask_socketio import emit
    endpoint_prefix = data.get('endpoint_prefix')
    target_model = data.get('target_model')

    if endpoint_prefix and target_model:
        # Save the target model configuration for endpoint
        save_target_model_for_endpoint(endpoint_prefix, target_model)

        # Send confirmation back to client
        socketio.emit('models_updated', {
            'models': list(cached_models.values()),
            'endpoints': proxy_server.endpoints if 'proxy_server' in globals() else [],
            'redirects': model_redirects,
            'message': f'Target model for endpoint {endpoint_prefix} set to: {target_model}'
        })
    else:
        emit('error', {'message': 'Both endpoint prefix and target model must be specified'})


def save_fixed_models_config(endpoint_prefix, fixed_models):
    """Save fixed models configuration for an endpoint to the proxy config file."""
    try:
        config = read_config_file()

        # Update the endpoint with fixed models
        for endpoint in config['endpoints']:
            if endpoint['proxy_path_prefix'] == endpoint_prefix:
                endpoint['models'] = fixed_models
                break

        # Write back to file atomically
        write_config_file(config)

        logger.info(f"Fixed models configuration saved for {endpoint_prefix}: {fixed_models}")
    except Exception as e:
        import traceback
        logger.error(f"Error saving fixed models configuration: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")


def save_target_model_for_endpoint(endpoint_prefix, target_model):
    """Save target model configuration for an endpoint to the proxy config file."""
    try:
        config = read_config_file()

        # Create endpoint_target_configs section if it doesn't exist
        if 'endpoint_target_configs' not in config:
            config['endpoint_target_configs'] = {}

        # Add the target model configuration for the endpoint
        config['endpoint_target_configs'][endpoint_prefix] = target_model

        # Write back to file atomically
        write_config_file(config)

        logger.info(f"Target model configuration saved for endpoint {endpoint_prefix}: {target_model}")
    except Exception as e:
        import traceback
        logger.error(f"Error saving target model configuration for endpoint: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")


def load_endpoint_target_configs():
    """Load endpoint target model configurations from the proxy config file."""
    try:
        config = read_config_file()

        # Load endpoint target configs if they exist
        if 'endpoint_target_configs' in config:
            endpoint_targets = config['endpoint_target_configs']
            logger.info(f"Loaded {len(endpoint_targets)} endpoint target configurations from config")
        else:
            logger.info("No endpoint target configurations found in config")
    except Exception as e:
        import traceback
        logger.error(f"Error loading endpoint target configurations: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")


def check_model_redirect_for_pure_proxy(model_name):
    """Check if a model on the pure proxy endpoint has a redirect configured."""
    # Since all redirects are now in model_redirects, we just need to check there
    # Look for case-insensitive match in the global model_redirects
    model_lower = model_name.lower()
    for source_model, target_model in model_redirects.items():
        if source_model.lower() == model_lower:
            return target_model

    return None


def get_model_prefix(model_name):
    """
    Match model name to known prefixes based on the provided list:
    Grok, DeepSeek, Qwen3.5, Qwen3, Qwen, GLM, Kimi, MiniMax, Doubao
    """
    model_name_lower = model_name.lower()

    # Get prefix mappings from config
    prefix_map = get_prefix_map_from_config()

    for pattern, prefix in prefix_map:
        if pattern in model_name_lower:
            return prefix

    return None


def get_prefix_map_from_config():
    """Get prefix map from config file, with fallback to default."""
    try:
        config = read_config_file()

        # Check if prefix_map exists in the config
        if 'prefix_map' in config:
            return config['prefix_map']
        else:
            # Return default prefix map if not found in config
            return [
                ['grok', 'Grok'],
                ['deepseek', 'Deepseek'],
                ['qwen3.5', 'Qwen'],
                ['qwen3', 'Qwen'],
                ['qwen', 'Qwen'],
                ['glm', 'GLM'],
                ['kimi', 'Kimi'],
                ['minimax', 'MiniMax'],
                ['doubao', 'Doubao']
            ]
    except Exception as e:
        logger.error(f"Error loading prefix map from config: {e}")
        # Return default if there's an error
        return [
            ['grok', 'Grok'],
            ['deepseek', 'Deepseek'],
            ['qwen3.5', 'Qwen'],
            ['qwen3', 'Qwen'],
            ['qwen', 'Qwen'],
            ['glm', 'GLM'],
            ['kimi', 'Kimi'],
            ['minimax', 'MiniMax'],
            ['doubao', 'Doubao']
        ]


def fetch_models_from_provider(provider_endpoint):
    """Fetch models from a specific provider endpoint."""
    global cached_models, model_routing

    import requests
    from urllib.parse import urljoin

    # Load config
    config_local = read_config_file()

    endpoints = config_local.get('endpoints', [])

    # Find the specific endpoint
    for endpoint in endpoints:
        if endpoint.get('proxy_path_prefix') == provider_endpoint:
            try:
                # Check if static models are configured for this endpoint
                static_models = endpoint.get('static_models') or endpoint.get('models')
                if static_models:
                    # Use static models configuration
                    proxy_prefix = endpoint['proxy_path_prefix']
                    for model in static_models:
                        # Handle both string format (like in 'models' array) and object format (like in 'static_models')
                        if isinstance(model, str):
                            # String format: just the model name
                            model_id = model
                            model_obj = {
                                'id': model_id,
                                'object': 'model',
                                'created': int(time.time()),
                                'owned_by': 'unknown'
                            }
                        else:
                            # Object format: full model object
                            model_id = model.get('id')
                            model_obj = model

                        if model_id:
                            # Store the original model ID without any prefix modification
                            final_model_id = model_id

                            # Check if this original model name already exists in our collection
                            original_exists = False
                            for existing_id, existing_model in cached_models.items():
                                if existing_model.get('original_id') == model_id:
                                    # This original model name already exists
                                    original_exists = True
                                    # Add this endpoint to the available endpoints for this model
                                    if 'available_endpoints' not in existing_model:
                                        existing_model['available_endpoints'] = []
                                    if proxy_prefix not in existing_model['available_endpoints']:
                                        existing_model['available_endpoints'].append(proxy_prefix)
                                    # Update the source_endpoint to be the first one (as per config order)
                                    if 'first_source_endpoint' not in existing_model:
                                        existing_model['first_source_endpoint'] = proxy_prefix
                                    break

                            # Check if this original model already exists
                            original_already_exists = False
                            for existing_id, existing_model in cached_models.items():
                                if existing_model.get('original_id') == model_id:
                                    original_already_exists = True
                                    # Add this endpoint to the available endpoints for this model
                                    if 'available_endpoints' not in existing_model:
                                        existing_model['available_endpoints'] = []
                                    if proxy_prefix not in existing_model['available_endpoints']:
                                        existing_model['available_endpoints'].append(proxy_prefix)
                                    # Update the source_endpoint to be the first one (as per config order)
                                    if 'first_source_endpoint' not in existing_model:
                                        existing_model['first_source_endpoint'] = proxy_prefix
                                    break

                            if not original_already_exists:
                                # This is the first occurrence of this original model name
                                # Use the prefixed version as the ID for display purposes
                                # Store the model info with its source endpoint
                                # Preserve existing display setting if it exists, otherwise default to True
                                existing_display_setting = cached_models.get(final_model_id, {}).get('is_displayed', True)
                                cached_models[final_model_id] = {
                                    'id': final_model_id,
                                    'original_id': model_id,  # Keep track of original ID
                                    'object': model_obj.get('object', 'model'),
                                    'created': model_obj.get('created', int(time.time())),
                                    'owned_by': model_obj.get('owned_by', 'unknown'),
                                    'source_endpoint': proxy_prefix,  # First endpoint in config order
                                    'available_endpoints': [proxy_prefix],  # All endpoints that provide this model
                                    'is_static': True,  # Mark as static model
                                    'is_displayed': model_display_settings.get(final_model_id, existing_display_setting),  # Apply saved display setting
                                    'redirect_to': model_redirects.get(final_model_id)  # NEW: Include redirect info
                                }
                else:
                    # Fetch models from the upstream API
                    proxy_prefix = endpoint['proxy_path_prefix']
                    target_base = endpoint['target_base_url']
                    models_url = urljoin(target_base, 'v1/models')

                    # Prepare headers with API key if configured
                    headers = {}
                    headers.update(resolve_api_key_headers(endpoint))

                    # Make request to get models
                    # Set User-Agent to mimic OpenAI/JS client
                    headers['User-Agent'] = 'OpenAI/JS 6.26.0'

                    # Retry logic for model fetching
                    max_retries = 3
                    timeout = 1  # 1 second timeout
                    for attempt in range(max_retries):
                        try:
                            response = requests.get(models_url, headers=headers, timeout=timeout)
                            break  # Success, exit retry loop
                        except requests.exceptions.Timeout:
                            if attempt == max_retries - 1:  # Last attempt
                                logger.error(f"Failed to fetch models from {endpoint['proxy_path_prefix']} after {max_retries} attempts: Timeout")
                                raise  # Re-raise the exception after max retries
                            else:
                                logger.warning(f"Attempt {attempt + 1} failed for {endpoint['proxy_path_prefix']}: Timeout, retrying...")
                                continue  # Continue to next attempt

                    if response.status_code == 200:
                        data = response.json()
                        if 'data' in data:  # OpenAI format
                            for model in data['data']:
                                model_id = model.get('id')
                                if model_id:
                                    # Store the original model ID without any prefix modification
                                    final_model_id = model_id

                                    # Check if this original model name already exists in our collection
                                    original_exists = False
                                    for existing_id, existing_model in cached_models.items():
                                        if existing_model.get('original_id') == model_id:
                                            # This original model name already exists
                                            original_exists = True
                                            # Add this endpoint to the available endpoints for this model
                                            if 'available_endpoints' not in existing_model:
                                                existing_model['available_endpoints'] = []
                                            if proxy_prefix not in existing_model['available_endpoints']:
                                                existing_model['available_endpoints'].append(proxy_prefix)
                                            # Update the source_endpoint to be the first one (as per config order)
                                            if 'first_source_endpoint' not in existing_model:
                                                existing_model['first_source_endpoint'] = proxy_prefix
                                            break

                                    # Check if this original model already exists
                                    original_already_exists = False
                                    for existing_id, existing_model in cached_models.items():
                                        if existing_model.get('original_id') == model_id:
                                            original_already_exists = True
                                            # Add this endpoint to the available endpoints for this model
                                            if 'available_endpoints' not in existing_model:
                                                existing_model['available_endpoints'] = []
                                            if proxy_prefix not in existing_model['available_endpoints']:
                                                existing_model['available_endpoints'].append(proxy_prefix)
                                            # Update the source_endpoint to be the first one (as per config order)
                                            if 'first_source_endpoint' not in existing_model:
                                                existing_model['first_source_endpoint'] = proxy_prefix
                                            break

                                    if not original_already_exists:
                                        # This is the first occurrence of this original model name
                                        # Use the prefixed version as the ID for display purposes
                                        # Store the model info with its source endpoint
                                        # Preserve existing display setting if it exists, otherwise default to True
                                        existing_display_setting = cached_models.get(final_model_id, {}).get('is_displayed', True)
                                        cached_models[final_model_id] = {
                                            'id': final_model_id,
                                            'original_id': model_id,  # Keep track of original ID
                                            'object': model.get('object', 'model'),
                                            'created': model.get('created', int(time.time())),
                                            'owned_by': model.get('owned_by', 'unknown'),
                                            'source_endpoint': proxy_prefix,  # First endpoint in config order
                                            'available_endpoints': [proxy_prefix],  # All endpoints that provide this model
                                            'is_static': False,  # Mark as dynamic model
                                            'is_displayed': model_display_settings.get(final_model_id, existing_display_setting),  # Apply saved display setting
                                            'redirect_to': model_redirects.get(final_model_id)  # NEW: Include redirect info
                                        }
            except Exception as e:
                import traceback
                logger.error(f"Error fetching models from {provider_endpoint}: {e}")
                logger.error(f"Traceback: {traceback.format_exc()}")
            break


def save_model_display_settings():
    """Save model display settings to the proxy config file."""
    try:
        config = read_config_file()

        # Add model display settings to the config
        config['model_display_settings'] = model_display_settings

        # Write back to file atomically
        write_config_file(config)

        logger.info("Model display settings saved to config")
    except Exception as e:
        import traceback
        logger.error(f"Error saving model display settings: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")


def load_model_display_settings():
    """Load model display settings from the proxy config file."""
    global model_display_settings
    try:
        config = read_config_file()

        # Load model display settings if they exist
        if 'model_display_settings' in config:
            model_display_settings = config['model_display_settings']
            logger.info(f"Loaded {len(model_display_settings)} model display settings from config")
        else:
            logger.info("No model display settings found in config, using defaults")
    except Exception as e:
        import traceback
        logger.error(f"Error loading model display settings: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        model_display_settings = {}


def save_model_routing_settings():
    """Save model routing settings to the proxy config file."""
    try:
        config = read_config_file()

        # Add model routing settings to the config
        config['model_routing_settings'] = custom_model_routing

        # Write back to file atomically
        write_config_file(config)

        logger.info("Model routing settings saved to config")
    except Exception as e:
        import traceback
        logger.error(f"Error saving model routing settings: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")


def load_model_routing_settings():
    """Load model routing settings from the proxy config file."""
    global custom_model_routing
    try:
        config = read_config_file()

        # Load model routing settings if they exist
        if 'model_routing_settings' in config:
            custom_model_routing = config['model_routing_settings']
            logger.info(f"Loaded {len(custom_model_routing)} model routing settings from config")
        else:
            logger.info("No model routing settings found in config, using defaults")
    except Exception as e:
        import traceback
        logger.error(f"Error loading model routing settings: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        custom_model_routing = {}


# NEW: Functions to handle model redirects
def save_model_redirects():
    """Save model redirects to the proxy config file."""
    try:
        config = read_config_file()

        # Add model redirects to the config
        config['model_redirects'] = model_redirects

        # Write back to file atomically
        write_config_file(config)

        logger.info("Model redirects saved to config")
    except Exception as e:
        import traceback
        logger.error(f"Error saving model redirects: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")


def load_model_redirects():
    """Load model redirects from the proxy config file."""
    global model_redirects
    try:
        config = read_config_file()

        # Load model redirects if they exist
        if 'model_redirects' in config:
            model_redirects = config['model_redirects']
            logger.info(f"Loaded {len(model_redirects)} model redirects from config")
        else:
            logger.info("No model redirects found in config, using defaults")
    except Exception as e:
        import traceback
        logger.error(f"Error loading model redirects: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        model_redirects = {}


def load_model_vision_redirects():
    """Load image-to-vision model redirects from the proxy config file."""
    global model_vision_redirects
    try:
        config = read_config_file()

        if 'model_vision_redirects' in config:
            model_vision_redirects = config['model_vision_redirects']
            logger.info(f"Loaded {len(model_vision_redirects)} model vision redirects from config")
        else:
            logger.info("No model vision redirects found in config, using defaults")
    except Exception as e:
        import traceback
        logger.error(f"Error loading model vision redirects: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        model_vision_redirects = {}


def request_contains_image(data):
    """Return True if the chat completion request payload contains image content."""
    if not isinstance(data, dict):
        return False
    messages = data.get('messages')
    if not isinstance(messages, list):
        return False
    for msg in messages:
        content = msg.get('content') if isinstance(msg, dict) else None
        if isinstance(content, str):
            continue
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = str(part.get('type', '')).lower()
                if ptype in ('image', 'image_url', 'input_image'):
                    return True
                # Some providers nest under image_url
                if 'image_url' in part:
                    return True
    return False


def set_vision_redirect(original_model, target_model):
    """Set (or clear) a vision redirect from original_model to target_model."""
    if target_model is None or target_model == '':
        model_vision_redirects.pop(original_model, None)
        logger.info(f"Cleared vision redirect for {original_model}")
    else:
        model_vision_redirects[original_model] = target_model
        logger.info(f"Set vision redirect: {original_model} -> {target_model}")

    try:
        config = read_config_file()
        config['model_vision_redirects'] = model_vision_redirects
        write_config_file(config)
        logger.info("Vision redirects saved to config")
    except Exception as e:
        import traceback
        logger.error(f"Error saving vision redirects: {e}")
        logger.error(traceback.format_exc())


def set_model_redirect(original_model, target_model):
    """Set a redirect from original_model to target_model."""
    model_redirects[original_model] = target_model
    save_model_redirects()
    logger.info(f"Set redirect: {original_model} -> {target_model}")

    # Update cached model with redirect info
    if original_model in cached_models:
        cached_models[original_model]['redirect_to'] = target_model


def save_target_model_config(source_model, target_model):
    """Save target model configuration to the proxy config file."""
    try:
        config = read_config_file()

        # Create target_model_configs section if it doesn't exist
        if 'target_model_configs' not in config:
            config['target_model_configs'] = {}

        # Add the target model configuration
        config['target_model_configs'][source_model] = target_model

        # Write back to file atomically
        write_config_file(config)

        logger.info(f"Target model configuration saved: {source_model} -> {target_model}")
    except Exception as e:
        import traceback
        logger.error(f"Error saving target model configuration: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")


def load_target_model_configs():
    """Load target model configurations from the proxy config file."""
    global cached_models
    try:
        config = read_config_file()

        # Load target model configs if they exist
        if 'target_model_configs' in config:
            target_configs = config['target_model_configs']
            logger.info(f"Loaded {len(target_configs)} target model configurations from config")

            # Update cached models with target configurations
            for source_model, target_model in target_configs.items():
                if source_model in cached_models:
                    cached_models[source_model]['target_model'] = target_model
        else:
            logger.info("No target model configurations found in config")
    except Exception as e:
        import traceback
        logger.error(f"Error loading target model configurations: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")


# ---------------------------------------------------------------------------
# Real-time proxy_config.json sync
# ---------------------------------------------------------------------------

def apply_config(config, broadcast=True):
    """Apply a parsed config to the live in-memory proxy state (hot reload)."""
    global model_display_settings, custom_model_routing, model_redirects
    proxy = globals().get('proxy_server')
    if proxy is None:
        return

    proxy.config = config
    proxy.endpoints = config.get('endpoints', [])
    new_port = config.get('port', 16900)
    if new_port != proxy.port:
        logger.warning(f"Config port changed {proxy.port} -> {new_port} (requires restart to bind)")
        proxy.port = new_port

    # Reload runtime settings dicts from the new config
    model_display_settings = config.get('model_display_settings', {})
    custom_model_routing = config.get('model_routing_settings', {})
    model_redirects = config.get('model_redirects', {})
    _config_state['last_loaded_content'] = json.loads(json.dumps(config))

    logger.info("Config applied to runtime (real-time sync)")
    if broadcast:
        try:
            socketio.emit('config_updated', {'config': config, 'source': 'file'})
        except Exception as e:
            logger.error(f"Failed to broadcast config_updated: {e}")


def reload_config_from_disk():
    """Read proxy_config.json, apply it if the content actually changed.

    Self-originated writes (UI saves) are skipped here because the save
    handlers already applied the change in memory; external edits are applied
    immediately and pushed to every connected monitor client.
    """
    try:
        cfg = read_config_file()
    except Exception as e:
        logger.error(f"Config reload failed: {e}")
        return

    last_self = _config_state.get('last_self_content')
    if last_self is not None and cfg == last_self:
        _config_state['last_loaded_content'] = cfg
        return

    last_loaded = _config_state.get('last_loaded_content')
    if last_loaded is not None and cfg == last_loaded:
        return

    apply_config(cfg, broadcast=True)
    refresh_models_in_background()


def refresh_models_in_background():
    """Re-fetch provider models after a config change and notify all clients."""
    def _worker():
        try:
            fetch_all_models(refresh=True)
            socketio.emit('models_updated', {
                'models': list(cached_models.values()),
                'endpoints': proxy_server.endpoints if 'proxy_server' in globals() else [],
                'redirects': model_redirects,
                'message': 'Config sync: models refreshed'
            })
        except Exception as e:
            import traceback
            logger.error(f"Background model refresh failed: {e}")
            logger.error(traceback.format_exc())
    Thread(target=_worker, daemon=True, name='model-refresh').start()


def start_config_watcher(interval=1.0):
    """Poll proxy_config.json for external edits and hot-reload on change."""
    try:
        last_mtime = os.path.getmtime(CONFIG_PATH)
    except OSError:
        last_mtime = None

    def _watch():
        nonlocal last_mtime
        while True:
            time.sleep(interval)
            try:
                mtime = os.path.getmtime(CONFIG_PATH)
            except OSError:
                continue
            if mtime != last_mtime:
                last_mtime = mtime
                reload_config_from_disk()

    t = Thread(target=_watch, daemon=True, name='config-watcher')
    t.start()
    logger.info(f"Config watcher started for {CONFIG_PATH}")


ZOMBIE_MAX_AGE_SECONDS = 2 * 60 * 60  # 2 hours; real requests never last this long


def start_zombie_sweeper(interval=60):
    """Periodically purge impossible long-lived connections/streams.

    If a request tracked a connection but never cleaned it up (e.g. an early
    return path before the response close hook), it would remain in
    active_connections forever and show up in the monitor as a multi-hundred
    minute zombie. This sweep is a safety net that removes anything older than
    ZOMBIE_MAX_AGE_SECONDS.
    """
    def _sweep():
        while True:
            time.sleep(interval)
            now = time.time()
            removed_conn = 0
            removed_stream = 0
            with active_connections_lock:
                for rid in list(active_connections.keys()):
                    conn = active_connections[rid]
                    start = conn.get('start_time')
                    if isinstance(start, (int, float)) and (now - start) > ZOMBIE_MAX_AGE_SECONDS:
                        del active_connections[rid]
                        removed_conn += 1
            with active_streams_lock:
                for rid in list(active_streams.keys()):
                    stream = active_streams[rid]
                    start = stream.get('start_time')
                    if isinstance(start, (int, float)) and (now - start) > ZOMBIE_MAX_AGE_SECONDS:
                        del active_streams[rid]
                        removed_stream += 1
            if removed_conn or removed_stream:
                logger.warning(
                    "Zombie sweep removed %d connection(s), %d stream(s)",
                    removed_conn, removed_stream,
                )
                try:
                    socketio.emit('server_stats_update', {
                        'active_connections': len(active_connections),
                        'active_streams': len(active_streams),
                        'total_messages': server_stats.get('total_messages', 0)
                    })
                except Exception as e:
                    logger.error(f"Failed to emit stats after zombie sweep: {e}")

    t = Thread(target=_sweep, daemon=True, name='zombie-sweeper')
    t.start()
    logger.info("Zombie sweeper started")


@socketio.on('request_config')
def handle_request_config():
    """Send the current proxy_config.json content to the requesting client."""
    from flask_socketio import emit
    try:
        cfg = read_config_file()
        emit('config_updated', {'config': cfg, 'source': 'request'})
    except Exception as e:
        emit('config_update_error', {'message': f'Failed to read config: {e}'})


@socketio.on('request_config_reload')
def handle_request_config_reload():
    """Force a reload of proxy_config.json from disk and broadcast the result."""
    reload_config_from_disk()


@socketio.on('save_config_json')
def handle_save_config_json(data):
    """Validate, atomically persist, and hot-apply a full config from the UI."""
    from flask_socketio import emit
    raw = data.get('config')
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception as e:
            emit('config_update_error', {'message': f'Invalid JSON: {e}'})
            return
    if not isinstance(raw, dict):
        emit('config_update_error', {'message': 'Config must be a JSON object'})
        return

    try:
        endpoints = raw.get('endpoints', [])
        if not isinstance(endpoints, list) or not endpoints:
            emit('config_update_error', {'message': 'Config must contain a non-empty "endpoints" array'})
            return
        for ep in endpoints:
            if not isinstance(ep, dict) or not ep.get('proxy_path_prefix'):
                emit('config_update_error', {'message': 'Each endpoint requires a proxy_path_prefix'})
                return

        write_config_file(raw)
        apply_config(raw, broadcast=False)
        socketio.emit('config_updated', {
            'config': raw,
            'source': 'ui',
            'message': 'Config saved and applied'
        })
        refresh_models_in_background()
    except Exception as e:
        emit('config_update_error', {'message': f'Failed to save config: {e}'})


if __name__ == '__main__':
    proxy_server = APIProxyServer('./proxy_config.json')
    # Store the instance in the global variable so the aggregated endpoint can access it
    globals()['proxy_server_instance'] = proxy_server

    # Load model display, routing, redirect, and target model settings from config
    load_model_display_settings()
    load_model_routing_settings()
    load_model_redirects()  # NEW: Load model redirects
    load_model_vision_redirects()  # NEW: Load model vision redirects
    load_target_model_configs()  # NEW: Load target model configurations
    load_endpoint_target_configs()  # NEW: Load endpoint target configurations

    # Fetch all models on startup
    fetch_all_models()

    proxy_server.run(host='0.0.0.0')
