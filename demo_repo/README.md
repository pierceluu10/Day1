# acme-api

Acme's users service: a small FastAPI app that stores users in a SQL database.

> This repository is a demo built to mirror common onboarding problems.

## Getting started

```bash
git clone git@gitea:acme/acme-api.git
cd acme-api
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pytest
```

## Running the service

```bash
uvicorn acme_api.app:app --reload
```

## API

| Method | Path | |
|---|---|---|
| GET | `/health` | liveness |
| GET | `/users` | list users |
| POST | `/users` | create a user (`email`, `name`) |
| GET | `/users/{id}` | one user |
| PATCH | `/users/{id}` | rename a user |
| DELETE | `/users/{id}` | delete a user |
