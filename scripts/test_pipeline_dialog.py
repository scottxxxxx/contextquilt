import requests
import json


def test_pipeline():
    url = "http://localhost:8000/api/dashboard/test-pipeline"
    
    # "Dialogflow-like" conversation converted to our format
    conversation = [
        {"role": "assistant", "content": "Hello! Welcome to CompuWorld. How can I help you find your next computer today?"},
        {"role": "user", "content": "Hi there. I'm looking to buy a new laptop. My old one crashed last week and I need a replacement urgently."},
        {"role": "assistant", "content": "I'm sorry to hear about your old laptop! We can definitely help you find a quick replacement. What will be the primary use for this new machine?"},
        {"role": "user", "content": "I'm a freelance graphic designer, so I'll be using it mostly for Adobe Creative Cloud - Photoshop, Illustrator, and Premiere Pro."},
        {"role": "assistant", "content": "Understood. For graphic design and video editing, you'll need good performance. Do you have a preference between macOS and Windows?"},
        {"role": "user", "content": "I definitely prefer Mac. I've been using Apple products for 10 years and I'm stuck in the ecosystem."},
        {"role": "assistant", "content": "Makes sense! We have the new MacBook Pros in stock. What specific performance requirements do you have regarding screen size or memory?"},
        {"role": "user", "content": "It needs to handle 4k video editing smoothly. I hate lag. Also, I travel a lot so 14-inch is better than 16-inch."},
        {"role": "assistant", "content": "Got it. Powerful but portable. Finally, do you have a specific budget range in mind?"},
        {"role": "user", "content": "I'm trying to keep it under $2500. Also, I'm not very technical with hardware upgrades, so I want something that's fully specced out of the box."}
    ]

    print(f"Sending {len(conversation)} messages to Context Quilt Pipeline...")
    
    try:
        response = requests.post(
            url, 
            json={"messages": conversation}, 
            headers={"Content-Type": "application/json"},
            stream=True
        )
        response.raise_for_status()

        print("\n--- Pipeline Stream ---")
        for line in response.iter_lines():
            if line:
                decoded_line = line.decode('utf-8')
                if decoded_line.startswith('data: '):
                    json_str = decoded_line[6:]
                    try:
                        data = json.loads(json_str)
                        event_type = data.get("type")
                        
                        if event_type == "step_start":
                            print(f"[START] {data.get('step').upper()}...")
                        elif event_type == "step_complete":
                            print(f"   [DONE] {data.get('step').upper()} ({data.get('time_ms')}ms)")
                        elif event_type == "result":
                            print("\n--- Extraction Result ---")
                            print(f"Raw LLM Response Length: {len(data.get('raw_response', ''))}")
                            print("\n[EXTRACTED PATCHES]:")
                            patches = data.get("patches", [])
                            for p in patches:
                                print(f"- [{p.get('patch_type', 'unknown').upper()}] {p.get('value')}")
                        elif event_type == "error":
                            print(f"[ERROR] {data.get('message')}")
                            
                    except json.JSONDecodeError:
                        print(f"Failed to parse: {decoded_line}")
                        
    except Exception as e:
        print(f"Test Failed: {e}")

if __name__ == "__main__":
    test_pipeline()
