-- BYOK: Add encrypted LLM API key column to applications
-- Keys are encrypted at rest using Fernet (AES-128-CBC).
-- The encryption key is CQ_KEY_ENCRYPTION_KEY env var.

ALTER TABLE applications ADD COLUMN IF NOT EXISTS llm_api_key_encrypted TEXT;
ALTER TABLE applications ADD COLUMN IF NOT EXISTS llm_base_url TEXT;
ALTER TABLE applications ADD COLUMN IF NOT EXISTS llm_model TEXT;
