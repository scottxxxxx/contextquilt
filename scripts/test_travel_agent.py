#!/usr/bin/env python3
"""
Test Script: Travel Agent Agentic App Simulation
Simulates the kind of chat logs a travel booking AI would generate,
revealing implicit user preferences through conversations.
"""

import requests
import json
from datetime import datetime

BASE_URL = "http://localhost:8000"

# Simulated travel agent conversation that reveals implicit preferences
TRAVEL_AGENT_CHAT = {
    "user_id": "travel_user_sarah",
    "app_id": "TravelAgentAI",
    "messages": [
        {
            "role": "user",
            "content": "I need to book a trip to New York for a conference next month."
        },
        {
            "role": "assistant", 
            "content": "I found several flight options. United has a red-eye departing at 11 PM."
        },
        {
            "role": "user",
            "content": "No red-eyes please, I can never sleep on planes. I need to be functional for my presentation."
        },
        {
            "role": "assistant",
            "content": "Got it! Here's an 8 AM departure with a 45-minute layover in Chicago."
        },
        {
            "role": "user",
            "content": "That layover is too short - last time I almost missed my connection. Can you find something with at least 90 minutes?"
        },
        {
            "role": "assistant",
            "content": "Found one with a 2-hour layover. Would you like a window or aisle seat?"
        },
        {
            "role": "user",
            "content": "Aisle always - I'm tall and need the legroom to stretch. Also, I get a bit claustrophobic by the window."
        },
        {
            "role": "assistant",
            "content": "Perfect. For hotels, I found The Lively Times Square - it's in the heart of the action!"
        },
        {
            "role": "user",
            "content": "That place is way too busy and noisy. I need somewhere quiet to prepare for my presentation. Maybe something in Midtown but off the main streets?"
        },
        {
            "role": "assistant",
            "content": "How about the Park Hyatt? It's $450/night but very quiet and has great WiFi."
        },
        {
            "role": "user",
            "content": "That's too expensive for a work trip - my company caps at $250. I always stay at Marriott properties anyway for the Bonvoy points. Can you find a Courtyard or Residence Inn?"
        },
        {
            "role": "assistant",
            "content": "Found Residence Inn Midtown - $220/night with free breakfast and a kitchenette."
        },
        {
            "role": "user",
            "content": "Perfect! The kitchenette is great since I'm vegetarian and sometimes the conference food options are limited. Does it have a gym? I like to run in the mornings before meetings."
        },
        {
            "role": "assistant",
            "content": "Yes, 24-hour fitness center! For dinner, there's an excellent seafood place nearby."
        },
        {
            "role": "user",
            "content": "I don't eat seafood - shellfish allergy. Maybe find something with good vegetarian options instead?"
        }
    ]
}

# A second scenario: Family vacation planning
FAMILY_VACATION_CHAT = {
    "user_id": "family_user_mike",
    "app_id": "TravelAgentAI",
    "messages": [
        {
            "role": "user",
            "content": "Planning a Disney World trip for next summer with my wife and our two kids, ages 4 and 7."
        },
        {
            "role": "assistant",
            "content": "Great! I can help plan that. Should I look at value resorts to save money?"
        },
        {
            "role": "user",
            "content": "Actually, let's do a deluxe resort this time. We saved up for years and want to make it special. But we need a room with two beds - the kids still won't sleep alone in a new place."
        },
        {
            "role": "assistant",
            "content": "The Contemporary Resort has availability. Would you like early morning dining reservations?"
        },
        {
            "role": "user",
            "content": "No super early mornings - with young kids we're never out of the room before 9 AM. They need their sleep or we all pay the price later."
        },
        {
            "role": "assistant",
            "content": "I'll schedule first activities for 10 AM. Do you want thrill ride reservations?"
        },
        {
            "role": "user",
            "content": "Only for rides my 7-year-old can handle. My 4-year-old gets scared easily and my wife doesn't do roller coasters at all. Space Mountain is her absolute limit."
        },
        {
            "role": "assistant",
            "content": "Noted! Should I add park hopper tickets for maximum flexibility?"
        },
        {
            "role": "user",
            "content": "No, we tried that last vacation and it was too exhausting moving between parks with the kids. One park per day works better for us."
        }
    ]
}

def test_travel_scenario(chat_data, scenario_name):
    """Send a travel agent chat log for passive learning analysis."""
    print(f"\n{'='*60}")
    print(f"Testing: {scenario_name}")
    print(f"User: {chat_data['user_id']}")
    print(f"App: {chat_data['app_id']}")
    print(f"{'='*60}")
    
    # Submit for passive learning via the Cold Path
    response = requests.post(
        f"{BASE_URL}/api/cold-path/submit",
        json={
            "task_type": "chat_log",
            "payload": chat_data
        }
    )
    
    if response.status_code == 200:
        print(f"✅ Chat log submitted successfully!")
        print(f"   Messages: {len(chat_data['messages'])}")
        print(f"   Worker will extract insights using LLM...")
    else:
        print(f"❌ Failed: {response.status_code} - {response.text}")
    
    return response.status_code == 200

def main():
    print("\n" + "="*60)
    print("🧳 TRAVEL AGENT AI - Passive Learning Test")
    print("="*60)
    print("\nThis test simulates chat logs from a travel booking agentic app.")
    print("The LLM should extract implicit preferences like:")
    print("  - No red-eye flights (sleep preference)")
    print("  - Prefers aisle seats (tall, claustrophobic)")
    print("  - Low noise tolerance (rejected busy hotel)")
    print("  - Budget-conscious for work travel ($250 cap)")
    print("  - Marriott Bonvoy loyalty program member")
    print("  - Vegetarian with shellfish allergy")
    print("  - Morning runner / fitness routine")
    
    # Test Scenario 1: Business Traveler
    test_travel_scenario(TRAVEL_AGENT_CHAT, "Business Traveler - Conference Trip")
    
    # Test Scenario 2: Family Vacation
    test_travel_scenario(FAMILY_VACATION_CHAT, "Family Vacation - Disney World")
    
    print("\n" + "-"*60)
    print("⏳ Wait ~2 minutes for Ollama to process, then check:")
    print("   docker logs --tail 50 context-quilt-worker")
    print("   docker exec context-quilt-db psql -U postgres -d context_quilt \\")
    print("     -c \"SELECT user_id, category, fact FROM facts WHERE source='learned' ORDER BY created_at DESC LIMIT 15;\"")
    print("-"*60)

if __name__ == "__main__":
    main()
