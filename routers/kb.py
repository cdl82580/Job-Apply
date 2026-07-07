"""
routers/kb.py — Knowledge Base article management.

Public endpoints (any authenticated user):
  GET  /api/kb/articles          — list all articles
  GET  /api/kb/articles/{id}     — get one article

Admin-only endpoints:
  POST   /api/admin/kb/articles              — create article
  PUT    /api/admin/kb/articles/{id}         — update article
  DELETE /api/admin/kb/articles/{id}         — delete (status 204)
  PUT    /api/admin/kb/categories/{id}       — update a category label/desc/icon
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from scripts import storage

router = APIRouter(tags=["kb"])

_KB_KEY = "kb/data.json"


# ---------------------------------------------------------------------------
# Seed data — used when no KB has been saved yet
# ---------------------------------------------------------------------------

_SEED_CATEGORIES = [
    {"id": "getting-started", "label": "Getting Started",      "icon": "🚀", "desc": "First steps, workflow overview, and how the agent produces your documents."},
    {"id": "role-types",      "label": "Role Types & Framing", "icon": "🎯", "desc": "Framing angles and strategy for each role category the agent supports."},
    {"id": "resume",          "label": "Resume Tailoring",     "icon": "📄", "desc": "How the agent customizes the master resume — competencies, bullets, and structure."},
    {"id": "cover-letter",    "label": "Cover Letter",         "icon": "✉️",  "desc": "Structure, tone rules, opening hooks, and evidence paragraph strategy."},
    {"id": "ats",             "label": "ATS Optimization",     "icon": "🤖", "desc": "How the ATS resume is built and what makes it parser-friendly."},
    {"id": "workflow",        "label": "Workflow & Web App",   "icon": "⚙️",  "desc": "Using the web interface, running the agent, and the interview prep workflow."},
    {"id": "slack",           "label": "Slack Integration",    "icon": "💬", "iconImg": "/img/slack-icon.svg", "desc": "Slash commands for running the agent, tracking applications, and calendar management."},
    {"id": "teams",           "label": "Teams Integration",    "icon": "👥", "iconImg": "/img/teams-icon.svg", "desc": "Chat commands for running the agent, tracking applications, and calendar management from Microsoft Teams."},
    {"id": "admin",           "label": "Admin",                "icon": "🔐", "desc": "Webhooks, system architecture, Drive integration, XML editing, and troubleshooting.", "adminOnly": True},
]


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def _load() -> dict:
    raw = storage.get_text(_KB_KEY)
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return {"categories": _SEED_CATEGORIES, "articles": []}


def _save(data: dict) -> None:
    storage.put_text(_KB_KEY, json.dumps(data, ensure_ascii=False))


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _require_auth(request: Request) -> dict:
    from api import _require_user  # noqa: PLC0415
    return _require_user(request)


def _require_admin(request: Request) -> dict:
    from api import _require_admin as _ra  # noqa: PLC0415
    return _ra(request)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class ArticleCreate(BaseModel):
    id:        str | None = None
    category:  str
    title:     str
    snippet:   str = ""
    read_time: str = "3 min"
    body:      str = ""
    admin_only: bool = False


class ArticleUpdate(BaseModel):
    category:  str | None = None
    title:     str | None = None
    snippet:   str | None = None
    read_time: str | None = None
    body:      str | None = None
    admin_only: bool | None = None
    sort_order: int | None = None


class CategoryCreate(BaseModel):
    id:        str
    label:     str
    icon:      str = ""
    desc:      str = ""
    admin_only: bool = False


class CategoryUpdate(BaseModel):
    label:     str | None = None
    icon:      str | None = None
    desc:      str | None = None
    admin_only: bool | None = None


# ---------------------------------------------------------------------------
# Public endpoints
# ---------------------------------------------------------------------------

@router.get("/api/kb/articles")
async def list_articles(request: Request):
    _require_auth(request)
    data = _load()
    return {
        "categories": data.get("categories", _SEED_CATEGORIES),
        "articles":   data.get("articles", []),
    }


@router.get("/api/kb/articles/{article_id}")
async def get_article(article_id: str, request: Request):
    _require_auth(request)
    data = _load()
    for a in data.get("articles", []):
        if a["id"] == article_id:
            return a
    raise HTTPException(404, "Article not found")


@router.get("/api/kb/categories")
async def list_categories(request: Request):
    _require_auth(request)
    data = _load()
    return data.get("categories", _SEED_CATEGORIES)


# ---------------------------------------------------------------------------
# Admin endpoints — articles
# ---------------------------------------------------------------------------

@router.post("/api/admin/kb/articles", status_code=201)
async def create_article(body: ArticleCreate, request: Request):
    admin = _require_admin(request)
    data = _load()
    articles = data.get("articles", [])

    art_id = (body.id or "").strip() or str(uuid.uuid4())[:8]
    if any(a["id"] == art_id for a in articles):
        raise HTTPException(400, f"Article ID '{art_id}' already exists")

    now = _now()
    article: dict[str, Any] = {
        "id":         art_id,
        "category":   body.category,
        "title":      body.title.strip(),
        "snippet":    body.snippet.strip(),
        "readTime":   body.read_time,
        "body":       body.body,
        "adminOnly":  body.admin_only,
        "created_at": now,
        "updated_at": now,
        "created_by": admin["email"],
    }
    articles.append(article)
    data["articles"] = articles
    _save(data)
    return article


@router.put("/api/admin/kb/articles/{article_id}")
async def update_article(article_id: str, body: ArticleUpdate, request: Request):
    admin = _require_admin(request)
    data = _load()
    articles = data.get("articles", [])

    for i, a in enumerate(articles):
        if a["id"] == article_id:
            if body.category  is not None: a["category"]  = body.category
            if body.title     is not None: a["title"]      = body.title.strip()
            if body.snippet   is not None: a["snippet"]    = body.snippet.strip()
            if body.read_time is not None: a["readTime"]   = body.read_time
            if body.body      is not None: a["body"]       = body.body
            if body.admin_only is not None: a["adminOnly"] = body.admin_only
            a["updated_at"] = _now()
            a["updated_by"] = admin["email"]
            articles[i] = a
            data["articles"] = articles
            _save(data)
            return a

    raise HTTPException(404, "Article not found")


@router.delete("/api/admin/kb/articles/{article_id}", status_code=204)
async def delete_article(article_id: str, request: Request):
    _require_admin(request)
    data = _load()
    before = len(data.get("articles", []))
    data["articles"] = [a for a in data.get("articles", []) if a["id"] != article_id]
    if len(data["articles"]) == before:
        raise HTTPException(404, "Article not found")
    _save(data)


# ---------------------------------------------------------------------------
# Admin endpoints — seed from frontend KB const
# ---------------------------------------------------------------------------

class SeedPayload(BaseModel):
    categories: list[dict]
    articles:   list[dict]


@router.post("/api/admin/kb/seed", status_code=200)
async def seed_kb(body: SeedPayload, request: Request):
    """Replace the stored KB with the data sent from the frontend seed."""
    admin = _require_admin(request)
    now = _now()
    for a in body.articles:
        a.setdefault("created_at", now)
        a.setdefault("updated_at", now)
        a.setdefault("created_by", admin["email"])
    data = {"categories": body.categories, "articles": body.articles}
    _save(data)
    return {"ok": True, "articles": len(body.articles), "categories": len(body.categories)}


@router.post("/api/admin/kb/seed-from-file", status_code=200)
async def seed_kb_from_file(request: Request):
    """Extract KB data from frontend/kb.html via Node.js and seed to storage."""
    import subprocess  # noqa: PLC0415
    import os          # noqa: PLC0415

    admin = _require_admin(request)

    kb_path = os.path.join(os.path.dirname(__file__), "..", "frontend", "kb.html")
    kb_path = os.path.abspath(kb_path)

    if not os.path.exists(kb_path):
        raise HTTPException(500, "frontend/kb.html not found on server")

    with open(kb_path, "r", encoding="utf-8") as f:
        kb_html = f.read()

    # Embed the HTML directly in the script to avoid process.argv index differences
    node_script = (
        "const html = " + json.dumps(kb_html) + r""";
