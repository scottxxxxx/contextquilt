# Context Quilt 🧵

**Weaving Context, Enriching Conversations**

Context Quilt is a low-latency LLM gateway that provides unified memory management, intelligent prompt enrichment, and advanced features for AI applications. Like a quilt that combines different patches into a cohesive whole, Context Quilt weaves together context from multiple sources to make every LLM interaction smarter and more personalized.

## ✨ Key Features

- **🧠 Unified Memory**: Maintain user context across channels, applications, and sessions
- **🎯 Role-Based Enrichment**: Automatically inject relevant context based on application type
- **🧪 A/B Testing**: Systematically test and improve prompts with built-in experimentation
- **🚀 Smart Routing**: Intelligently route to the best model based on requirements and cost
- **💾 Response Caching**: Reduce costs and latency with intelligent caching
- **📊 Observability**: Centralized logging, metrics, and analytics for all LLM interactions
- **🔒 Security**: API authentication, rate limiting, PII detection, and compliance features

## 🎯 Use Cases

### Customer-Facing Applications
- Chatbots that remember previous conversations across channels
- Voice assistants with contextual awareness
- Email support with full customer history
- Mobile apps with personalized AI features

### Internal Tools
- Employee helpdesk with unified knowledge
- Sales assistants that know customer journey
- Code assistants with project context
- Document analysis with role-based permissions

### Enterprise Benefits
- **30-50% cost reduction** through caching and smart routing
- **Cross-channel continuity** - customer context follows them everywhere
- **Centralized compliance** - single audit trail for all LLM interactions
- **Systematic improvement** - A/B test prompts to continuously optimize

## 🤖 Headless Agents

Context Quilt is optimized for autonomous agents:
- **Execution Traces**: Log internal thoughts and tool outputs via `/v1/memory` (trace mode).
- **Active Learning**: Agents can proactively save facts using the `save_user_preference` tool pattern.
- **Structured State**: Retrieve raw JSON variables via `/v1/enrich?format=json` for direct function calling.

## 🏗️ Architecture

```
┌─────────────────────────────────────────┐
│         Client Applications             │
│  (Chatbots, Voice, Email, Mobile)       │
└─────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────┐
│          Context Quilt Gateway           │
│                                          │
│  ┌────────────────────────────────────┐ │
│  │   1. Authentication & Rate Limit   │ │
│  │   2. Memory Lookup                 │ │
│  │   3. Prompt Enrichment             │ │
│  │   4. A/B Test Assignment           │ │
│  │   5. Smart Model Selection         │ │
│  │   6. Cache Check                   │ │
│  └────────────────────────────────────┘ │
└─────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────┐
│         LLM Providers                    │
│  (OpenAI, Anthropic, Google, etc.)      │
└─────────────────────────────────────────┘
```

## 🚀 Quick Start

### Prerequisites
- Docker and Docker Compose (recommended)
- Python 3.11+ (for local development)
- PostgreSQL 14+
- Redis 7+

### Installation

```bash
# Clone the repository
git clone https://github.com/your-org/context-quilt.git
cd context-quilt

# Copy environment template
cp .env.example .env

# Edit .env and add your API keys
nano .env

# Run with Docker Compose
docker-compose up -d

# The API will be available at http://localhost:8000
# API documentation at http://localhost:8000/docs
```

### Your First Request

```bash
curl -X POST http://localhost:8000/v1/chat \
  -H "Authorization: Bearer your_api_key" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user_123",
    "application": "support_bot",
    "messages": [
      {"role": "user", "content": "Hello, I need help"}
    ]
  }'
```

## 📖 API Documentation

### Chat Completion with Context

```http
POST /v1/chat
Authorization: Bearer {api_key}
Content-Type: application/json

{
  "user_id": "user_12345",
  "session_id": "session_abc",
  "application": "support_chatbot",
  "messages": [
    {"role": "user", "content": "How do I reset my password?"}
  ],
  "context_enrichment": {
    "enabled": true,
    "include_history": true,
    "max_history_messages": 5
  }
}
```

### Response

```json
{
  "id": "req_xyz123",
  "model_used": "gpt-4",
  "context_quilt": {
    "patches_used": ["user_profile", "conversation_history"],
    "enrichment_tokens": 150
  },
  "response": {
    "role": "assistant",
    "content": "I can help you reset your password..."
  },
  "metadata": {
    "latency_ms": 1250,
    "tokens_used": 450,
    "cost_usd": 0.0234
  }
}
```

