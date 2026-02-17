"""
Context Quilt - End-to-End Verification Script (Standard Lib)
"""

import urllib.request
import urllib.error
import json
import time

API_URL = "http://localhost:8000"
USER_ID = "test_user_123"
APP_ID = "test_app_01"

def print_step(step):
    print(f"\n=== {step} ===")

def make_request(method, endpoint, data=None, params=None, headers=None):
    url = f"{API_URL}{endpoint}"
    if params:
        query = "&".join([f"{k}={v}" for k, v in params.items()])
        url = f"{url}?{query}"
    
    req = urllib.request.Request(url, method=method)
    
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
            
    if data:
        json_data = json.dumps(data).encode('utf-8')
        req.add_header('Content-Type', 'application/json')
        req.data = json_data
        
    try:
        with urllib.request.urlopen(req) as response:
            status = response.status
            body = response.read().decode('utf-8')
            try:
                json_body = json.loads(body)
            except:
                json_body = body
            return status, json_body
    except urllib.error.HTTPError as e:
        print(f"HTTP Error: {e.code} - {e.read().decode('utf-8')}")
        raise
    except Exception as e:
        print(f"Error: {e}")
        raise

def test_health():
    print_step("Testing Health")
    status, body = make_request("GET", "/health")
    print(f"Status: {status}")
    print(f"Response: {body}")
    assert status == 200

def test_hydration():
    print_step("Testing Hydration (Pre-warm)")
    status, body = make_request(
        "POST", 
        "/v1/prewarm", 
        params={"user_id": USER_ID},
        headers={"X-App-ID": APP_ID}
    )
    print(f"Hydration Status: {status}")
    print(body)
    time.sleep(2)

def test_enrichment_chatbot():
    print_step("Testing Enrichment (Chatbot Pattern)")
    template = "User prefers [[seat_preference|Any]] seat. Diet: [[dietary_restrictions]]."
    
    status, body = make_request(
        "POST",
        "/v1/enrich",
        data={"user_id": USER_ID, "template": template},
        headers={"X-App-ID": APP_ID}
    )
    print(f"Enrich Status: {status}")
    print(f"Enriched: {body['enriched_prompt']}")
    print(f"Used Vars: {body['used_variables']}")

def test_profile_agent():
    print_step("Testing Profile Retrieval (Agent Pattern)")
    status, body = make_request(
        "GET",
        f"/v1/profile/{USER_ID}",
        headers={"X-App-ID": APP_ID}
    )
    print(f"Profile Status: {status}")
    print(f"Profile: {body}")

def test_memory_update_tool():
    print_step("Testing Memory Update (Tool Call)")
    payload = {
        "user_id": USER_ID,
        "interaction_type": "tool_call",
        "fact": "User loves sushi",
        "category": "food_preference",
        "confidence": 0.95
    }
    status, body = make_request(
        "POST",
        "/v1/memory",
        data=payload,
        headers={"X-App-ID": APP_ID}
    )
    print(f"Update Status: {status}")
    print(body)

def test_memory_update_trace():
    print_step("Testing Memory Update (Trace)")
    payload = {
        "user_id": USER_ID,
        "interaction_type": "trace",
        "execution_trace": [
            {"step": 1, "type": "thought", "content": "Thinking..."},
            {"step": 2, "type": "tool_call", "tool_name": "search", "tool_args": "query"}
        ]
    }
    status, body = make_request(
        "POST",
        "/v1/memory",
        data=payload,
        headers={"X-App-ID": APP_ID}
    )
    print(f"Trace Status: {status}")
    print(body)

if __name__ == "__main__":
    try:
        test_health()
        test_hydration()
        time.sleep(1)
        test_enrichment_chatbot()
        test_profile_agent()
        test_memory_update_tool()
        test_memory_update_trace()
        print("\n✅ Verification Complete!")
    except Exception as e:
        print(f"\n❌ Verification Failed: {e}")