const start = html.indexOf('const KB = {');
if (start === -1) { console.error('KB const not found'); process.exit(1); }
const sub = html.slice(start + 'const KB = '.length);
let depth = 0, i = 0, inStr = false, strChar = '', inTemplate = 0;
for (; i < sub.length; i++) {
  const c = sub[i];
  if (inStr) {
    if (c === '\\') { i++; continue; }
    if (c === strChar) inStr = false;
    continue;
  }
  if (c === '`') { inTemplate = inTemplate ? 0 : 1; continue; }
  if (inTemplate) { if (c === '\\') { i++; } continue; }
  if (c === '"' || c === "'") { inStr = true; strChar = c; continue; }
  if (c === '{') depth++;
  else if (c === '}') { depth--; if (depth === 0) { i++; break; } }
}
const objSrc = sub.slice(0, i);
let KB;
try { KB = eval('(' + objSrc + ')'); } catch(e) { console.error('eval failed: ' + e.message); process.exit(1); }
console.log(JSON.stringify(KB));
"""
    )
    try:
        result = subprocess.run(
            ["node", "-e", node_script],
            capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError:
        raise HTTPException(500, "Node.js not available on server")
    except subprocess.TimeoutExpired:
        raise HTTPException(500, "Node.js script timed out")

    if result.returncode != 0:
        raise HTTPException(500, f"KB extraction failed: {result.stderr.strip()}")

    try:
        kb_data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise HTTPException(500, f"KB JSON parse failed: {exc}")

    now = _now()
    articles = []
    for a in kb_data.get("articles", []):
        articles.append({
            "id":         a.get("id", ""),
            "category":   a.get("category", ""),
            "title":      a.get("title", ""),
            "snippet":    a.get("snippet", ""),
            "readTime":   a.get("readTime", "3 min"),
            "body":       a.get("body", ""),
            "adminOnly":  bool(a.get("adminOnly", False)),
            "created_at": now,
            "updated_at": now,
            "created_by": admin["email"],
        })

    categories = [
        {
            "id":        c.get("id", ""),
            "label":     c.get("label", ""),
            "icon":      c.get("icon", ""),
            "iconImg":   c.get("iconImg", ""),
            "desc":      c.get("desc", ""),
            "adminOnly": bool(c.get("adminOnly", False)),
        }
        for c in kb_data.get("categories", _SEED_CATEGORIES)
    ]

    data = {"categories": categories, "articles": articles}
    _save(data)
    return {"ok": True, "articles": len(articles), "categories": len(categories)}


# ---------------------------------------------------------------------------
# Admin endpoints — categories
# ---------------------------------------------------------------------------

@router.post("/api/admin/kb/categories", status_code=201)
async def create_category(body: CategoryCreate, request: Request):
    _require_admin(request)
    data = _load()
    categories = data.get("categories", list(_SEED_CATEGORIES))
    cat_id = body.id.strip().lower().replace(" ", "-")
    if not cat_id:
        raise HTTPException(400, "Category ID is required")
    if any(c["id"] == cat_id for c in categories):
        raise HTTPException(400, f"Category ID '{cat_id}' already exists")
    cat = {
        "id":        cat_id,
        "label":     body.label.strip(),
        "icon":      body.icon,
        "desc":      body.desc,
        "adminOnly": body.admin_only,
    }
    categories.append(cat)
    data["categories"] = categories
    _save(data)
    return cat


@router.delete("/api/admin/kb/categories/{category_id}", status_code=204)
async def delete_category(category_id: str, request: Request):
    _require_admin(request)
    data = _load()
    categories = data.get("categories", list(_SEED_CATEGORIES))
    if not any(c["id"] == category_id for c in categories):
        raise HTTPException(404, "Category not found")
    data["categories"] = [c for c in categories if c["id"] != category_id]
    _save(data)


@router.put("/api/admin/kb/categories/{category_id}")
async def update_category(category_id: str, body: CategoryUpdate, request: Request):
    admin = _require_admin(request)
    data = _load()
    categories = data.get("categories", list(_SEED_CATEGORIES))

    for i, c in enumerate(categories):
        if c["id"] == category_id:
            if body.label     is not None: c["label"]     = body.label
            if body.icon      is not None: c["icon"]      = body.icon
            if body.desc      is not None: c["desc"]      = body.desc
            if body.admin_only is not None: c["adminOnly"] = body.admin_only
            categories[i] = c
            data["categories"] = categories
            _save(data)
            return c

    raise HTTPException(404, "Category not found")
