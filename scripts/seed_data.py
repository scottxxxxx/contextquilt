import json
import urllib.request
import urllib.error
import time
import random
from datetime import datetime, timedelta

API_URL = "http://localhost:8000/v1/memory"
APP_ID = "synthetic_seeder"

USERS = [
    {
        "user_id": "alice_wonder",
        "app_id": "Figma",
        "role": "Designer",
        "facts": [
            ("Prefers dark mode interfaces", "preference"),
            ("Uses Figma for prototyping", "tooling"),
            ("Working on the 'Nebula' design system", "project"),
            ("Dislikes low contrast text", "preference"),
            ("Expert in accessibility standards", "skill")
        ]
    },
    {
        "user_id": "bob_builder",
        "app_id": "Terraform",
        "role": "DevOps Engineer",
        "facts": [
            ("Manages Kubernetes clusters", "responsibility"),
            ("Prefers Terraform over CloudFormation", "preference"),
            ("On-call rotation is every 3 weeks", "schedule"),
            ("Writing a script to automate backups", "current_task"),
            ("Certified AWS Solutions Architect", "certification")
        ]
    },
    {
        "user_id": "charlie_chef",
        "app_id": "VSCode",
        "role": "Backend Developer",
        "facts": [
            ("Specializes in Python and FastAPI", "skill"),
            ("Refactoring the payment gateway", "current_task"),
            ("Advocates for test-driven development", "methodology"),
            ("Uses VS Code with Vim keybindings", "tooling"),
            ("Learning Rust in spare time", "interest")
        ]
    },
    {
        "user_id": "diana_doctor",
        "app_id": "Jupyter",
        "role": "Data Scientist",
        "facts": [
            ("Building a recommendation engine", "project"),
            ("Uses Jupyter Notebooks for exploration", "tooling"),
            ("Needs GPU instances for training", "resource"),
            ("Prefer PyTorch over TensorFlow", "preference"),
            ("Analyzing user churn data", "current_task")
        ]
    },
    {
        "user_id": "evan_engineer",
        "app_id": "React",
        "role": "Frontend Developer",
        "facts": [
            ("Migrating legacy app to React 18", "project"),
            ("Focusing on performance optimization", "goal"),
            ("Uses Tailwind CSS for styling", "tooling"),
            ("Debugging a memory leak in the dashboard", "current_task"),
            ("Fan of functional programming patterns", "preference")
        ]
    }
]

FACTS = [
    "{user} is working on a new feature",
    "{user} prefers Python over Java",
    "{user} is debugging a critical issue",
    "{user} updated the documentation",
    "{user} deployed to production",
    "{user} is reviewing a pull request",
    "{user} attended the daily standup",
    "{user} is optimizing database queries",
    "{user} wrote a new test case",
    "{user} is refactoring legacy code"
]

CATEGORIES = ["project", "preference", "tooling", "current_task", "goal", "skill", "interest"]

APPS = ["Chatbot", "AgentApp", "Figma", "VSCode", "Jira", "Slack", "Notion"]

def seed_data():
    print(f"Starting data seed for {len(USERS)} users...")
    
    success_count = 0
    fail_count = 0

    # 2. Generate Historical Data (Past 30 Days)
    print("Generating historical data for the past 30 days...")
    for day_offset in range(30, -1, -1): # 30 days ago to today
        current_date = datetime.utcnow() - timedelta(days=day_offset)
        
        # Varying rate: Random number of facts per day (0 to 20)
        # Simulate some "busy" days and some "quiet" days
        daily_volume = random.randint(0, 20)
        
        # Simulate a "spike" on weekends or specific days
        if day_offset % 7 == 0: # Weekly spike
            daily_volume += random.randint(10, 30)
            
        print(f"  - {current_date.date()}: Generating {daily_volume} facts...")
        
        for _ in range(daily_volume):
            user_choice = random.choice(USERS)
            user_id = user_choice["user_id"] # Extract user_id from the chosen user dict
            app_id = random.choice(APPS)
            fact_template = random.choice(FACTS)
            fact = fact_template.format(user=user_id) # Use user_id here
            
            payload = {
                "user_id": user_id, # Use user_id here
                "interaction_type": "tool_call",
                "fact": fact,
                "category": random.choice(CATEGORIES),
                "app_id": app_id,
                "memory_type": random.choice(["identity", "preference", "trait"]),
                "timestamp": current_date.isoformat()
            }
            
            try:
                req = urllib.request.Request(
                    API_URL, # Corrected: API_URL already includes /v1/memory
                    data=json.dumps(payload).encode('utf-8'),
                    headers={
                        "Content-Type": "application/json",
                        "X-App-ID": app_id
                    },
                    method="POST"
                )
                with urllib.request.urlopen(req) as response:
                    if response.status == 200:
                        success_count += 1
                    else:
                        fail_count += 1
            except Exception as e:
                print(f"Failed to seed fact: {e}")
                fail_count += 1
            
            # Small delay to simulate realistic arrival and not hammer the local DB too hard
            time.sleep(0.1)

    print(f"\nSeeding Complete!")
    print(f"Success: {success_count}")
    print(f"Failed: {fail_count}")

if __name__ == "__main__":
    seed_data()
