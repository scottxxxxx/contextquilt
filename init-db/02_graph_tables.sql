-- Context Quilt Graph Memory Layer
-- Entities and relationships for episodic memory

-- Entities: named things CQ has learned about
CREATE TABLE IF NOT EXISTS entities (
    entity_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    entity_type TEXT NOT NULL,  -- person, project, company, feature, artifact, deadline, metric
    description TEXT,
    metadata JSONB DEFAULT '{}'::jsonb,
    first_seen_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_seen_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    mention_count INTEGER DEFAULT 1,
    UNIQUE(user_id, name, entity_type)
);

CREATE INDEX IF NOT EXISTS idx_entities_user ON entities(user_id);
CREATE INDEX IF NOT EXISTS idx_entities_user_name ON entities(user_id, name);
CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type);

-- Relationships: connections between entities
CREATE TABLE IF NOT EXISTS relationships (
    relationship_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL,
    from_entity_id UUID NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    to_entity_id UUID NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    relationship_type TEXT NOT NULL,  -- works_on, committed_to, requires, has_deadline, etc.
    context TEXT,                     -- brief explanation of the relationship
    metadata JSONB DEFAULT '{}'::jsonb,
    first_seen_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_seen_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    mention_count INTEGER DEFAULT 1,
    UNIQUE(user_id, from_entity_id, to_entity_id, relationship_type)
);

CREATE INDEX IF NOT EXISTS idx_relationships_user ON relationships(user_id);
CREATE INDEX IF NOT EXISTS idx_relationships_from ON relationships(from_entity_id);
CREATE INDEX IF NOT EXISTS idx_relationships_to ON relationships(to_entity_id);
