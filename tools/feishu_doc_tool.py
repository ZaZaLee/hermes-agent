"""Feishu/Lark document reading tool.

Supports two execution modes:

1. Generic chat/tool use: accepts a Feishu/Lark wiki/doc/docx URL, exchanges
   ``FEISHU_APP_ID`` + ``FEISHU_APP_SECRET`` for a tenant access token, then
   reads the document via OpenAPI.
2. Feishu comment workflow: reuses the thread-local lark client injected by
   ``gateway.platforms.feishu_comment`` so comment replies can read nearby
   context without needing env-based auth inside the tool call itself.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, quote, urlparse

import httpx

from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

_local = threading.local()

_FEISHU_OPEN_BASES = {
    "feishu": "https://open.feishu.cn",
    "lark": "https://open.larksuite.com",
}
_TOKEN_REFRESH_SKEW_SECONDS = 60
_DEFAULT_TIMEOUT_SECONDS = 20.0
_DEFAULT_MAX_CHARS = 20_000
_DOC_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,}$")
_DOCX_RAW_CONTENT_URI = "/open-apis/docx/v1/documents/:document_id/raw_content"


@dataclass(frozen=True)
class FeishuDocRef:
    source_kind: str
    token: str
    domain: str
    original_url: str = ""


_TOKEN_CACHE: Dict[Tuple[str, str], Tuple[str, float]] = {}


def set_client(client):
    """Store a lark client for the current thread (called by feishu_comment)."""
    _local.client = client


def get_client():
    """Return the lark client for the current thread, or None."""
    return getattr(_local, "client", None)


def _normalize_domain(domain: str) -> str:
    normalized = (domain or "").strip().lower()
    return "lark" if normalized == "lark" else "feishu"


def _open_api_base(domain: str) -> str:
    return _FEISHU_OPEN_BASES[_normalize_domain(domain)]


def _resolve_domain(url: str = "") -> str:
    parsed = urlparse(url or "")
    host = (parsed.netloc or "").lower()
    if "larksuite" in host or "larkoffice" in host:
        return "lark"

    env_domain = os.getenv("FEISHU_DOMAIN", "").strip()
    if env_domain:
        return _normalize_domain(env_domain)
    return "feishu"


def _trim_content(content: str, max_chars: int) -> tuple[str, bool]:
    if max_chars <= 0 or len(content) <= max_chars:
        return content, False
    return (
        content[:max_chars].rstrip()
        + f"\n\n[Content truncated: showing first {max_chars:,} characters.]",
        True,
    )


def _extract_token_from_query(parsed) -> Optional[FeishuDocRef]:
    query = parse_qs(parsed.query or "")

    wiki_token = (query.get("token") or [None])[0]
    if parsed.path.rstrip("/").endswith("/wiki") and wiki_token:
        return FeishuDocRef(
            source_kind="wiki",
            token=wiki_token,
            domain=_resolve_domain(parsed.geturl()),
            original_url=parsed.geturl(),
        )

    doc_token = (query.get("doc_token") or query.get("document_id") or [None])[0]
    if doc_token:
        return FeishuDocRef(
            source_kind="docx",
            token=doc_token,
            domain=_resolve_domain(parsed.geturl()),
            original_url=parsed.geturl(),
        )

    return None


def parse_feishu_doc_ref(
    url_or_token: str,
    *,
    doc_type: str = "",
    domain: str = "",
) -> FeishuDocRef:
    """Parse a Feishu/Lark wiki/doc/docx link into a structured reference."""
    raw = (url_or_token or "").strip()
    normalized_doc_type = (doc_type or "").strip().lower()
    resolved_domain = _normalize_domain(domain) if domain else _resolve_domain(raw)

    if not raw:
        raise ValueError("A Feishu document URL or token is required")

    if raw.startswith(("http://", "https://")):
        parsed = urlparse(raw)
        path_parts = [part for part in parsed.path.split("/") if part]
        url_domain = _resolve_domain(raw)

        for idx, part in enumerate(path_parts):
            part_lower = part.lower()
            if part_lower in {"wiki", "docx", "docs", "doc"} and idx + 1 < len(path_parts):
                token = path_parts[idx + 1]
                if token:
                    source_kind = "doc" if part_lower in {"docs", "doc"} else part_lower
                    return FeishuDocRef(
                        source_kind=source_kind,
                        token=token,
                        domain=url_domain,
                        original_url=raw,
                    )

        from_query = _extract_token_from_query(parsed)
        if from_query:
            return from_query

        raise ValueError(
            "Unsupported Feishu document URL. Expected a /wiki/, /docx/, or /doc/ link."
        )

    if _DOC_TOKEN_RE.fullmatch(raw):
        token_kind = normalized_doc_type or "docx"
        if token_kind not in {"wiki", "docx", "doc"}:
            raise ValueError("doc_type must be one of: wiki, docx, doc")
        return FeishuDocRef(
            source_kind=token_kind,
            token=raw,
            domain=resolved_domain,
            original_url="",
        )

    raise ValueError("Unsupported Feishu document reference. Use a doc/wiki URL or token.")


def _http_request(
    *,
    method: str,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    with httpx.Client(timeout=timeout) as client:
        response = client.request(
            method=method,
            url=url,
            headers=headers,
            json=json_body,
            params=params,
        )

    try:
        payload = response.json()
    except ValueError as exc:
        text_preview = response.text[:300].strip()
        raise ValueError(
            f"Feishu API returned a non-JSON response (HTTP {response.status_code}): {text_preview}"
        ) from exc

    if response.status_code >= 400:
        message = ""
        if isinstance(payload, dict):
            message = (
                str(payload.get("msg", "")).strip()
                or str(payload.get("message", "")).strip()
                or str(payload.get("error", "")).strip()
            )
        raise ValueError(f"Feishu API HTTP {response.status_code}: {message or payload}")

    if isinstance(payload, dict) and payload.get("code") not in (None, 0):
        code = payload.get("code")
        msg = payload.get("msg") or payload.get("message") or "unknown error"
        raise ValueError(f"Feishu API error {code}: {msg}")

    return payload


def _get_tenant_access_token(domain: str) -> str:
    app_id = os.getenv("FEISHU_APP_ID", "").strip()
    app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        raise ValueError("FEISHU_APP_ID and FEISHU_APP_SECRET must both be set")

    cache_key = (_normalize_domain(domain), app_id)
    cached = _TOKEN_CACHE.get(cache_key)
    now = time.time()
    if cached and cached[1] > now:
        return cached[0]

    payload = _http_request(
        method="POST",
        url=f"{_open_api_base(domain)}/open-apis/auth/v3/tenant_access_token/internal",
        headers={"Content-Type": "application/json"},
        json_body={"app_id": app_id, "app_secret": app_secret},
    )

    token = str(payload.get("tenant_access_token", "")).strip()
    if not token:
        raise ValueError("Feishu auth succeeded but no tenant_access_token was returned")

    expire = int(payload.get("expire", 7200) or 7200)
    _TOKEN_CACHE[cache_key] = (
        token,
        now + max(60, expire - _TOKEN_REFRESH_SKEW_SECONDS),
    )
    return token


def _authorized_headers(domain: str) -> Dict[str, str]:
    token = _get_tenant_access_token(domain)
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }


def _extract_data(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _resolve_wiki_node(ref: FeishuDocRef) -> tuple[FeishuDocRef, Dict[str, Any]]:
    payload = _http_request(
        method="GET",
        url=f"{_open_api_base(ref.domain)}/open-apis/wiki/v2/spaces/get_node",
        headers=_authorized_headers(ref.domain),
        params={"token": ref.token},
    )
    data = _extract_data(payload)
    node = data.get("node") if isinstance(data.get("node"), dict) else data

    obj_type = str(node.get("obj_type") or node.get("type") or "").strip().lower()
    obj_token = str(node.get("obj_token") or node.get("token") or "").strip()
    if not obj_type or not obj_token:
        raise ValueError("Wiki node resolved, but no target document type/token was returned")

    if obj_type not in {"docx", "doc"}:
        raise ValueError(
            f"Wiki node points to unsupported content type '{obj_type}'. "
            "This version supports docx and doc documents."
        )

    resolved = FeishuDocRef(
        source_kind=obj_type,
        token=obj_token,
        domain=ref.domain,
        original_url=ref.original_url,
    )
    return resolved, node


def _extract_content_from_data(data: Dict[str, Any], empty_message: str) -> Dict[str, str]:
    content = (
        str(data.get("content") or "").strip()
        or str(data.get("raw_content") or "").strip()
        or str(data.get("text") or "").strip()
    )
    if not content:
        raise ValueError(empty_message)
    return {
        "title": str(data.get("title") or "").strip(),
        "content": content,
    }


def _fetch_docx_content_via_openapi(ref: FeishuDocRef) -> Dict[str, str]:
    payload = _http_request(
        method="GET",
        url=(
            f"{_open_api_base(ref.domain)}/open-apis/docx/v1/documents/"
            f"{quote(ref.token, safe='')}/raw_content"
        ),
        headers=_authorized_headers(ref.domain),
    )
    return _extract_content_from_data(
        _extract_data(payload),
        "Feishu docx API returned no raw content",
    )


def _fetch_doc_content_via_openapi(ref: FeishuDocRef) -> Dict[str, str]:
    payload = _http_request(
        method="GET",
        url=(
            f"{_open_api_base(ref.domain)}/open-apis/doc/v2/"
            f"{quote(ref.token, safe='')}/raw_content"
        ),
        headers=_authorized_headers(ref.domain),
    )
    return _extract_content_from_data(
        _extract_data(payload),
        "Feishu doc API returned no raw content",
    )


def _fetch_docx_content_via_client(doc_token: str) -> Dict[str, str]:
    client = get_client()
    if client is None:
        raise ValueError("Feishu client not available")

    try:
        from lark_oapi import AccessTokenType
        from lark_oapi.core.enum import HttpMethod
        from lark_oapi.core.model.base_request import BaseRequest
    except ImportError as exc:
        raise ValueError("lark_oapi not installed") from exc

    request = (
        BaseRequest.builder()
        .http_method(HttpMethod.GET)
        .uri(_DOCX_RAW_CONTENT_URI)
        .token_types({AccessTokenType.TENANT})
        .paths({"document_id": doc_token})
        .build()
    )
    response = client.request(request)

    code = getattr(response, "code", None)
    if code != 0:
        msg = getattr(response, "msg", "unknown error")
        raise ValueError(f"Failed to read document: code={code} msg={msg}")

    raw = getattr(response, "raw", None)
    if raw and hasattr(raw, "content"):
        try:
            body = json.loads(raw.content)
            return _extract_content_from_data(
                body.get("data", {}),
                "Feishu docx API returned no raw content",
            )
        except (json.JSONDecodeError, AttributeError):
            pass

    data = getattr(response, "data", None)
    if isinstance(data, dict):
        return _extract_content_from_data(data, "Feishu docx API returned no raw content")
    if data is not None:
        content = getattr(data, "content", "") or str(data)
        title = getattr(data, "title", "")
        if str(content).strip():
            return {"title": str(title).strip(), "content": str(content).strip()}

    raise ValueError("No content returned from document API")


def _permission_hint(exc: Exception) -> str:
    text = str(exc).lower()
    if any(token in text for token in ("forbidden", "permission", "unauthorized", "99991663", "91403")):
        return (
            "The app can authenticate, but Feishu denied document access. "
            "Confirm the application has doc/wiki scopes and that the document "
            "is readable by the application's tenant identity."
        )
    if "redirect" in text or "login" in text:
        return "This looks like an anonymous web fetch. Use app credentials or the Feishu comment integration."
    return ""


def _read_ref_content(ref: FeishuDocRef) -> tuple[Dict[str, str], Dict[str, Any]]:
    wiki_info: Dict[str, Any] = {}
    effective_ref = ref

    if effective_ref.source_kind == "wiki":
        effective_ref, wiki_info = _resolve_wiki_node(effective_ref)

    if effective_ref.source_kind == "docx":
        try:
            fetched = _fetch_docx_content_via_openapi(effective_ref)
        except Exception:
            if get_client() is None:
                raise
            fetched = _fetch_docx_content_via_client(effective_ref.token)
    elif effective_ref.source_kind == "doc":
        fetched = _fetch_doc_content_via_openapi(effective_ref)
    else:
        raise ValueError(
            f"Unsupported Feishu document type '{effective_ref.source_kind}'. "
            "Use a wiki/doc/docx link."
        )

    return {
        "source_type": "wiki" if wiki_info else effective_ref.source_kind,
        "resolved_type": effective_ref.source_kind,
        "domain": effective_ref.domain,
        "title": fetched.get("title") or str(wiki_info.get("title") or "").strip(),
        "content": fetched["content"],
        "token": effective_ref.token,
    }, wiki_info


def check_feishu_doc_requirements() -> bool:
    have_env_auth = bool(
        os.getenv("FEISHU_APP_ID", "").strip()
        and os.getenv("FEISHU_APP_SECRET", "").strip()
    )
    if have_env_auth:
        return True
    try:
        import lark_oapi  # noqa: F401
        return True
    except ImportError:
        return False


def feishu_doc_read_tool(
    *,
    url: str = "",
    doc_token: str = "",
    doc_type: str = "",
    max_chars: int = _DEFAULT_MAX_CHARS,
) -> str:
    """Read a private Feishu/Lark cloud document with app identity."""
    try:
        raw_ref = (url or doc_token or "").strip()
        if not raw_ref:
            return tool_error("url or doc_token is required")

        ref = parse_feishu_doc_ref(
            raw_ref,
            doc_type=doc_type,
            domain=_resolve_domain(url or ""),
        )
        fetched, wiki_info = _read_ref_content(ref)
        trimmed_content, truncated = _trim_content(
            fetched["content"],
            int(max_chars or 0),
        )

        response = {
            "success": True,
            "source_type": fetched["source_type"],
            "resolved_type": fetched["resolved_type"],
            "domain": fetched["domain"],
            "title": fetched["title"],
            "content": trimmed_content,
            "truncated": truncated,
            "token": fetched["token"],
            "original_url": url or ref.original_url,
        }
        if wiki_info:
            response["wiki_title"] = str(wiki_info.get("title") or "").strip()
        return tool_result(response)
    except Exception as exc:
        logger.error("Feishu document read failed: %s", exc, exc_info=True)
        extra = _permission_hint(exc)
        return tool_error(
            str(exc),
            success=False,
            hint=extra or None,
        )


FEISHU_DOC_READ_SCHEMA = {
    "name": "feishu_doc_read",
    "description": (
        "Read the text content of a private Feishu/Lark cloud document using the app's "
        "tenant_access_token instead of anonymous web scraping. Use this for Feishu "
        "wiki/doc/docx links that would otherwise redirect to the login page. In "
        "Feishu comment workflows, you can also pass doc_token directly."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "A Feishu/Lark wiki, docx, or doc URL.",
            },
            "doc_token": {
                "type": "string",
                "description": "A raw document token when you already have it from comment context.",
            },
            "doc_type": {
                "type": "string",
                "enum": ["wiki", "docx", "doc"],
                "description": "Optional type hint when providing doc_token directly.",
            },
            "max_chars": {
                "type": "integer",
                "description": "Maximum characters to return from the document content.",
                "default": _DEFAULT_MAX_CHARS,
            },
        },
        "anyOf": [
            {"required": ["url"]},
            {"required": ["doc_token"]},
        ],
    },
}


def _handle_feishu_doc_read(args: Dict[str, Any], **_: Any) -> str:
    return feishu_doc_read_tool(
        url=str(args.get("url") or "").strip(),
        doc_token=str(args.get("doc_token") or "").strip(),
        doc_type=str(args.get("doc_type") or "").strip(),
        max_chars=int(args.get("max_chars") or _DEFAULT_MAX_CHARS),
    )


registry.register(
    name="feishu_doc_read",
    toolset="feishu_doc",
    schema=FEISHU_DOC_READ_SCHEMA,
    handler=_handle_feishu_doc_read,
    check_fn=check_feishu_doc_requirements,
    requires_env=["FEISHU_APP_ID", "FEISHU_APP_SECRET"],
    is_async=False,
    description="Read Feishu document content",
    emoji="📄",
)