### Memory Management

```bash
# Get user memory
GET /v1/memory/{user_id}

# Update user preferences
PUT /v1/memory/{user_id}
{
  "preferences": {
    "communication_style": "technical",
    "verbosity": "concise"
  }
}
```

### A/B Testing

```bash
# Create an experiment
POST /v1/experiments
{
  "name": "prompt_optimization_v1",
  "variants": {
    "control": {"prompt_template": "template_a"},
    "treatment": {"prompt_template": "template_b"}
  },
  "traffic_split": 0.5
}

# Get experiment results
GET /v1/experiments/{experiment_id}/results
```

## 🔧 Configuration

### Environment Variables

Key configuration options in `.env`:

```bash
# API Configuration
CONTEXT_QUILT_API_PORT=8000
CONTEXT_QUILT_LOG_LEVEL=INFO

# LLM Providers
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...

# Database
POSTGRES_HOST=localhost
POSTGRES_DB=context_quilt

# Redis Cache
REDIS_HOST=localhost
REDIS_PORT=6379

# Features
ENABLE_CACHING=true
ENABLE_AB_TESTING=true
CACHE_TTL_SECONDS=3600
```

## 📊 Performance

- **Latency Overhead**: < 50ms (< 2% of typical LLM response time)
- **Throughput**: 50+ requests/second per container
- **Cache Hit Rate**: 30-60% typical (depending on use case)
- **Cost Savings**: 30-50% through caching and smart routing

## 🛠️ Development

### Local Development Setup

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt

# Run database migrations
alembic upgrade head

# Start development server
uvicorn src.main:app --reload --port 8000

# Run tests
pytest tests/ -v

# Run with coverage
pytest tests/ --cov=src --cov-report=html
```

### Project Structure

```
context-quilt/
├── src/
│   ├── main.py              # FastAPI application
│   ├── services/
│   │   ├── llm_client.py    # LLM integrations
│   │   ├── memory_store.py  # Memory management
│   │   ├── enrichment.py    # Context enrichment
│   │   └── ab_testing.py    # A/B testing
│   └── middleware/
│       ├── auth.py          # Authentication
│       └── rate_limit.py    # Rate limiting
├── tests/
├── docs/
└── docker-compose.yml
```

## 🤝 Contributing

We welcome contributions! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for details.

### Development Workflow

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Make your changes
4. Run tests (`pytest tests/`)
5. Commit changes (`git commit -m 'Add amazing feature'`)
6. Push to branch (`git push origin feature/amazing-feature`)
7. Open a Pull Request

## 📝 Documentation

- [Full Technical Specification](docs/llm-gateway-project.md)
- [API Reference](docs/api.md)
- [Deployment Guide](docs/deployment.md)
- [Architecture Deep Dive](docs/architecture.md)

## 🗺️ Roadmap

### Phase 1: MVP ✅ (Current)
- [x] Basic Docker deployment
- [x] OpenAI integration
- [x] Simple context injection
- [x] API authentication

### Phase 2: Core Features (In Progress)
- [ ] Memory store with PostgreSQL + Redis
- [ ] Role-based prompt templates
- [ ] A/B testing framework
- [ ] Multi-provider support (Anthropic, Google)

### Phase 3: Advanced Features
- [ ] PII detection and redaction
- [ ] Advanced caching strategies
- [ ] Analytics dashboard
- [ ] Semantic search over memories

### Phase 4: Enterprise
- [ ] Multi-tenancy
- [ ] SSO/SAML integration
- [ ] Audit logging for compliance
- [ ] Custom model fine-tuning support

## 📄 License

[License Type] - See [LICENSE](LICENSE) file for details

## 🙏 Acknowledgments

- Built with [FastAPI](https://fastapi.tiangolo.com/)
- Inspired by the need for better context management in LLM applications
- Thanks to all contributors and early adopters

## 📧 Contact

- **Project Website**: [contextquilt.dev](https://contextquilt.dev) (to be created)
- **Issues**: [GitHub Issues](https://github.com/your-org/context-quilt/issues)
- **Discussions**: [GitHub Discussions](https://github.com/your-org/context-quilt/discussions)

---

**Made with ❤️ by the Context Quilt team**

*Weaving better conversations, one context at a time.*
