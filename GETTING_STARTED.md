# Context Quilt - Getting Started Guide

## 📦 What You Have

I've created a complete starter kit for **Context Quilt** - your LLM gateway with unified memory management. Here's what's included:

### Core Documentation
1. **llm-gateway-project.md** - Complete technical specification with architecture, features, and roadmap
2. **README.md** - Project overview and quick start guide

### Configuration Files
3. **docker-compose.yml** - Full stack setup (API, PostgreSQL, Redis, optional GUI tools)
4. **.env.example** - All environment variables with explanations
5. **Dockerfile** - Multi-stage optimized container build
6. **requirements.txt** - All Python dependencies

### Code
7. **main.py** - Working FastAPI MVP with:
   - OpenAI integration
   - Context enrichment
   - Memory management (in-memory for MVP)
   - API authentication
   - Health checks
   - Full API documentation

## 🚀 Quick Start (5 Minutes)

### Step 1: Download Files
Save all the files to a new directory:
```bash
mkdir context-quilt
cd context-quilt
```

### Step 2: Set Up Environment
```bash
# Copy environment template
cp .env.example .env

# Edit .env and add your OpenAI API key
nano .env  # or use your preferred editor
```

**Required**: Add your OpenAI API key to `.env`:
```bash
OPENAI_API_KEY=sk-your-actual-key-here
```

### Step 3: Run with Docker Compose
```bash
# Start all services (API, PostgreSQL, Redis)
docker-compose up -d

# Check logs
docker-compose logs -f context-quilt
```

The API will be running at http://localhost:8000

### Step 4: Test It
```bash
# Health check
curl http://localhost:8000/health

# Make your first enriched chat request
curl -X POST http://localhost:8000/v1/chat \
  -H "Authorization: Bearer test_key_12345" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user_001",
    "application": "support_bot",
    "messages": [
      {"role": "user", "content": "Hello, I need help with my account"}
    ]
  }'
```

### Step 5: Explore API Docs
Visit http://localhost:8000/docs to see interactive API documentation.

## 📁 File Structure

```
context-quilt/
├── README.md                    # Project overview
├── llm-gateway-project.md       # Complete technical spec
├── docker-compose.yml           # Docker setup
├── Dockerfile                   # Container definition
├── .env.example                 # Environment template
├── .env                         # Your actual config (create this)
├── requirements.txt             # Python dependencies
└── main.py                      # FastAPI application
```

## 🎯 What Works Now (MVP)

The current MVP includes:
- ✅ **Chat completion** with OpenAI GPT-4
- ✅ **Context enrichment** based on application type
- ✅ **In-memory user profiles** and conversation history
- ✅ **API authentication** with Bearer tokens
- ✅ **Health checks** and monitoring endpoints
- ✅ **Interactive API docs** at /docs

## 🔄 Development Workflow

### Option 1: Docker (Recommended for Testing)
```bash
# Start services
docker-compose up -d

# View logs
docker-compose logs -f

# Stop services
docker-compose down
```

### Option 2: Local Development (Better for Coding)
```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run directly
python main.py

# Or with uvicorn (auto-reload on changes)
uvicorn main:app --reload
```

## 📝 Example API Calls

### 1. Simple Chat
```bash
curl -X POST http://localhost:8000/v1/chat \
  -H "Authorization: Bearer test_key" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user_123",
    "application": "support_bot",
    "messages": [
      {"role": "user", "content": "How do I reset my password?"}
    ]
  }'
```

### 2. Chat with Custom Model
```bash
curl -X POST http://localhost:8000/v1/chat \
  -H "Authorization: Bearer test_key" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user_123",
    "application": "sales_assistant",
    "model": "gpt-3.5-turbo",
    "messages": [
      {"role": "user", "content": "Tell me about your enterprise plans"}
    ]
  }'
```

### 3. Get User Memory
```bash
curl -X GET http://localhost:8000/v1/memory/user_123 \
  -H "Authorization: Bearer test_key"
```

