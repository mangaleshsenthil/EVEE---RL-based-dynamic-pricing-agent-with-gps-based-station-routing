# EVEE - RL based dynamic pricing agent with gps based station routing

> Reinforcement Learning-powered EV charging price optimisation with GPS-based station routing, vehicle validation, and PostgreSQL persistence.

![CI/CD](https://github.com/Aparajitha12/evee-rl-dynamic-pricing-gps-station-routing/actions/workflows/ci-cd.yml/badge.svg)
![Python](https://img.shields.io/badge/python-3.11-blue)
![Streamlit](https://img.shields.io/badge/streamlit-1.35+-red)
![Docker](https://img.shields.io/badge/docker-ready-blue)
![PostgreSQL](https://img.shields.io/badge/postgresql-15-336791)

---

## Features

- **RL Pricing** - PPO / SAC / TD3 policies dynamically adjust price per kWh based on demand, time, weather, and grid conditions
- **GPS Station Routing** - Auto-detects browser location, fetches real EV stations via Open Charge Map API, ranks by driver skill profile
- **Vehicle Validation** - 60+ vehicle database; petrol/diesel/non-plug hybrids blocked at registration and login
- **Indian Plate Format** - Enforces `TN01AB1234` standard with real-time validation and duplicate detection
- **PostgreSQL** - Full persistence with UNIQUE plate constraint; graceful in-memory fallback
- **RBAC** - Owner and Driver roles with separate dashboards; PII not exposed to owners
- **Docker** - Multi-stage image, docker-compose with PostgreSQL service, GitHub Actions CI/CD

---

## Quick Start

### Option A - Docker Compose (recommended)

```bash
# 1. Clone the repo
git clone https://github.com/Aparajitha12/evee-rl-dynamic-pricing-gps-station-routing.git
cd evee-rl-dynamic-pricing-gps-station-routing

# 2. Set environment variables
cp .env.example .env
# Edit .env with your DB password and OCM API key

# 3. Configure secrets
cp secrets_docker.toml.example secrets_docker.toml
# Edit secrets_docker.toml with same credentials

# 4. Start everything
docker compose up --build

# App available at http://localhost:8501
```

### Option B - Local Python

```bash
# 1. Create virtual environment
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure secrets
mkdir -p .streamlit
cp secrets.toml.example .streamlit/secrets.toml
# Edit with your PostgreSQL credentials
# Get a free API key at: https://openchargemap.org/site/develop/api
# Add to secrets.toml as OCM_API_KEY

# 4. Run
streamlit run ev_app.py
```
---
## Running Tests

```bash
pip install pytest pytest-cov
pytest tests/ -v --cov=ev_app
```

---

## CI/CD Pipeline

On every push to `main`:

1. **Lint** - flake8 syntax check + bandit security scan
2. **Test** - pytest with PostgreSQL test service
3. **Build** - Docker image built and pushed to GitHub Container Registry (`ghcr.io`)


Pull image:
```bash
docker pull ghcr.io/aparajitha12/evee-rl-dynamic-pricing-gps-station-routing/evee-app:latest
```

---

## PostgreSQL Setup

```bash
# Run schema manually if not using docker-compose
psql -U postgres -f setup_postgres.sql
```

The schema auto-creates on first app start if tables don't exist (`_init_pg_schema()`).

---
