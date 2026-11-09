# My Tube 3.0 — Open-Source Technology Registry

Status date: 2026-11-09. This registry records what is **actually used** separately from
what was **evaluated but not adopted**. No proprietary AI API, paid SaaS dependency or
mandatory cloud service is used by the intelligence layer.

## 1. Runtime software actually used

| Component | Repository / docs | License | Version verified here | Runtime | Role | Offline | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Python | https://github.com/python/cpython | PSF-2.0 | 3.15.2 | CPython | Backend | Yes | Requires ≥3.12 in `backend/pyproject.toml`. |
| FastAPI | https://github.com/fastapi/fastapi | MIT | 0.162.2 | Python | HTTP API | Yes | Existing foundation; all new routes live in `app/api/intelligence.py`. |
| Starlette | https://github.com/encode/starlette | BSD-3-Clause | 1.10.0 | Python | ASGI toolkit (FastAPI dependency) | Yes | |
| Pydantic | https://github.com/pydantic/pydantic | MIT | 2.14.3 | Python | Request/response validation | Yes | |
| pydantic-settings | https://github.com/pydantic/pydantic-settings | MIT | 2.16.2 | Python | Environment settings | Yes | |
| SQLAlchemy | https://github.com/sqlalchemy/sqlalchemy | MIT | 2.1.8 | Python | ORM and schema | Yes | Existing foundation. |
| Alembic | https://github.com/sqlalchemy/alembic | MIT | 1.20.1 | Python | Migrations | Yes | Migration `003_intelligence` is additive. |
| Uvicorn | https://github.com/encode/uvicorn | BSD-3-Clause | 0.54.0 | Python | Local ASGI server | Yes | Loopback default. |
| SQLite | https://sqlite.org | Public domain | 3.54.1 | C | Database | Yes | Includes FTS5 (`transcript_fts`, `knowledge_fts`, existing video FTS). |
| pytest | https://github.com/pytest-dev/pytest | MIT | 9.2.0 | Python | Test runner (dev) | Yes | |
| httpx | https://github.com/encode/httpx | BSD-3-Clause | 0.28.1 | Python | TestClient transport (dev) | Yes | |
| yt-dlp | https://github.com/yt-dlp/yt-dlp | Unlicense | not installed in this environment | Python | Existing metadata/caption adapter | No | Only used by the existing build/sync path. It is not required by the intelligence API and is not invoked implicitly. |

The frontend remains dependency-free vanilla JavaScript/CSS in `site/index.html`.
Graph rendering uses inline SVG, not Cytoscape.js, so no new browser dependency or CDN
was added.

## 2. Algorithms implemented in-repo (no new library)

| Algorithm | Source | License status of reference | Implementation |
| --- | --- | --- | --- |
| FSRS-5 scheduling | https://github.com/open-spaced-repetition | MIT (py-fsrs / ts-fsrs) | Independent stdlib implementation in `backend/app/services/learning/fsrs.py`; 19 default weights. No model weights. |
| BM25 / FTS5 retrieval | SQLite FTS5, Robertson & Zaragoza | Public domain / published algorithm | Existing FTS plus `intel_fts.py`. |
| TF-IDF, cosine, signed hashing | Published IR algorithms | Public domain mathematics | `services/knowledge/text.py`, `services/video/chapters.py`, `services/ai/embeddings.py`. |
| Reciprocal-rank fusion | Cormack et al., SIGIR 2009 | Published algorithm | `services/knowledge/ask.py`. |
| MMR diversification, BPR ranking | Carbonell & Goldstein 1998; Rendle et al. 2009 | Published algorithms | Existing `services/recommendations.py`, reused unchanged by perspectives. |
| Topological prerequisite order | Kahn's algorithm | Published algorithm | `services/learning/paths.py`. |

## 3. Optional local AI software (evaluated; **not bundled, not installed**)

| Candidate | Repository | License | Verdict | Why |
| --- | --- | --- | --- | --- |
| Ollama | https://github.com/ollama/ollama | MIT | Supported optional backend | Loopback HTTP adapter; no model download. |
| llama.cpp | https://github.com/ggml-org/llama.cpp | MIT | Supported optional backend | OpenAI-compatible local server endpoint. |
| whisper.cpp | https://github.com/ggml-org/whisper.cpp | MIT | Evaluated, not integrated | ASR remains a user-supplied transcript path; no model is downloaded. |
| faster-whisper | https://github.com/SYSTRAN/faster-whisper | MIT | Evaluated, not integrated | Same reason; model weights have separate terms. |
| sentence-transformers | https://github.com/UKPLab/sentence-transformers | Apache-2.0 | Opt-in only if already installed | `MYTUBE_EMBEDDING_MODEL` selects an installed model; otherwise deterministic hashing. |
| FastEmbed | https://github.com/qdrant/fastembed | Apache-2.0 | Evaluated, not adopted | Would add another embedding stack without a demonstrated need. |
| Haystack | https://github.com/deepset-ai/haystack | Apache-2.0 | Rejected for now | Overlaps with a small inspectable retrieval pipeline; violates “no unnecessary frameworks”. |
| LlamaIndex | https://github.com/run-llama/llama_index | MIT | Rejected for now | Same reason. |
| Cytoscape.js | https://github.com/cytoscape/cytoscape.js | MIT | Evaluated, not adopted | Inline SVG covers the required graph interactions without a CDN. |
| Graphology | https://github.com/graphology/graphology | MIT | Evaluated, not adopted | Graph analysis is small enough server-side. |
| py-fsrs / ts-fsrs | https://github.com/open-spaced-repetition | MIT | Evaluated; algorithm reimplemented | Keeps the backend dependency-free and review history portable. |

## 4. Model-weight licensing (important)

**No model weights are distributed with My Tube and none are downloaded automatically.**
If a user installs a model for Ollama/llama.cpp/whisper.cpp/sentence-transformers, the
model's own license applies and must be checked independently. Examples, not endorsements:

- Llama 3.x: Meta Llama Community License (not OSI-approved; acceptable-use and naming terms).
- Mistral / Mixtral: Apache-2.0 for many released weights; verify per checkpoint.
- Qwen: Apache-2.0 for many releases; verify per checkpoint.
- Gemma: Gemma Terms of Use (not OSI-approved).
- Whisper weights: MIT in OpenAI's release, but redistributions can carry additional terms.
- Sentence-transformers checkpoints: varied (Apache-2.0, MIT, CC-BY-NC). Check each model card.

The software license and the weights license are separate. A permissively licensed
runtime does not make every checkpoint permissive.

## 5. Network boundaries

| Action | Network? | Trigger |
| --- | --- | --- |
| Library pages, search, graph, paths, flashcards, analytics, radar | No | Local SQLite/API. |
| Thumbnails / embedded playback | Yes | Browser loads YouTube assets; existing behavior. |
| Metadata build (`build.py`/yt-dlp) | Yes | Explicit user build/sync action. |
| Transcript download | Not implemented here | User supplies captions/text or a locally produced transcript. |
| Local LLM inference | Loopback only by default | Explicit `MYTUBE_AI_ENABLED=1` or UI selection. |
| Telemetry / analytics upload | Never | No telemetry code exists. |

## 6. Rejected categories

- Proprietary hosted LLM, embedding, transcription or vector APIs (OpenAI, Anthropic,
  Gemini, hosted Pinecone/Weaviate, etc.).
- Mandatory accounts, subscriptions or paid tiers.
- Browser CDNs for graph/chart libraries in this increment.
- Undocumented YouTube APIs, cookie/password collection, or authentication bypasses.