### 4. Update User Preferences
```bash
curl -X PUT http://localhost:8000/v1/memory/user_123 \
  -H "Authorization: Bearer test_key" \
  -H "Content-Type: application/json" \
  -d '{
    "preferences": {
      "communication_style": "technical",
      "verbosity": "concise"
    }
  }'
```

## 🎨 How Context Enrichment Works

When a request comes in, Context Quilt automatically:

1. **Identifies the application type** (support_bot, sales_assistant, etc.)
2. **Retrieves user memory** (profile, preferences, past interactions)
3. **Builds enriched context** with:
   - Role-specific instructions
   - User profile information
   - Recent conversation history
4. **Injects context into the prompt** before sending to LLM
5. **Saves the interaction** for future context

Example enrichment for a support bot:
```
[ROLE CONTEXT]
You are a helpful customer support assistant. Be empathetic and solution-oriented.

[USER PROFILE]
{"tier": "premium", "name": "John Doe"}

[RECENT HISTORY]
- 2024-11-10: Password reset issue - resolved
- 2024-11-09: Billing inquiry - Enterprise plan

[USER MESSAGE]
How do I reset my password?
```

## 🛠️ Next Steps for Production

### Phase 1: Replace In-Memory Storage
- [ ] Implement PostgreSQL for user memory
- [ ] Implement Redis for caching
- [ ] Add database migrations with Alembic

### Phase 2: Add Advanced Features
- [ ] A/B testing framework
- [ ] Multi-provider support (Anthropic, etc.)
- [ ] Smart model routing
- [ ] Cost tracking and budgets

### Phase 3: Security & Compliance
- [ ] Proper API key management
- [ ] Rate limiting per key
- [ ] PII detection
- [ ] Audit logging

### Phase 4: Observability
- [ ] Prometheus metrics
- [ ] Grafana dashboards
- [ ] Structured logging
- [ ] Error tracking (Sentry)

## 📚 Useful Resources

### API Documentation
- Interactive docs: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc

### Database Tools (when using docker-compose)
- pgAdmin: http://localhost:5050 (default: admin@contextquilt.local / admin)
- Redis Commander: http://localhost:8081

To enable these tools:
```bash
docker-compose --profile debug up -d
```

## 🐛 Troubleshooting

### Issue: "Module not found" errors
**Solution**: Make sure you've installed dependencies
```bash
pip install -r requirements.txt
```

### Issue: Docker won't start
**Solution**: Check if ports are already in use
```bash
# Check what's using port 8000
lsof -i :8000  # macOS/Linux
netstat -ano | findstr :8000  # Windows

# Change port in docker-compose.yml if needed
```

### Issue: OpenAI API errors
**Solution**: Verify your API key in .env
```bash
# Test your key directly
curl https://api.openai.com/v1/models \
  -H "Authorization: Bearer $OPENAI_API_KEY"
```

### Issue: Database connection errors
**Solution**: Ensure PostgreSQL is running
```bash
docker-compose ps
docker-compose logs postgres
```

## 💡 Tips for Development

1. **Use the interactive docs** at /docs - you can test all endpoints directly in your browser
2. **Check logs frequently** - FastAPI has excellent error messages
3. **Start simple** - Get basic chat working before adding complex features
4. **Use environment variables** - Never hardcode API keys
5. **Test incrementally** - Add one feature at a time

## 🤝 Contributing to Context Quilt

This is your project foundation! Here's how to evolve it:

1. **Add features incrementally** - Start with the MVP, add features as needed
2. **Keep documentation updated** - Update README as you add features
3. **Write tests** - Add pytest tests as you build
4. **Profile performance** - Use FastAPI's built-in profiling
5. **Share with team** - Docker makes it easy for others to run

## 📧 Getting Help

- Check the technical spec: `llm-gateway-project.md`
- Review API docs: http://localhost:8000/docs
- FastAPI documentation: https://fastapi.tiangolo.com/
- OpenAI API reference: https://platform.openai.com/docs/

---

**You're all set!** 🎉

You now have a working LLM gateway with context enrichment. Start with the MVP, test it out, and gradually add the advanced features from the roadmap.

Happy coding! 🧵
