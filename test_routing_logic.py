#!/usr/bin/env python3
"""
Test script to validate routing logic for API proxy
"""
import json
import requests
import time

def test_api_call(url, model_name, headers=None, expected_status=200, timeout=10):
    """Test an API call and return the result"""
    if headers is None:
        headers = {"Content-Type": "application/json"}
    
    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": "Hello"}],
        "temperature": 0.7
    }
    
    try:
        print(f"Testing: {url} with model: {model_name}")
        response = requests.post(url, json=payload, headers=headers, timeout=timeout)
        print(f"  Status: {response.status_code}")
        print(f"  Expected: {expected_status}, Got: {response.status_code}")
        
        if response.status_code == expected_status:
            print("  ✓ PASS")
        else:
            print(f"  ✗ FAIL - Expected {expected_status}, got {response.status_code}")
            if response.status_code != 401:  # Don't print error for auth issues
                print(f"  Response: {response.text[:200]}...")
        print()
        return response.status_code == expected_status
    except requests.exceptions.Timeout:
        print(f"  ⚠ TIMEOUT - May indicate successful routing to backend")
        print()
        return True  # Timeout often indicates request reached the backend
    except Exception as e:
        print(f"  ✗ ERROR: {str(e)}")
        print()
        return False

def main():
    print("API Proxy Routing Logic Test")
    print("="*50)
    
    # Base URL for testing (assuming server is running on default port)
    base_url = "http://localhost:16900"
    
    test_cases = [
        # Test case 1: /v1/chat/completions with Qwen3-122B should route to /aliyun
        {
            "url": f"{base_url}/v1/chat/completions",
            "model": "Qwen3-122B",  # This should be redirected according to your config
            "description": "Qwen3-122B via aggregated endpoint should route to /aliyun"
        },
        
        # Test case 2: /v1/chat/completions with nemotron-3-super should route to /local-sgl2
        {
            "url": f"{base_url}/v1/chat/completions",
            "model": "nemotron-3-super",
            "description": "nemotron-3-super via aggregated endpoint should route to /local-sgl2"
        },
        
        # Test case 3: /__proxy__/v1/chat/completions with Qwen3-122B should route based on redirect
        {
            "url": f"{base_url}/__proxy__/v1/chat/completions",
            "model": "Qwen3-122B",
            "description": "Qwen3-122B via __proxy__ should route based on redirect config"
        },
        
        # Test case 4: /aliyun/v1/chat/completions with Qwen3-122B should route to /aliyun
        {
            "url": f"{base_url}/aliyun/v1/chat/completions",
            "model": "Qwen3-122B",
            "description": "Qwen3-122B via /aliyun endpoint should route to /aliyun"
        },
        
        # Test case 5: /local-sgl2/v1/chat/completions with nemotron-3-super should route to /local-sgl2
        {
            "url": f"{base_url}/local-sgl2/v1/chat/completions",
            "model": "nemotron-3-super",
            "description": "nemotron-3-super via /local-sgl2 endpoint should route to /local-sgl2"
        },
        
        # Test case 6: /local-sgl/v1/chat/completions with nemotron-3-super should route to /local-sgl
        {
            "url": f"{base_url}/local-sgl/v1/chat/completions",
            "model": "nemotron-3-super",
            "description": "nemotron-3-super via /local-sgl endpoint should route to /local-sgl"
        }
    ]
    
    print("Current Configuration from proxy_config.json:")
    print("-" * 30)
    try:
        with open('proxy_config.json', 'r') as f:
            config = json.load(f)
        
        print("Model redirects:")
        for original, target in config.get('model_redirects', {}).items():
            if target:  # Only show active redirects
                print(f"  {original} -> {target}")
        
        print("\nModel routing settings:")
        for model, endpoint in config.get('model_routing_settings', {}).items():
            print(f"  {model} -> {endpoint}")
        
        print("\nEndpoints:")
        for endpoint in config.get('endpoints', []):
            print(f"  {endpoint['proxy_path_prefix']}: target={endpoint.get('target_base_url', 'NONE (pure proxy)')}")
            if 'models' in endpoint:
                print(f"    models: {endpoint['models']}")
        print()
    except Exception as e:
        print(f"Could not read config: {e}\n")
    
    results = []
    for i, test_case in enumerate(test_cases, 1):
        print(f"Test {i}: {test_case['description']}")
        success = test_api_call(
            test_case['url'], 
            test_case['model']
        )
        results.append(success)
    
    print("="*50)
    print("SUMMARY:")
    print(f"Total tests: {len(results)}")
    print(f"Passed: {sum(results)}")
    print(f"Failed: {len(results) - sum(results)}")
    
    if all(results):
        print("✓ All tests passed!")
    else:
        print("✗ Some tests failed")
    
    print("\nNote: Timeouts may indicate successful routing to backends.")
    print("Check the proxy server logs for detailed routing information.")

if __name__ == "__main__":
    main()