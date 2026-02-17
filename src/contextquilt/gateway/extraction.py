import json
import re
from typing import List, Union

def classify_fact(fact_text: str) -> str:
    """
    Hybrid Approach: Python-based fact classification.
    Let the LLM extract facts (fuzzy work), let Python classify (strict work).
    
    Categories (PatchCategory):
    - identity: Who the user is (name, role, team, job title, skills/expertise)
    - preference: The 'What' - Content choices (likes, dislikes, constraints, e.g. "Vegan")
    - trait: The 'How' - Style/Behavior (communication style, personality, e.g. "Concise")
    - experience: Episodic memory (projects, past events, specific interactions)
    """
    fact_lower = fact_text.lower()
    
    # Identity patterns - who they are & what they know (Skills are part of Identity)
    identity_keywords = [
        'name is', 'i am a', 'my role', 'works as', 'job title',
        'team', 'works at', 'employed', 'developer', 'engineer',
        'manager', 'designer', 'analyst', 'architect', 'lead',
        'knows', 'know ', 'experienced', 'expert', 'familiar with', 'proficient',
        'years of', 'using python', 'using rust', 'using java', 'using typescript',
        'certified', 'degree in', 'can write', 'programs in', 'codes in', 'fluent in'
    ]
    if any(kw in fact_lower for kw in identity_keywords):
        return 'identity'
    
    # Preference patterns - what they prefer
    preference_keywords = [
        'prefers', 'likes', 'loves', 'favorite', 'dislikes', 'hates',
        'hate', 'dislike', 'love', 'like to', # Added base forms
        'rather', 'instead of', 'over', 'better than', 'prefer',
        'chooses', 'enjoys', 'appreciates', 'avoids', 'doesn\'t like',
        'vegan', 'aisle seat' # Examples from user
    ]
    if any(kw in fact_lower for kw in preference_keywords):
        return 'preference'
    
    # Experience patterns - what they are doing / have done (Projects are Experiences)
    experience_keywords = [
        'working on', 'current project', 'building', 'developing',
        'implementing', 'debugging', 'refactoring', 'migrating',
        'task', 'sprint', 'roadmap', 'deadline', 'milestone',
        'remember when', 'last week', 'yesterday', 'meeting', 'discussed'
    ]
    if any(kw in fact_lower for kw in experience_keywords):
        return 'experience'
    
    # Trait patterns - how they behave
    trait_keywords = [
        'concise', 'technical', 'verbose', 'detailed', 'simple',
        'tone', 'style', 'responds', 'slow', 'fast'
    ]
    if any(kw in fact_lower for kw in trait_keywords):
        return 'trait'
    
    # Default to trait (behavioral patterns) or experience if it looks like an event
    return 'trait'


def extract_facts_from_response(raw_response: str) -> List[str]:
    """
    Deterministic Guardrail: Extract facts from any LLM output format.
    Implements the "Layer 3" code fix for unreliable LLM outputs.
    
    Handles:
    - JSON array: ["fact1", "fact2"]
    - JSON objects: {"fact": "...", "category": "..."}
    - FINAL JSON block from Detective prompt
    - Plain text sentences as fallback
    """
    facts = []
    
    # Strategy 1: Look for FINAL JSON block (Detective prompt output)
    final_json_match = re.search(r'FINAL JSON:\s*\[([^\]]+)\]', raw_response, re.IGNORECASE | re.DOTALL)
    if final_json_match:
        try:
            json_str = '[' + final_json_match.group(1) + ']'
            parsed = json.loads(json_str)
            for item in parsed:
                if isinstance(item, str):
                    facts.append(item)
                elif isinstance(item, dict) and 'fact' in item:
                    facts.append(item['fact'])
            if facts:
                return facts
        except json.JSONDecodeError:
            pass
    
    # Strategy 2: Look for JSON array anywhere in response
    try:
        start_idx = raw_response.find('[')
        end_idx = raw_response.rfind(']')
        if start_idx != -1 and end_idx != -1:
            json_str = raw_response[start_idx:end_idx+1]
            parsed = json.loads(json_str)
            for item in parsed:
                if isinstance(item, str):
                    facts.append(item)
                elif isinstance(item, dict) and 'fact' in item:
                    facts.append(item['fact'])
            if facts:
                return facts
    except json.JSONDecodeError:
        pass
    
    # Strategy 3: Extract individual JSON objects with regex
    obj_pattern = r'\{[^{}]*"fact"\s*:\s*"([^"]+)"[^{}]*\}'
    matches = re.findall(obj_pattern, raw_response, re.DOTALL)
    if matches:
        return matches
    
    # Strategy 4: Extract quoted strings (potential facts)
    quoted = re.findall(r'"([^"]{10,})"', raw_response)
    if quoted:
        # Filter out JSON-like noise and keep fact-like statements
        facts = [q for q in quoted if not q.startswith('{') and 'fact' not in q.lower()]
        if facts:
            return facts[:5]  # Limit to 5 facts
    
    # Strategy 5: FINAL FALLBACK - Extract sentences from plain text
    # Only use if response looks like prose (not JSON attempts)
    if '{' not in raw_response and '[' not in raw_response:
        # Split on sentence boundaries
        sentences = re.split(r'[.!?]\s+', raw_response.strip())
        for sentence in sentences:
            sentence = sentence.strip()
            # Keep sentences that look like facts about the user
            if len(sentence) > 15 and any(kw in sentence.lower() for kw in 
                ['user', 'prefers', 'likes', 'knows', 'uses', 'works', 'new to', 'i am', 'i use', 'i prefer']):
                facts.append(sentence)
        if facts:
            return facts[:5]
    
    return facts
