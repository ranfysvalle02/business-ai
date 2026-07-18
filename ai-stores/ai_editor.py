"""
AI Stores — conversational store editor (Google Gemini, JSON response mode).

This module is the brain behind the admin chat widget. It is deliberately
*safe by construction*:

    * The LLM never writes to the database. It only proposes a list of
      structured operations ("ops").
    * Every proposed op is validated here against the manifest (collection
      schemas, enums, the section-type registry) before the admin ever sees
      it, and re-validated again at apply time.
    * Applying only happens after the admin confirms; writes go through the
      scoped DB and every change is recorded in ``audit_log``.

Scope (by product decision): layout/sections, store copy/info, catalog
items, and specials. Theme/appearance is intentionally *out of scope* — the
dedicated Appearance panel handles that — so the assistant redirects those.

Runtime is Google's Gemini API (``generativelanguage.googleapis.com``) driven
in **structured-output / JSON response mode**: we set
``responseMimeType="application/json"`` plus a ``responseSchema`` so the model
is *constrained* to emit a single JSON object ``{reply, ops:[{tool, args}]}``.
Each op's ``args`` is itself a JSON-encoded string, which keeps the schema
valid while letting per-tool arguments stay free-form. Structured output and
function-calling can't be combined in one request, so we describe the tool
contract in the system prompt and let the schema do the constraining. All
trust still lives in the validation + confirm steps below.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from bson import ObjectId
from bson.errors import InvalidId

logger = logging.getLogger("ai-stores.ai_editor")

# ── Gemini configuration ──────────────────────────────────────────────────
# GEMINI_API_KEY is the only required setting; the editor stays disabled (routes
# return 503 with a clear message) until it is present. GEMINI_MODEL defaults to
# the latest flash model — fast, cheap, and strong at structured output — and is
# swappable to any generateContent-capable model (e.g. gemini-2.5-flash,
# gemini-3.1-pro-preview) without code changes.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash").strip()
GEMINI_API_BASE = os.getenv(
    "GEMINI_API_BASE", "https://generativelanguage.googleapis.com/v1beta"
).rstrip("/")
GEMINI_TIMEOUT = float(os.getenv("GEMINI_TIMEOUT", "60"))
GEMINI_TEMPERATURE = float(os.getenv("GEMINI_TEMPERATURE", "0.1"))

ITEM_STATUS_ENUM = ["Available", "Limited Availability", "Pending", "Sold"]

# Human-facing store fields the chat may edit. Everything else (theme_*, fonts,
# radius, slug_id, analytics IDs, logo/hero URLs, embed HTML) is off-limits.
STORE_COPY_FIELDS = {
    "name", "business_type", "tagline", "about_text", "address", "email",
    "phone", "phone_display", "hours", "inventory_description",
    "cta_label", "cta_href", "currency", "price_range", "seo_keywords", "socials",
}


class AIEditorError(Exception):
    """Raised when the AI backend is unreachable or misconfigured."""


# ── Manifest-derived config ───────────────────────────────────────────────

def section_registry(manifest: dict) -> dict[str, Any]:
    """The declarative section-type registry, if present."""
    return manifest.get("section_types") or {}


def section_type_enum(manifest: dict) -> list[str]:
    """Allowed section ``type`` values — registry first, schema enum fallback."""
    reg = section_registry(manifest)
    if reg:
        return list(reg.keys())
    try:
        return manifest["collections"]["sections"]["schema"]["properties"]["type"]["enum"]
    except (KeyError, TypeError):
        return ["hero", "catalog", "specials", "richtext", "gallery", "contact", "cta"]


def _section_settings_spec(manifest: dict, stype: str) -> dict[str, Any]:
    """Per-type settings schema from the registry (may be empty)."""
    return (section_registry(manifest).get(stype, {}) or {}).get("settings", {}) or {}


# ── Tool schemas ───────────────────────────────────────────────────────────
# The single source of truth for the ops the assistant may propose. In JSON
# response mode these are not sent as callable "tools"; instead they render the
# tool contract in the system prompt (``_tools_contract``) and supply the
# ``tool`` enum for the response schema (``response_schema``).

def build_tools(manifest: dict) -> list[dict[str, Any]]:
    stypes = section_type_enum(manifest)

    def fn(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required or [],
                },
            },
        }

    return [
        # ── Layout / sections ──
        fn("add_section", "Add a new section to the home page layout.", {
            "type": {"type": "string", "enum": stypes, "description": "The section type to add."},
            "title": {"type": "string", "description": "Admin-facing label for the section."},
            "position": {"type": "string", "description": "Where to place it: 'start', 'end', 'after:<key>', or 'before:<key>'. Default 'end'."},
            "settings": {"type": "object", "description": "Optional section settings (e.g. heading, columns)."},
        }, ["type"]),
        fn("remove_section", "Remove a section from the layout by its key.", {
            "key": {"type": "string", "description": "The existing section key to remove."},
        }, ["key"]),
        fn("reorder_sections", "Set the order of sections. Provide keys in the desired top-to-bottom order.", {
            "order": {"type": "array", "items": {"type": "string"}, "description": "Section keys in the new order."},
        }, ["order"]),
        fn("toggle_section", "Show or hide a section without deleting it.", {
            "key": {"type": "string", "description": "The section key."},
            "visible": {"type": "boolean", "description": "true to show, false to hide."},
        }, ["key", "visible"]),
        fn("update_section_settings", "Change a section's settings (e.g. heading, subheading, columns).", {
            "key": {"type": "string", "description": "The section key."},
            "settings": {"type": "object", "description": "Settings to merge into the section."},
        }, ["key", "settings"]),
        # ── Store copy / info ──
        fn("update_store_info", "Update store copy/info fields (name, tagline, about, hours, address, contact, CTA). Do NOT use for colors/fonts/theme.", {
            "fields": {"type": "object", "description": "Object of store fields to set, e.g. {\"tagline\": \"...\"}."},
        }, ["fields"]),
        # ── Catalog items ──
        fn("create_item", "Create a new catalog item.", {
            "name": {"type": "string"},
            "price": {"type": "number"},
            "category": {"type": "string"},
            "description": {"type": "string"},
            "status": {"type": "string", "enum": ITEM_STATUS_ENUM},
            "item_code": {"type": "string", "description": "Optional unique code; auto-generated from the name if omitted."},
        }, ["name"]),
        fn("update_item", "Update fields on an existing catalog item.", {
            "selector": {"type": "string", "description": "The item's item_code or exact name."},
            "fields": {"type": "object", "description": "Fields to change, e.g. {\"price\": 49}."},
        }, ["selector", "fields"]),
        fn("set_item_status", "Set an item's availability status.", {
            "selector": {"type": "string", "description": "The item's item_code or exact name."},
            "status": {"type": "string", "enum": ITEM_STATUS_ENUM},
        }, ["selector", "status"]),
        # ── Specials ──
        fn("create_special", "Create a special offer or announcement.", {
            "title": {"type": "string"},
            "content": {"type": "string", "description": "The body text of the announcement."},
            "description": {"type": "string"},
            "discount_percent": {"type": "number"},
            "valid_until": {"type": "string", "description": "Optional ISO date the special is valid until."},
        }, ["title", "content"]),
        fn("update_special", "Update an existing special.", {
            "selector": {"type": "string", "description": "The special's exact title."},
            "fields": {"type": "object"},
        }, ["selector", "fields"]),
    ]


# ── Snapshot (grounding context) ──────────────────────────────────────────

async def build_snapshot(db) -> dict[str, Any]:
    """Compact, id-bearing view of the store used for grounding + resolution."""
    store = await db["stores"].find_one({}) or {}
    sections = []
    async for s in db["sections"].find({}).sort("order", 1):
        sections.append({
            "_id": str(s.get("_id")),
            "key": s.get("key"),
            "type": s.get("type"),
            "title": s.get("title"),
            "order": s.get("order"),
            "visible": s.get("visible", True),
            "settings": s.get("settings", {}),
        })
    items = []
    async for it in db["items"].find({}).sort("date_added", -1).limit(100):
        items.append({
            "_id": str(it.get("_id")),
            "item_code": it.get("item_code"),
            "name": it.get("name"),
            "price": it.get("price"),
            "status": it.get("status"),
            "category": it.get("category"),
        })
    specials = []
    async for sp in db["specials"].find({}).sort("date_created", -1).limit(50):
        specials.append({
            "_id": str(sp.get("_id")),
            "title": sp.get("title"),
            "discount_percent": sp.get("discount_percent"),
            "valid_until": sp.get("valid_until"),
        })

    store_copy = {k: store.get(k) for k in STORE_COPY_FIELDS if k in store}
    store_copy["_id"] = str(store.get("_id")) if store.get("_id") else None
    return {"store": store_copy, "sections": sections, "items": items, "specials": specials}


def _tools_contract(manifest: dict) -> str:
    """Render the tool set as a compact, prompt-friendly contract."""
    lines: list[str] = []
    for tool in build_tools(manifest):
        fn = tool["function"]
        params = fn.get("parameters", {}) or {}
        required = set(params.get("required") or [])
        arg_specs: list[str] = []
        for pname, pspec in (params.get("properties") or {}).items():
            frag = pname
            flags = [pspec.get("type", "")]
            if pname in required:
                flags.append("required")
            frag += f" ({', '.join(f for f in flags if f)})"
            if pspec.get("enum"):
                frag += f" one of {pspec['enum']}"
            if pspec.get("description"):
                frag += f" — {pspec['description']}"
            arg_specs.append(frag)
        lines.append(f"- {fn['name']}: {fn['description']}")
        if arg_specs:
            lines.append("    args: " + "; ".join(arg_specs))
    return "\n".join(lines)


def build_system_prompt(snapshot: dict, manifest: dict) -> str:
    stypes = ", ".join(section_type_enum(manifest))
    compact = {
        "store": {k: v for k, v in snapshot["store"].items() if k != "_id"},
        "sections": [{"key": s["key"], "type": s["type"], "order": s["order"], "visible": s["visible"]} for s in snapshot["sections"]],
        "items": [{"item_code": i["item_code"], "name": i["name"], "price": i["price"], "status": i["status"]} for i in snapshot["items"][:40]],
        "specials": [{"title": s["title"]} for s in snapshot["specials"][:20]],
    }
    return (
        "You are the assistant inside an online store's admin panel. You help the "
        "owner change their store. Only make changes the user explicitly asks for.\n\n"
        "Respond with a SINGLE JSON object of the form "
        '{"reply": string, "ops": [{"tool": string, "args": string}]}. '
        "Each op's `args` is a JSON object of that tool's arguments, encoded as a "
        'JSON string — e.g. {"tool": "update_store_info", '
        '"args": "{\\"tagline\\": \\"Fresh daily\\"}"}.\n\n'
        "Rules:\n"
        f"- Valid section types: {stypes}.\n"
        "- Use the EXACT existing keys, item_codes, and titles from the store state below. "
        "Do not invent fields or values.\n"
        "- To make a change, add one or more ops and leave `reply` empty (the UI shows a "
        "diff the owner confirms before anything is saved).\n"
        "- If the request is ambiguous or missing required info (e.g. which item, what price), "
        "return `ops: []` and put a short clarifying question in `reply`.\n"
        "- You cannot change colors, fonts, or the visual theme. If asked, return `ops: []` and "
        "tell the user to use the Appearance panel on the dashboard.\n\n"
        "Available tools:\n" + _tools_contract(manifest) + "\n\n"
        "Current store state (JSON):\n" + json.dumps(compact, ensure_ascii=False)
    )


# ── Gemini call (JSON response mode) ───────────────────────────────────────

def _parse_args(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def response_schema(manifest: dict) -> dict[str, Any]:
    """The Gemini ``responseSchema`` that constrains the model to our contract.

    ``tool`` is limited to the known tool names; ``args`` is a JSON-encoded
    string so per-tool arguments stay free-form while the schema stays valid.
    """
    tool_names = [t["function"]["name"] for t in build_tools(manifest)]
    return {
        "type": "OBJECT",
        "properties": {
            "reply": {
                "type": "STRING",
                "description": "A short message to the owner: a clarifying question when the request is ambiguous or out of scope, otherwise an empty string.",
            },
            "ops": {
                "type": "ARRAY",
                "description": "Ordered changes to propose. Empty when you are asking for clarification.",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "tool": {"type": "STRING", "enum": tool_names},
                        "args": {
                            "type": "STRING",
                            "description": "The chosen tool's arguments as a JSON object, encoded as a JSON string.",
                        },
                    },
                    "required": ["tool", "args"],
                },
            },
        },
        "required": ["reply", "ops"],
        "propertyOrdering": ["reply", "ops"],
    }


def _to_gemini_contents(messages: list[dict]) -> list[dict[str, Any]]:
    """Map chat turns to Gemini ``contents`` (assistant → ``model`` role)."""
    contents: list[dict[str, Any]] = []
    for m in messages:
        text = str(m.get("content") or "").strip()
        if not text:
            continue
        role = "model" if m.get("role") == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": text}]})
    return contents


def build_payload(messages: list[dict], snapshot: dict, manifest: dict) -> dict[str, Any]:
    """Assemble the ``generateContent`` request body (JSON response mode)."""
    return {
        "systemInstruction": {"parts": [{"text": build_system_prompt(snapshot, manifest)}]},
        "contents": _to_gemini_contents(messages),
        "generationConfig": {
            "temperature": GEMINI_TEMPERATURE,
            "responseMimeType": "application/json",
            "responseSchema": response_schema(manifest),
        },
    }


def _gemini_error_message(resp: "httpx.Response") -> str:
    try:
        return str((resp.json().get("error") or {}).get("message") or "").strip()
    except Exception:  # noqa: BLE001
        return resp.text[:200]


def parse_gemini_response(data: dict) -> dict[str, Any]:
    """Turn a ``generateContent`` response into ``{'ops': [...]}`` or ``{'reply': str}``."""
    if (data.get("promptFeedback") or {}).get("blockReason"):
        return {"reply": "I can't help with that request. Try describing the store change differently."}

    candidates = data.get("candidates") or []
    if not candidates:
        return {"reply": "I'm not sure how to help with that yet."}

    cand = candidates[0]
    parts = ((cand.get("content") or {}).get("parts")) or []
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
    if not text:
        if cand.get("finishReason") == "MAX_TOKENS":
            return {"reply": "That was a bit much to handle in one step — try a smaller, more specific change."}
        return {"reply": "I'm not sure how to help with that yet."}

    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        # JSON mode should guarantee valid JSON; fall back to the raw text.
        return {"reply": text[:500]}
    if not isinstance(obj, dict):
        return {"reply": "I'm not sure how to help with that yet."}

    ops: list[dict[str, Any]] = []
    for item in obj.get("ops") or []:
        if isinstance(item, dict) and item.get("tool"):
            ops.append({"tool": item["tool"], "args": _parse_args(item.get("args"))})
    if ops:
        return {"ops": ops}

    reply = str(obj.get("reply") or "").strip()
    return {"reply": reply or "I couldn't turn that into a change. Could you rephrase?"}


async def propose(messages: list[dict], snapshot: dict, manifest: dict) -> dict[str, Any]:
    """Ask Gemini for structured ops. Returns ``{'reply': str}`` or ``{'ops': [...]}``."""
    if not GEMINI_API_KEY:
        raise AIEditorError(
            "The AI editor isn't configured. Set GEMINI_API_KEY to enable it."
        )

    url = f"{GEMINI_API_BASE}/models/{GEMINI_MODEL}:generateContent"
    payload = build_payload(messages, snapshot, manifest)
    try:
        async with httpx.AsyncClient(timeout=GEMINI_TIMEOUT) as client:
            resp = await client.post(
                url,
                headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
                json=payload,
            )
    except httpx.HTTPError as exc:
        raise AIEditorError("Couldn't reach the Gemini API. Check your network connection.") from exc

    if resp.status_code in (401, 403):
        raise AIEditorError("Gemini rejected the API key. Check GEMINI_API_KEY.")
    if resp.status_code == 404:
        raise AIEditorError(
            f"Gemini model '{GEMINI_MODEL}' was not found. Set GEMINI_MODEL to an available model."
        )
    if resp.status_code == 429:
        raise AIEditorError("Gemini is rate-limiting requests. Wait a moment and try again.")
    if resp.status_code >= 400:
        raise AIEditorError(f"Gemini error: {_gemini_error_message(resp) or resp.status_code}")

    return parse_gemini_response(resp.json())


# ── Validation + normalization ────────────────────────────────────────────

def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return s or "item"


def _find_section(snapshot: dict, key: str) -> dict | None:
    return next((s for s in snapshot["sections"] if s.get("key") == key), None)


def _find_item(snapshot: dict, selector: str) -> dict | None:
    sel = str(selector or "").strip()
    low = sel.lower()
    return next(
        (i for i in snapshot["items"]
         if i.get("item_code") == sel or str(i.get("name", "")).lower() == low),
        None,
    )


def _find_special(snapshot: dict, selector: str) -> dict | None:
    low = str(selector or "").strip().lower()
    return next((s for s in snapshot["specials"] if str(s.get("title", "")).lower() == low), None)


def _coerce_number(val: Any) -> float | None:
    if val is None or val == "":
        return None
    try:
        return float(str(val).replace("$", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _ordered_keys(snapshot: dict) -> list[str]:
    return [s["key"] for s in sorted(snapshot["sections"], key=lambda s: (s.get("order") or 0)) if s.get("key")]


def validate_ops(ops: list[dict], snapshot: dict, manifest: dict) -> tuple[list[dict], list[str], list[str], list[dict]]:
    """Validate raw ops against the manifest + current state.

    Returns ``(normalized_ops, diff_lines, errors, valid_raw)`` where
    ``normalized_ops`` are ready to execute, ``valid_raw`` are the original
    ``{tool,args}`` ops that passed (so they can be re-validated at apply
    time against a fresh snapshot), and invalid ops are dropped with a
    human-readable reason in ``errors``.
    """
    stypes = set(section_type_enum(manifest))
    existing_keys = {s["key"] for s in snapshot["sections"]}
    normalized: list[dict] = []
    diff: list[str] = []
    errors: list[str] = []
    valid_raw: list[dict] = []
    _before = 0

    for op in ops:
        tool = op.get("tool")
        a = op.get("args", {}) or {}
        _before = len(normalized)
        try:
            if tool == "add_section":
                stype = a.get("type")
                if stype not in stypes:
                    errors.append(f"Unknown section type '{stype}'.")
                    continue
                # Unique key derived from type.
                base = _slug(stype)
                key = base
                n = 2
                taken = set(existing_keys)
                while key in taken:
                    key = f"{base}-{n}"
                    n += 1
                position = a.get("position") or "end"
                settings = a.get("settings") if isinstance(a.get("settings"), dict) else {}
                title = a.get("title") or f"{stype.capitalize()} section"
                new_order = _compute_insert_order(snapshot, position)
                normalized.append({
                    "tool": tool,
                    "doc": {
                        "key": key, "type": stype, "title": title,
                        "order": new_order, "visible": True, "anchor": key,
                        "settings": settings,
                    },
                    "resequence": _sequence_with_insert(snapshot, key, position),
                })
                existing_keys.add(key)
                diff.append(f"Add section: {stype} (\"{title}\") at {position}")

            elif tool == "remove_section":
                sec = _find_section(snapshot, a.get("key"))
                if not sec:
                    errors.append(f"No section with key '{a.get('key')}' to remove.")
                    continue
                normalized.append({"tool": tool, "key": sec["key"], "id": sec["_id"]})
                diff.append(f"Remove section: {sec['key']} ({sec['type']})")

            elif tool == "reorder_sections":
                order = [k for k in (a.get("order") or []) if k in existing_keys]
                if not order:
                    errors.append("Reorder needs valid existing section keys.")
                    continue
                # Append any omitted keys in their current order.
                for k in _ordered_keys(snapshot):
                    if k not in order:
                        order.append(k)
                normalized.append({"tool": tool, "order": order})
                diff.append("Reorder sections: " + ", ".join(order))

            elif tool == "toggle_section":
                sec = _find_section(snapshot, a.get("key"))
                if not sec:
                    errors.append(f"No section with key '{a.get('key')}' to toggle.")
                    continue
                visible = bool(a.get("visible"))
                normalized.append({"tool": tool, "key": sec["key"], "id": sec["_id"], "visible": visible})
                diff.append(f"{'Show' if visible else 'Hide'} section: {sec['key']}")

            elif tool == "update_section_settings":
                sec = _find_section(snapshot, a.get("key"))
                if not sec:
                    errors.append(f"No section with key '{a.get('key')}' to update.")
                    continue
                incoming = a.get("settings") if isinstance(a.get("settings"), dict) else {}
                clean = _validate_settings(incoming, sec["type"], manifest, errors)
                if not clean:
                    continue
                merged = {**(sec.get("settings") or {}), **clean}
                normalized.append({"tool": tool, "key": sec["key"], "id": sec["_id"], "settings": merged})
                diff.append(f"Update {sec['key']} settings: " + ", ".join(f"{k}={v}" for k, v in clean.items()))

            elif tool == "update_store_info":
                fields = a.get("fields") if isinstance(a.get("fields"), dict) else {}
                clean = {k: v for k, v in fields.items() if k in STORE_COPY_FIELDS}
                rejected = [k for k in fields if k not in STORE_COPY_FIELDS]
                if rejected:
                    errors.append("Ignored non-editable fields: " + ", ".join(rejected) + " (theme is set in the Appearance panel).")
                if not clean:
                    if not rejected:
                        errors.append("No editable store fields were provided.")
                    continue
                normalized.append({"tool": tool, "id": snapshot["store"].get("_id"), "fields": clean})
                diff.append("Update store info: " + ", ".join(f"{k}={_short(v)}" for k, v in clean.items()))

            elif tool == "create_item":
                name = str(a.get("name") or "").strip()
                if not name:
                    errors.append("An item needs a name.")
                    continue
                code = str(a.get("item_code") or "").strip() or _slug(name)
                existing_codes = {i.get("item_code") for i in snapshot["items"]}
                base_code, n = code, 2
                while code in existing_codes:
                    code = f"{base_code}-{n}"
                    n += 1
                doc = {"name": name, "item_code": code}
                price = _coerce_number(a.get("price"))
                if price is not None:
                    doc["price"] = price
                if a.get("category"):
                    doc["category"] = str(a["category"])
                if a.get("description"):
                    doc["description"] = str(a["description"])
                status = a.get("status")
                doc["status"] = status if status in ITEM_STATUS_ENUM else "Available"
                normalized.append({"tool": tool, "doc": doc})
                diff.append(f"Add item: {name}" + (f" (${price:g})" if price is not None else ""))

            elif tool == "update_item":
                it = _find_item(snapshot, a.get("selector"))
                if not it:
                    errors.append(f"No item matching '{a.get('selector')}'.")
                    continue
                raw = a.get("fields") if isinstance(a.get("fields"), dict) else {}
                fields = _validate_item_fields(raw, errors)
                if not fields:
                    continue
                normalized.append({"tool": tool, "id": it["_id"], "name": it["name"], "fields": fields})
                diff.append(f"Update item {it['name']}: " + ", ".join(f"{k}={_short(v)}" for k, v in fields.items()))

            elif tool == "set_item_status":
                it = _find_item(snapshot, a.get("selector"))
                if not it:
                    errors.append(f"No item matching '{a.get('selector')}'.")
                    continue
                status = a.get("status")
                if status not in ITEM_STATUS_ENUM:
                    errors.append(f"Invalid status '{status}'.")
                    continue
                normalized.append({"tool": tool, "id": it["_id"], "name": it["name"], "status": status})
                diff.append(f"Set {it['name']} status to {status}")

            elif tool == "create_special":
                title = str(a.get("title") or "").strip()
                content = str(a.get("content") or "").strip()
                if not title or not content:
                    errors.append("A special needs a title and content.")
                    continue
                doc = {"title": title, "content": content}
                if a.get("description"):
                    doc["description"] = str(a["description"])
                dp = _coerce_number(a.get("discount_percent"))
                if dp is not None:
                    doc["discount_percent"] = max(0.0, min(100.0, dp))
                if a.get("valid_until"):
                    doc["valid_until"] = str(a["valid_until"])
                normalized.append({"tool": tool, "doc": doc})
                diff.append(f"Add special: {title}")

            elif tool == "update_special":
                sp = _find_special(snapshot, a.get("selector"))
                if not sp:
                    errors.append(f"No special titled '{a.get('selector')}'.")
                    continue
                raw = a.get("fields") if isinstance(a.get("fields"), dict) else {}
                fields = _validate_special_fields(raw, errors)
                if not fields:
                    continue
                normalized.append({"tool": tool, "id": sp["_id"], "title": sp["title"], "fields": fields})
                diff.append(f"Update special {sp['title']}: " + ", ".join(f"{k}={_short(v)}" for k, v in fields.items()))

            else:
                errors.append(f"Unknown action '{tool}'.")
        except Exception as exc:  # noqa: BLE001 — never let one bad op crash validation
            logger.warning("op validation failed: %s", exc)
            errors.append(f"Could not process '{tool}'.")

        # An op is "valid" iff it produced a normalized op this iteration.
        if len(normalized) > _before:
            valid_raw.append(op)

    return normalized, diff, errors, valid_raw


def _validate_settings(incoming: dict, stype: str, manifest: dict, errors: list[str]) -> dict:
    spec = _section_settings_spec(manifest, stype)
    if not spec:
        # No registry entry: pass through simple scalar settings only.
        return {k: v for k, v in incoming.items() if isinstance(v, (str, int, float, bool))}
    clean: dict[str, Any] = {}
    for k, v in incoming.items():
        if k not in spec:
            errors.append(f"'{k}' is not a setting of the {stype} section.")
            continue
        field = spec[k] or {}
        ftype = field.get("type")
        if ftype == "integer":
            try:
                v = int(v)
            except (ValueError, TypeError):
                errors.append(f"Setting '{k}' must be a number.")
                continue
        elif ftype == "boolean":
            v = bool(v)
        elif ftype == "string":
            v = str(v)
        elif ftype == "array":
            if not isinstance(v, list):
                errors.append(f"Setting '{k}' must be a list.")
                continue
            v = [str(x) for x in v]
        if field.get("enum") and v not in field["enum"]:
            errors.append(f"Setting '{k}' must be one of {field['enum']}.")
            continue
        clean[k] = v
    return clean


def _validate_item_fields(raw: dict, errors: list[str]) -> dict:
    allowed = {"name", "price", "category", "description", "status", "image_url"}
    fields: dict[str, Any] = {}
    for k, v in raw.items():
        if k not in allowed:
            continue
        if k == "price":
            num = _coerce_number(v)
            if num is None:
                errors.append("Price must be a number.")
                continue
            fields["price"] = num
        elif k == "status":
            if v not in ITEM_STATUS_ENUM:
                errors.append(f"Invalid status '{v}'.")
                continue
            fields["status"] = v
        else:
            fields[k] = str(v)
    return fields


def _validate_special_fields(raw: dict, errors: list[str]) -> dict:
    allowed = {"title", "content", "description", "discount_percent", "valid_until", "image_url"}
    fields: dict[str, Any] = {}
    for k, v in raw.items():
        if k not in allowed:
            continue
        if k == "discount_percent":
            num = _coerce_number(v)
            if num is None:
                errors.append("Discount must be a number.")
                continue
            fields["discount_percent"] = max(0.0, min(100.0, num))
        else:
            fields[k] = str(v)
    return fields


def _compute_insert_order(snapshot: dict, position: str) -> int:
    orders = [s.get("order") or 0 for s in snapshot["sections"]]
    if not orders:
        return 1
    return (max(orders) + 1) if not str(position).startswith("start") else (min(orders) - 1)


def _sequence_with_insert(snapshot: dict, new_key: str, position: str) -> list[str]:
    seq = _ordered_keys(snapshot)
    position = str(position or "end")
    if position == "start":
        return [new_key, *seq]
    if position.startswith("after:"):
        ref = position.split(":", 1)[1]
        if ref in seq:
            i = seq.index(ref)
            return [*seq[: i + 1], new_key, *seq[i + 1:]]
    if position.startswith("before:"):
        ref = position.split(":", 1)[1]
        if ref in seq:
            i = seq.index(ref)
            return [*seq[:i], new_key, *seq[i:]]
    return [*seq, new_key]


def _short(v: Any, n: int = 40) -> str:
    s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    return s if len(s) <= n else s[: n - 1] + "…"


# ── Apply (executes confirmed ops) ─────────────────────────────────────────

def _oid(id_str: str):
    try:
        return ObjectId(id_str)
    except (InvalidId, TypeError):
        return None


async def apply_ops(db, ops: list[dict], user: dict | None) -> list[dict]:
    """Execute already-validated + normalized ops via the scoped DB."""
    results: list[dict] = []
    actor = (user or {}).get("email") or (user or {}).get("username") or "admin"

    async def audit(event: str, entity_id: Any, detail: dict) -> None:
        try:
            await db["audit_log"].insert_one({
                "event": event,
                "entity_id": str(entity_id) if entity_id is not None else None,
                "actor": actor,
                "source": "ai_editor",
                "detail": detail,
                "timestamp": datetime.now(timezone.utc),
            })
        except Exception as exc:  # noqa: BLE001 — audit is best-effort
            logger.warning("audit write failed: %s", exc)

    async def _renumber(order_keys: list[str]) -> None:
        for idx, k in enumerate(order_keys, start=1):
            await db["sections"].update_one({"key": k}, {"$set": {"order": idx}})

    for op in ops:
        tool = op["tool"]
        try:
            if tool == "add_section":
                doc = {**op["doc"], "created_at": datetime.now(timezone.utc)}
                res = await db["sections"].insert_one(doc)
                if op.get("resequence"):
                    await _renumber(op["resequence"])
                await audit("ai_section_add", res.inserted_id, {"key": doc["key"], "type": doc["type"]})
                results.append({"tool": tool, "ok": True, "id": str(res.inserted_id)})

            elif tool == "remove_section":
                oid = _oid(op["id"])
                await db["sections"].delete_one({"_id": oid} if oid else {"key": op["key"]})
                await audit("ai_section_remove", op["id"], {"key": op["key"]})
                results.append({"tool": tool, "ok": True})

            elif tool == "reorder_sections":
                await _renumber(op["order"])
                await audit("ai_section_reorder", None, {"order": op["order"]})
                results.append({"tool": tool, "ok": True})

            elif tool == "toggle_section":
                oid = _oid(op["id"])
                await db["sections"].update_one({"_id": oid} if oid else {"key": op["key"]}, {"$set": {"visible": op["visible"]}})
                await audit("ai_section_toggle", op["id"], {"key": op["key"], "visible": op["visible"]})
                results.append({"tool": tool, "ok": True})

            elif tool == "update_section_settings":
                oid = _oid(op["id"])
                await db["sections"].update_one({"_id": oid} if oid else {"key": op["key"]}, {"$set": {"settings": op["settings"]}})
                await audit("ai_section_settings", op["id"], {"key": op["key"]})
                results.append({"tool": tool, "ok": True})

            elif tool == "update_store_info":
                oid = _oid(op["id"])
                if not oid:
                    results.append({"tool": tool, "ok": False, "error": "store not found"})
                    continue
                await db["stores"].update_one({"_id": oid}, {"$set": op["fields"]})
                await audit("ai_store_update", op["id"], {"fields": list(op["fields"].keys())})
                results.append({"tool": tool, "ok": True})

            elif tool == "create_item":
                doc = {**op["doc"], "date_added": datetime.now(timezone.utc).isoformat(), "created_at": datetime.now(timezone.utc)}
                res = await db["items"].insert_one(doc)
                await audit("ai_item_create", res.inserted_id, {"item_code": doc["item_code"], "name": doc["name"]})
                results.append({"tool": tool, "ok": True, "id": str(res.inserted_id)})

            elif tool == "update_item":
                oid = _oid(op["id"])
                await db["items"].update_one({"_id": oid}, {"$set": op["fields"]})
                await audit("ai_item_update", op["id"], {"name": op.get("name"), "fields": list(op["fields"].keys())})
                results.append({"tool": tool, "ok": True})

            elif tool == "set_item_status":
                oid = _oid(op["id"])
                await db["items"].update_one({"_id": oid}, {"$set": {"status": op["status"]}})
                await audit("ai_item_status", op["id"], {"name": op.get("name"), "status": op["status"]})
                results.append({"tool": tool, "ok": True})

            elif tool == "create_special":
                doc = {**op["doc"], "date_created": datetime.now(timezone.utc).isoformat(), "created_at": datetime.now(timezone.utc)}
                res = await db["specials"].insert_one(doc)
                await audit("ai_special_create", res.inserted_id, {"title": doc["title"]})
                results.append({"tool": tool, "ok": True, "id": str(res.inserted_id)})

            elif tool == "update_special":
                oid = _oid(op["id"])
                await db["specials"].update_one({"_id": oid}, {"$set": op["fields"]})
                await audit("ai_special_update", op["id"], {"title": op.get("title"), "fields": list(op["fields"].keys())})
                results.append({"tool": tool, "ok": True})

            else:
                results.append({"tool": tool, "ok": False, "error": "unknown op"})
        except Exception as exc:  # noqa: BLE001
            logger.warning("apply failed for %s: %s", tool, exc)
            results.append({"tool": tool, "ok": False, "error": str(exc)})

    return results
