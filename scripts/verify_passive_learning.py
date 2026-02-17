import json
import urllib.request
import urllib.error
import time

API_URL = "http://localhost:8000/v1/memory"
APP_ID = "Chatbot"
USER_ID = "passive_learner_test"

def verify_passive_learning():
    print(f"Testing Passive Learning for user: {USER_ID}")
    
    # Simulate a chat conversation
    messages = [
        {"role": "user", "content": "I've been using Python for data analysis for years, but I'm new to Rust."},
        {"role": "assistant", "content": "Rust is great for performance! What kind of data analysis do you do?"},
        {"role": "user", "content": "Mostly financial modeling. I prefer using Pandas over Polars for now."},
        {"role": "assistant", "content": "Pandas is a solid choice."}
    ]
    
    payload = {
        "user_id": USER_ID,
        "interaction_type": "chat_log",
        "messages": messages,
        "app_id": APP_ID
    }
    
    print("Sending chat log to API...")
    try:
        req = urllib.request.Request(
            API_URL,
            data=json.dumps(payload).encode('utf-8'),
            headers={
                "Content-Type": "application/json",
                "X-App-ID": APP_ID
            },
            method="POST"
        )
        with urllib.request.urlopen(req) as response:
            if response.status == 200:
                print("  [OK] Chat log accepted by API.")
                print("  Worker should now be processing it with Ollama...")
            else:
                print(f"  [ERR] API returned status {response.status}")
    except Exception as e:
        print(f"  [FAIL] Request failed: {e}")

if __name__ == "__main__":
    verify_passive_learning()
