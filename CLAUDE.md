# CLAUDE.md — Webull_AI

## Project Overview

Webull_AI is an AI-powered project for Webull trading/investment workflows. This repository is in early development.

## Repository Structure

```
Webull_AI/
├── CLAUDE.md          # AI assistant guidance (this file)
└── (project files TBD)
```

## Development Setup

### Prerequisites

- Git
- Python 3.10+ (expected)
- A valid Webull account/API credentials (if applicable)

### Getting Started

```bash
git clone <repo-url>
cd Webull_AI
# Install dependencies (TBD — update once a package manager / requirements file is added)
```

## Development Workflows

### Branching

- Feature branches should follow the pattern: `feature/<description>`
- Bug fix branches: `fix/<description>`
- AI-generated branches use the `claude/` prefix

### Commits

- Use clear, descriptive commit messages
- Keep commits focused on a single logical change

### Testing

- (TBD) Add testing framework and commands here once tests are set up
- All changes should include tests where applicable

### Linting / Code Style

- (TBD) Add linter configuration once established
- Follow PEP 8 for Python code
- Use type hints where practical

## Key Conventions

- **No secrets in code**: Never commit API keys, tokens, or credentials. Use environment variables or a `.env` file (which must be in `.gitignore`).
- **Keep dependencies explicit**: Use `requirements.txt`, `pyproject.toml`, or equivalent to pin dependencies.
- **Document as you go**: Update this file and relevant README docs when adding new modules or changing project structure.

## Environment Variables

Expected environment variables (update as the project evolves):

| Variable | Description |
|----------|-------------|
| `WEBULL_API_KEY` | Webull API key (if applicable) |
| `WEBULL_API_SECRET` | Webull API secret (if applicable) |

## Notes for AI Assistants

- Read this file first before making changes
- Always check for existing patterns in the codebase before introducing new ones
- Do not create unnecessary files or over-engineer solutions
- Keep changes minimal and focused on the task at hand
- Run tests (once available) before committing
