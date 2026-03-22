logger.error(f"Error loading target model configurations: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")


# Catch-all for debugging - put this at the end after all specific routes
@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def catch_all(path):
    return jsonify({"error": "Endpoint not configured"}), 404


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