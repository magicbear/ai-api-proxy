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

# Initialize Flask app with SocketIO
app = Flask(__name__, static_url_path='', static_folder='.')
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
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = json.load(f)
        
        self.endpoints = self.config.get('endpoints', [])
        self.port = self.config.get('port', 16900)
        
    def setup_routes(self):
        # Serve monitor HTML
        @app.route('/monitor')
        def monitor():
            return send_from_directory('.', 'monitor.html')
        
        # Setup proxy routes for each endpoint
        for i, endpoint in enumerate(self.endpoints):
            self.setup_endpoint_route(endpoint, i)
        
        # Catch-all for debugging
        @app.route('/', defaults={'path': ''})
        @app.route('/<path:path>')
        def catch_all(path):
            return jsonify({"error": "Endpoint not configured"}), 404
    
    def setup_endpoint_route(self, endpoint, idx):
        prefix = endpoint['proxy_path_prefix']
        
        # Create a unique function name for each endpoint
        def make_proxy_handler(ep_config):
            def proxy_route(subpath=''):
                return self.handle_proxy_request(request, ep_config, subpath)
            proxy_route.__name__ = f"proxy_route_{idx}"  # Unique name to avoid conflicts
            return proxy_route
        
        handler = make_proxy_handler(endpoint)
        
        # Dynamic route based on the prefix
        app.add_url_rule(
            f'{prefix}/<path:subpath>',
            endpoint=f'proxy_{idx}_with_path',
            view_func=handler,
            methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS']
        )
        
        app.add_url_rule(
            f'{prefix}',
            endpoint=f'proxy_{idx}_root',
            view_func=handler,
            methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS'],
            defaults={'subpath': ''}
        )
    
    def handle_proxy_request(self, req, endpoint_config, subpath, original_model_id=None):
        request_id = f"{int(time.time())}-{hash(req.url) % 10000}"
        
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
            # Add prefixes to static models for display
            prefixed_static_models = []
            for model in static_models:
                if isinstance(model, str):
                    # If it's a string, add prefix if needed
                    if '/' not in model:
                        # Check if the model name matches any known prefixes
                        matched_prefix = get_model_prefix(model)
                        if matched_prefix:
                            prefixed_model = f"{matched_prefix}/{model}"
                        else:
                            # Use the source endpoint as a prefix if no specific prefix matches
                            source_endpoint = endpoint_config.get('proxy_path_prefix', 'unknown')
                            clean_prefix = source_endpoint.lstrip('/').replace('/', '-')
                            prefixed_model = f"{clean_prefix}/{model}"
                    else:
                        prefixed_model = model  # Already has prefix
                    prefixed_static_models.append(prefixed_model)
                else:
                    # If it's an object, add prefix to the id field if needed
                    model_obj = model.copy()
                    if 'id' in model_obj:
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
            if any(keyword in processed_subpath.lower() for keyword in ['chat', 'completions', 'embeddings', 'images', 'audio', 'moderations']):
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
        
        # Add API key if configured
        api_key_env = endpoint_config.get('api_key_env', '')
        if api_key_env and api_key_env.strip():
            api_key = os.environ.get(api_key_env)
            if api_key:
                headers[endpoint_config['api_key_header']] = f"{endpoint_config['api_key_prefix']}{api_key}"
            else:
                logger.warning(f"No API key found for {api_key_env}")
        
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
        
        # Track stream if applicable
        if is_chat_completions and is_streaming:
            stream_info = {
                'id': request_id,
                'url': req.full_path,
                'timestamp': datetime.now().isoformat(),
                'start_time': time.time(),
                'status': 'started',
                'endpoint': endpoint_prefix,
                'target_url': target_base_url
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
            
            if req.method == 'GET':
                response = requests.get(target_url, headers=headers, params=req.args, stream=True)
            elif req.method == 'POST':
                response = requests.post(target_url, headers=headers, data=data, stream=True)
            elif req.method == 'PUT':
                response = requests.put(target_url, headers=headers, data=data, stream=True)
            elif req.method == 'DELETE':
                response = requests.delete(target_url, headers=headers, stream=True)
            elif req.method == 'PATCH':
                response = requests.patch(target_url, headers=headers, data=data, stream=True)
            else:
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
                            
                            # Process chunk as bytes initially
                            if isinstance(chunk, bytes):
                                chunk_str = chunk.decode('utf-8')
                            else:
                                chunk_str = chunk
                            
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
                            
                            # Yield chunk for proper forwarding to client
                            yield chunk_str
                finally:
                    on_response_close()
            
            # Return streaming response
            response_headers = dict(response.headers.items())
            
            # Remove problematic encoding headers
            response_headers.pop('Content-Encoding', None)
            response_headers.pop('Transfer-Encoding', None)
            response_headers.pop('Connection', None)
            
            # Add proper content-type if missing
            if 'Content-Type' not in response_headers:
                response_headers['Content-Type'] = 'application/json'
            
            return Response(
                generate(),
                status=response.status_code,
                headers=response_headers
            )
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Request to target API failed: {e}")
            
            # Clean up tracking
            if request_id in active_connections:
                del active_connections[request_id]
                try:
                    socketio.emit('connection_removed', {'id': request_id})
                except Exception as e:
                    logger.error(f"Failed to emit connection_removed event: {e}")
                
                if is_chat_completions and is_streaming and request_id in active_streams:
                    del active_streams[request_id]
                    try:
                        socketio.emit('stream_finished', {
                            'id': request_id,
                            'timestamp': datetime.now().isoformat()
                        })
                    except Exception as e:
                        logger.error(f"Failed to emit stream_finished event: {e}")
            
            return jsonify({"error": "Failed to connect to target API"}), 502
    
    def run(self, host='0.0.0.0'):
        """Run the Flask server with SocketIO."""
        logger.info(f"Starting proxy server on {host}:{self.port}")
        logger.info(f"Monitor interface available at http://{host}:{self.port}/monitor")
        
        # Fetch models from all endpoints on startup
        fetch_all_models()
        
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
    
    # Load config locally
    with open('./proxy_config.json', 'r', encoding='utf-8') as f:
        config_local = json.load(f)
    
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
                if endpoint.get('api_key_env'):
                    api_key = os.environ.get(endpoint['api_key_env'])
                    if api_key:
                        headers[endpoint['api_key_header']] = f"{endpoint['api_key_prefix']}{api_key}"
                
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
        
        # Call the proxy handler with the correctly transformed model name
        if proxy_server_instance:
            # Set the final backend model name as an attribute for the proxy handler
            request.original_model_id = backend_model_name
            return proxy_server_instance.handle_proxy_request(request, selected_endpoint, 'v1/chat/completions')
        else:
            # Fallback: set the attribute and call the aggregated handler
            request.original_model_id = backend_model_name
            return handle_aggregated_request(request, selected_endpoint)
        
    except Exception as e:
        logger.error(f"Error in aggregated chat completions: {e}")
        return jsonify({"error": str(e)}), 500


def handle_aggregated_request(flask_request, endpoint_config):
    """Handle aggregated request using the same logic as the proxy, ensuring monitoring works."""
    request_id = f"{int(time.time())}-{hash(flask_request.url) % 10000}"
    
    # Track connection
    # Extract the endpoint prefix for display in monitoring
    endpoint_prefix = endpoint_config.get('proxy_path_prefix', 'unknown')
    target_base_url = endpoint_config.get('target_base_url', 'unknown')
    
    # Extract model from request if it's a chat completion request
    model_name = None
    if flask_request.is_json and ('/chat/completions' in flask_request.full_path or '/v1/chat' in flask_request.full_path):
        try:
            json_data = flask_request.get_json()
            if json_data and isinstance(json_data, dict):
                model_name = json_data.get('model', None)
        except:
            pass  # If JSON parsing fails, continue without model info

    # Calculate request size
    request_size = len(flask_request.get_data()) if flask_request.get_data() else 0

    connection_info = {
        'id': request_id,
        'method': flask_request.method,
        'url': flask_request.full_path,
        'timestamp': datetime.now().isoformat(),
        'headers': dict(flask_request.headers),
        'remote_address': flask_request.remote_addr,
        'endpoint': endpoint_prefix,  # Show the endpoint being used
        'target_url': target_base_url,  # Show the upstream target
        'model': model_name,  # Add model information to connection info
        'request_size': request_size  # Add request size in bytes
    }
    
    with active_connections_lock:
        active_connections[request_id] = connection_info
    # Use emit with callback to ensure it's sent
    try:
        socketio.emit('connection_added', {'connection': connection_info})
    except Exception as e:
        logger.error(f"Failed to emit connection_added event: {e}")
    
    # Determine target URL - for aggregated, we're calling v1/chat/completions on the target
    target_base = endpoint_config.get('target_base_url', '')
    
    # Check if this is a pure proxy endpoint without target_base_url
    if not target_base or target_base.strip() == '':
        # For pure proxy endpoints, we cannot forward to a target
        return jsonify({
            "error": "Cannot process chat completion request on pure proxy endpoint without target",
            "endpoint": endpoint_config['proxy_path_prefix']
        }), 400
    
    target_url = urljoin(target_base, 'v1/chat/completions')
    
    # Prepare headers
    headers = dict(flask_request.headers)
    
    # Update Host header to match the target URL
    from urllib.parse import urlparse
    target_parsed = urlparse(target_url)
    headers['Host'] = target_parsed.netloc
    
    # Add API key if configured
    api_key_env = endpoint_config.get('api_key_env', '')
    if api_key_env and api_key_env.strip():
        api_key = os.environ.get(api_key_env)
        if api_key:
            headers[endpoint_config['api_key_header']] = f"{endpoint_config['api_key_prefix']}{api_key}"
        else:
            logger.warning(f"No API key found for {api_key_env}")
    
    # Get original model name from the JSON data and restore it before sending to backend
    data = flask_request.get_data()
    try:
        json_data = json.loads(data.decode('utf-8'))
        
        # Check if original_model_id was attached to the request from aggregated function
        # This handles model redirects that were resolved in the aggregated function
        target_original_model_id = getattr(flask_request, 'original_model_id', None)
        if target_original_model_id:
            # Use the original model ID passed from the aggregated function
            # This already accounts for any model redirects
            json_data['model'] = target_original_model_id
            logger.info(f"Using provided original model ID in aggregated handler: {target_original_model_id}")
        else:
            # Restore original model name for backend (fallback behavior)
            original_model_id = json_data.get('model', '')
            # Find the original model ID if the request contains a prefixed model ID
            if original_model_id in cached_models:
                # If it's a prefixed model, get the original ID
                original_model_id = cached_models[original_model_id].get('original_id', original_model_id)
            json_data['model'] = original_model_id
        
        data = json.dumps(json_data).encode('utf-8')
    except Exception as e:
        logger.error(f"Error modifying request data: {e}")
    
    # Special handling for chat completions with streaming
    is_chat_completions = True  # This is definitely a chat completion request
    is_streaming = False
    
    try:
        json_data = json.loads(data.decode('utf-8'))
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
    
    # Track stream if applicable
    if is_streaming:
        # Extract the endpoint prefix for display in monitoring
        endpoint_prefix = endpoint_config.get('proxy_path_prefix', 'unknown')
        target_base_url = endpoint_config.get('target_base_url', 'unknown')
        
        stream_info = {
            'id': request_id,
            'url': flask_request.full_path,
            'timestamp': datetime.now().isoformat(),
            'start_time': time.time(),  # Add start time for duration calculation
            'status': 'started',
            'endpoint': endpoint_prefix,  # Show the endpoint being used
            'target_url': target_base_url  # Show the upstream target
        }
        with active_streams_lock:
            active_streams[request_id] = stream_info
        
        # Update server stats
        with server_stats_lock:
            server_stats['active_streams'] = len(active_streams)
            server_stats['total_messages'] += 1
        
        # Use emit with callback to ensure it's sent
        try:
            socketio.emit('stream_started', {'stream': stream_info})
            # Broadcast updated stats to all clients
            socketio.emit('server_stats_update', {
                'active_connections': server_stats['active_connections'],
                'active_streams': server_stats['active_streams'],
                'total_messages': server_stats['total_messages']
            })
        except Exception as e:
            import traceback
            logger.error(f"Failed to emit stream_started event: {e}")
            logger.error(f"Traceback: {traceback.format_exc()}")
    
    try:
        # Make the request to the target endpoint
        # Set User-Agent to mimic OpenAI/JS client
        headers['User-Agent'] = 'OpenAI/JS 6.26.0'
        
        if flask_request.method == 'GET':
            response = requests.get(target_url, headers=headers, params=flask_request.args, stream=True)
        elif flask_request.method == 'POST':
            response = requests.post(target_url, headers=headers, data=data, stream=True)
        elif flask_request.method == 'PUT':
            response = requests.put(target_url, headers=headers, data=data, stream=True)
        elif flask_request.method == 'DELETE':
            response = requests.delete(target_url, headers=headers, stream=True)
        elif flask_request.method == 'PATCH':
            response = requests.patch(target_url, headers=headers, data=data, stream=True)
        else:
            return jsonify({"error": f"Method {flask_request.method} not supported"}), 405
        
        # Initialize response_size variable to be accessible in on_response_close
        # We'll use a mutable container to hold the value so it can be modified by inner functions
        response_size_container = {'size': 0}
        
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
                import traceback
                logger.error(f"Failed to emit connection_removed event: {e}")
                logger.error(f"Traceback: {traceback.format_exc()}")
            
            if is_streaming:
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
                    import traceback
                    logger.error(f"Failed to emit stream_finished event: {e}")
                    logger.error(f"Traceback: {traceback.format_exc()}")
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
        
        # Stream the response back to the client
        # Initialize response_size variable to be accessible in on_response_close
        # We'll use a mutable container to hold the value so it can be modified by inner functions
        response_size_container = {'size': 0}
        
        def generate():
            try:
                # Check if the response is gzipped and handle accordingly
                is_gzipped = response.headers.get('Content-Encoding', '') == 'gzip'
                
                for chunk in response.iter_content(chunk_size=1024):
                    if chunk:
                        # Track response size
                        response_size_container['size'] += len(chunk)
                        
                        # If the response is gzipped, decompress it before sending
                        if is_gzipped:
                            import gzip
                            try:
                                # Decompress the chunk
                                chunk = gzip.decompress(chunk)
                            except Exception as e:
                                logger.error(f"Error decompressing chunk: {e}")
                                # If decompression fails, use the original chunk
                                pass
                        
                        # Process chunk as bytes initially
                        if isinstance(chunk, bytes):
                            chunk_str = chunk.decode('utf-8')
                        else:
                            chunk_str = chunk
                        
                        # Check if this chunk contains usage information for token stats
                        # Check for data lines regardless of streaming flag
                        if chunk_str.startswith('data: '):
                            try:
                                # Use the current endpoint as the provider for token stats
                                provider = endpoint_config['proxy_path_prefix'].strip('/').split('/')[-1] or endpoint_config['proxy_path_prefix'].strip('/').split('/')[0]
                                
                                # Parse the usage data from the chunk
                                # Find JSON objects in the chunk (it's in "data: {...}" format)
                                if chunk_str.startswith('data: ') and chunk_str != 'data: [DONE]\n':
                                    json_str = chunk_str[6:].strip()  # Remove 'data: ' prefix
                                    if json_str and json_str != '[DONE]':
                                        try:
                                            data_obj = json.loads(json_str)
                                            if 'usage' in data_obj and data_obj['usage'] and data_obj['usage'] != {}:
                                                usage = data_obj['usage']
                                                # Update token statistics
                                                if provider not in token_stats:
                                                    token_stats[provider] = {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}
                                                
                                                # Update stats - only add values that exist
                                                if 'prompt_tokens' in usage:
                                                    token_stats[provider]['prompt_tokens'] += usage['prompt_tokens']
                                                if 'completion_tokens' in usage:
                                                    token_stats[provider]['completion_tokens'] += usage['completion_tokens']
                                                if 'total_tokens' in usage:
                                                    token_stats[provider]['total_tokens'] += usage['total_tokens']
                                                
                                                # Emit token stats update
                                                try:
                                                    socketio.emit('token_stats_update', {'stats': token_stats})
                                                except Exception as e:
                                                    import traceback
                                                    logger.error(f"Failed to emit token_stats_update event: {e}")
                                                    logger.error(f"Traceback: {traceback.format_exc()}")
                                        except json.JSONDecodeError:
                                            # This chunk doesn't contain valid JSON, continue
                                            pass
                            except Exception as e:
                                import traceback
                                logger.error(f"Error processing usage data: {e}")
                                logger.error(f"Traceback: {traceback.format_exc()}")
                        
                        # Emit stream data event for real-time monitoring
                        # Monitor all responses that look like streams, not just explicitly streamed ones
                        if chunk_str.startswith('data: '):
                            try:
                                # Parse the chunk to extract delta content if it's a data: line
                                if chunk_str.startswith('data: ') and chunk_str != 'data: [DONE]\n':
                                    json_str = chunk_str[6:].strip()  # Remove 'data: ' prefix
                                    if json_str and json_str != '[DONE]':
                                        try:
                                            data_obj = json.loads(json_str)
                                            # Extract delta content for chat completions
                                            if 'choices' in data_obj and len(data_obj['choices']) > 0:
                                                choice = data_obj['choices'][0]
                                                if 'delta' in choice:
                                                    # Send the entire delta object, not just content
                                                    # This includes content, role, etc.
                                                    delta_data = choice['delta']
                                                    stream_data = {
                                                        'id': request_id,
                                                        'delta': delta_data,
                                                        'timestamp': datetime.now().isoformat()
                                                    }
                                                    socketio.emit('stream_chunk', {'data': stream_data})
                                                else:
                                                    # If no delta, send the whole parsed data object to see structure
                                                    stream_data = {
                                                        'id': request_id,
                                                        'parsed_data': data_obj,  # Send parsed structure to see what's there
                                                        'timestamp': datetime.now().isoformat()
                                                    }
                                                    socketio.emit('stream_chunk', {'data': stream_data})
                                            else:
                                                # If no choices, send the whole parsed data object
                                                stream_data = {
                                                    'id': request_id,
                                                    'parsed_data': data_obj,  # Send parsed structure to see what's there
                                                    'timestamp': datetime.now().isoformat()
                                                }
                                                socketio.emit('stream_chunk', {'data': stream_data})
                                        except json.JSONDecodeError:
                                            # If it's not valid JSON, emit as general chunk
                                            stream_data = {
                                                'id': request_id,
                                                'chunk': chunk_str[:200] + '...' if len(chunk_str) > 200 else chunk_str,  # Limit chunk size for performance
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
                                        'chunk': chunk_str[:200] + '...' if len(chunk_str) > 200 else chunk_str,  # Limit chunk size for performance
                                        'timestamp': datetime.now().isoformat()
                                    }
                                    socketio.emit('stream_chunk', {'data': stream_data})
                            except:
                                pass  # Silently ignore stream chunk emission failures
                        
                        # Yield chunk for proper forwarding to client
                        yield chunk_str
            finally:
                on_response_close()
        
        # Prepare response headers, removing problematic encoding headers
        response_headers = dict(response.headers.items())
        
        # Remove content-encoding and transfer-encoding that may cause issues when proxying
        response_headers.pop('Content-Encoding', None)
        response_headers.pop('Transfer-Encoding', None)
        response_headers.pop('Connection', None)  # Let Flask/Werkzeug handle connection
        
        # Add proper content-type if missing
        if 'Content-Type' not in response_headers:
            response_headers['Content-Type'] = 'application/json'
        
        return Response(
            generate(),
            status=response.status_code,
            headers=response_headers
        )
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Request to target API failed: {e}")
        
        # Clean up tracking
        if request_id in active_connections:
            del active_connections[request_id]
            try:
                socketio.emit('connection_removed', {'id': request_id})
            except Exception as e:
                logger.error(f"Failed to emit connection_removed event: {e}")
            
            if is_streaming and request_id in active_streams:
                del active_streams[request_id]
                try:
                    socketio.emit('stream_finished', {
                        'id': request_id,
                        'timestamp': datetime.now().isoformat()
                    })
                except Exception as e:
                    logger.error(f"Failed to emit stream_finished event: {e}")
        
        return jsonify({"error": "Failed to connect to target API"}), 502


@socketio.on('disconnect')
def handle_disconnect():
    """Handle WebSocket disconnections."""
    logger.info("Monitor client disconnected")


# Socket.IO event handlers for model routing configuration
@socketio.on('request_initial_models')
def handle_request_initial_models():
    """Send initial models data to the client."""
    from flask_socketio import emit
    # Ensure models are fresh
    current_cached_models = get_cached_models()
    emit('models_updated', {
        'models': list(current_cached_models.values()),
        'endpoints': proxy_server.endpoints if 'proxy_server' in globals() else [],
        'redirects': model_redirects  # NEW: Include redirects in the response
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
    emit('models_updated', {
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
    
    emit('models_updated', {
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
    
    emit('models_updated', {
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
        emit('models_updated', {
            'models': list(cached_models.values()),
            'endpoints': proxy_server.endpoints if 'proxy_server' in globals() else [],
            'redirects': model_redirects,
            'message': f'Model redirect set: {original_model} -> {target_model}'
        })
    else:
        emit('error', {'message': 'Both original and target models must be specified'})


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
        emit('models_updated', {
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
        emit('models_updated', {
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
        emit('models_updated', {
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
        # Load existing config
        with open('./proxy_config.json', 'r', encoding='utf-8') as f:
            config = json.load(f)
        
        # Update the endpoint with fixed models
        for endpoint in config['endpoints']:
            if endpoint['proxy_path_prefix'] == endpoint_prefix:
                endpoint['models'] = fixed_models
                break
        
        # Write back to file
        with open('./proxy_config.json', 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        
        logger.info(f"Fixed models configuration saved for {endpoint_prefix}: {fixed_models}")
    except Exception as e:
        import traceback
        logger.error(f"Error saving fixed models configuration: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")


def save_target_model_for_endpoint(endpoint_prefix, target_model):
    """Save target model configuration for an endpoint to the proxy config file."""
    try:
        # Load existing config
        with open('./proxy_config.json', 'r', encoding='utf-8') as f:
            config = json.load(f)
        
        # Create endpoint_target_configs section if it doesn't exist
        if 'endpoint_target_configs' not in config:
            config['endpoint_target_configs'] = {}
        
        # Add the target model configuration for the endpoint
        config['endpoint_target_configs'][endpoint_prefix] = target_model
        
        # Write back to file
        with open('./proxy_config.json', 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        
        logger.info(f"Target model configuration saved for endpoint {endpoint_prefix}: {target_model}")
    except Exception as e:
        import traceback
        logger.error(f"Error saving target model configuration for endpoint: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")


def load_endpoint_target_configs():
    """Load endpoint target model configurations from the proxy config file."""
    try:
        # Load existing config
        with open('./proxy_config.json', 'r', encoding='utf-8') as f:
            config = json.load(f)
        
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
        with open('./proxy_config.json', 'r', encoding='utf-8') as f:
            config = json.load(f)
        
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
    with open('./proxy_config.json', 'r', encoding='utf-8') as f:
        config_local = json.load(f)
    
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
                    if endpoint.get('api_key_env'):
                        api_key = os.environ.get(endpoint['api_key_env'])
                        if api_key:
                            headers[endpoint['api_key_header']] = f"{endpoint['api_key_prefix']}{api_key}"
                    
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
        # Load existing config
        with open('./proxy_config.json', 'r', encoding='utf-8') as f:
            config = json.load(f)
        
        # Add model display settings to the config
        config['model_display_settings'] = model_display_settings
        
        # Write back to file
        with open('./proxy_config.json', 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        
        logger.info("Model display settings saved to config")
    except Exception as e:
        import traceback
        logger.error(f"Error saving model display settings: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")


def load_model_display_settings():
    """Load model display settings from the proxy config file."""
    global model_display_settings
    try:
        # Load existing config
        with open('./proxy_config.json', 'r', encoding='utf-8') as f:
            config = json.load(f)
        
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
        # Load existing config
        with open('./proxy_config.json', 'r', encoding='utf-8') as f:
            config = json.load(f)
        
        # Add model routing settings to the config
        config['model_routing_settings'] = custom_model_routing
        
        # Write back to file
        with open('./proxy_config.json', 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        
        logger.info("Model routing settings saved to config")
    except Exception as e:
        import traceback
        logger.error(f"Error saving model routing settings: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")


def load_model_routing_settings():
    """Load model routing settings from the proxy config file."""
    global custom_model_routing
    try:
        # Load existing config
        with open('./proxy_config.json', 'r', encoding='utf-8') as f:
            config = json.load(f)
        
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
        # Load existing config
        with open('./proxy_config.json', 'r', encoding='utf-8') as f:
            config = json.load(f)
        
        # Add model redirects to the config
        config['model_redirects'] = model_redirects
        
        # Write back to file
        with open('./proxy_config.json', 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        
        logger.info("Model redirects saved to config")
    except Exception as e:
        import traceback
        logger.error(f"Error saving model redirects: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")


def load_model_redirects():
    """Load model redirects from the proxy config file."""
    global model_redirects
    try:
        # Load existing config
        with open('./proxy_config.json', 'r', encoding='utf-8') as f:
            config = json.load(f)
        
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
        # Load existing config
        with open('./proxy_config.json', 'r', encoding='utf-8') as f:
            config = json.load(f)
        
        # Create target_model_configs section if it doesn't exist
        if 'target_model_configs' not in config:
            config['target_model_configs'] = {}
        
        # Add the target model configuration
        config['target_model_configs'][source_model] = target_model
        
        # Write back to file
        with open('./proxy_config.json', 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        
        logger.info(f"Target model configuration saved: {source_model} -> {target_model}")
    except Exception as e:
        import traceback
        logger.error(f"Error saving target model configuration: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")


def load_target_model_configs():
    """Load target model configurations from the proxy config file."""
    global cached_models
    try:
        # Load existing config
        with open('./proxy_config.json', 'r', encoding='utf-8') as f:
            config = json.load(f)
        
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


if __name__ == '__main__':
    proxy_server = APIProxyServer('./proxy_config.json')
    # Store the instance in the global variable so the aggregated endpoint can access it
    globals()['proxy_server_instance'] = proxy_server
    
    # Load model display, routing, redirect, and target model settings from config
    load_model_display_settings()
    load_model_routing_settings()
    load_model_redirects()  # NEW: Load model redirects
    load_target_model_configs()  # NEW: Load target model configurations
    load_endpoint_target_configs()  # NEW: Load endpoint target configurations
    
    # Fetch all models on startup
    fetch_all_models()
    
    proxy_server.run(host='0.0.0.0')
