#!/usr/bin/env python3
"""
Test Script: Comprehensive Travel Agent Scenarios
Tests passive learning extraction across different user personas and implicit fact types.
Uses curl to submit chat logs to avoid python dependency issues.
"""

import subprocess
import json
import time
import sys

BASE_URL = "http://localhost:8000"

SCENARIOS = [
    {
        "name": "Business Traveler (Sarah)",
        "user_id": "travel_sarah_v2_final",
        "app_id": "TravelAgentAI",
        "messages": [
            {"role": "user", "content": "I need to book a trip to New York for a conference next month."},
            {"role": "assistant", "content": "I found several flight options. United has a red-eye departing at 11 PM."},
            {"role": "user", "content": "No red-eyes please, I can never sleep on planes. I need to be functional for my presentation."},
            {"role": "assistant", "content": "Got it! Here's an 8 AM departure with a 45-minute layover in Chicago."},
            {"role": "user", "content": "That layover is too short - last time I almost missed my connection. Can you find something with at least 90 minutes?"},
            {"role": "assistant", "content": "Found one with a 2-hour layover. Would you like a window or aisle seat?"},
            {"role": "user", "content": "Aisle always - I'm tall and need the legroom to stretch. Also, I get a bit claustrophobic by the window."},
            {"role": "assistant", "content": "Perfect. For hotels, I found The Lively Times Square - it's in the heart of the action!"},
            {"role": "user", "content": "That place is way too busy and noisy. I need somewhere quiet to prepare for my presentation. Maybe something in Midtown but off the main streets?"},
            {"role": "assistant", "content": "How about the Park Hyatt? It's $450/night but very quiet and has great WiFi."},
            {"role": "user", "content": "That's too expensive for a work trip - my company caps at $250. I always stay at Marriott properties anyway for the Bonvoy points. Can you find a Courtyard or Residence Inn?"},
            {"role": "assistant", "content": "Found Residence Inn Midtown - $220/night with free breakfast and a kitchenette."},
            {"role": "user", "content": "Perfect! The kitchenette is great since I'm vegetarian and sometimes the conference food options are limited. Does it have a gym? I like to run in the mornings before meetings."},
            {"role": "assistant", "content": "Yes, 24-hour fitness center! For dinner, there's an excellent seafood place nearby."},
            {"role": "user", "content": "I don't eat seafood - shellfish allergy. Maybe find something with good vegetarian options instead?"}
        ]
    },
    {
        "name": "Family Vacation (Mike)",
        "user_id": "family_mike_v2_final",
        "app_id": "TravelAgentAI",
        "messages": [
            {"role": "user", "content": "Planning a Disney World trip for next summer with my wife and our two kids, ages 4 and 7."},
            {"role": "assistant", "content": "Great! I can help plan that. Should I look at value resorts to save money?"},
            {"role": "user", "content": "Actually, let's do a deluxe resort this time. We saved up for years and want to make it special. But we need a room with two beds - the kids still won't sleep alone in a new place."},
            {"role": "assistant", "content": "The Contemporary Resort has availability. Would you like early morning dining reservations?"},
            {"role": "user", "content": "No super early mornings - with young kids we're never out of the room before 9 AM. They need their sleep or we all pay the price later."},
            {"role": "assistant", "content": "I'll schedule first activities for 10 AM. Do you want thrill ride reservations?"},
            {"role": "user", "content": "Only for rides my 7-year-old can handle. My 4-year-old gets scared easily and my wife doesn't do roller coasters at all. Space Mountain is her absolute limit."},
            {"role": "assistant", "content": "Noted! Should I add park hopper tickets for maximum flexibility?"},
            {"role": "user", "content": "No, we tried that last vacation and it was too exhausting moving between parks with the kids. One park per day works better for us."}
        ]
    },
    {
        "name": "Solo Adventure (Alex)",
        "user_id": "solo_alex_v2_final",
        "app_id": "TravelAgentAI",
        "messages": [
            {"role": "user", "content": "I'm looking for a 2-week trip to Patagonia in December. I'm an experienced hiker so I want challenging trails."},
            {"role": "assistant", "content": "Patagonia is amazing! Do you want to book guided tours or explore on your own?"},
            {"role": "user", "content": "I prefer solo hiking, no guides. I have my Wilderness First Responder certification so I'm comfortable being remote."},
            {"role": "assistant", "content": "Understood. For accommodation, there are some luxury eco-lodges or we can look at camping."},
            {"role": "user", "content": "Definitely camping or hostels. I hate luxury hotels, they feel too sterile. I want to meet other travelers."},
            {"role": "assistant", "content": "Okay. Are you bringing your own gear?"},
            {"role": "user", "content": "Yes, I have my own tent and sleeping bag. But I need to rent a stove locally because I can't fly with fuel."},
            {"role": "assistant", "content": "I'll note that. Do you speak Spanish?"},
            {"role": "user", "content": "I'm fluent in Spanish, so I can navigate local transport easily. No need for private transfers."}
        ]
    }
]

def submit_chat(scenario):
    print(f"\n🚀 Submitting: {scenario['name']} ({len(scenario['messages'])} messages)")
    
    payload = {
        "user_id": scenario["user_id"],
        "interaction_type": "chat_log",
        "messages": scenario["messages"]
    }
    
    cmd = [
        "curl", "-s", "-X", "POST", f"{BASE_URL}/v1/memory",
        "-H", f"X-App-ID: {scenario['app_id']}",
        "-H", "Content-Type: application/json",
        "-d", json.dumps(payload)
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode == 0 and "queued" in result.stdout:
        print(f"   ✅ Success: {result.stdout}")
        return True
    else:
        print(f"   ❌ Failed: {result.stderr or result.stdout}")
        return False

def main():
    print("="*60)
    print("🌍 TRAVEL AGENT SCENARIO TESTER")
    print("="*60)
    
    for scenario in SCENARIOS:
        submit_chat(scenario)
        # Small delay between submissions
        time.sleep(2)
        
    print("\n" + "-"*60)
    print("⏳ Waiting for processing (approx 3-5 mins)...")
    print("   Run this command to check results:")
    print("   docker exec context-quilt-db psql -U postgres -d context_quilt -c \"SELECT user_id, category, fact FROM facts WHERE source='learned' AND user_id LIKE '%final' ORDER BY created_at DESC;\"")
    print("-"*60)

if __name__ == "__main__":
    main()
