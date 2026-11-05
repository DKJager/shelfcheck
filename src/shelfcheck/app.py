# -*- coding: utf-8 -*-
"""AWS Lambda entry point for the Shelf Check application.

Implements three HTTP routes (GET ``/``, POST ``/analyze`` and POST ``/check``)
using a minimal, dependency‑free request dispatcher. All heavy lifting is
delegated to :class:`shelfcheck.youcam.YouCam` and the panel utilities.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List

# Add src directory to Python path to ensure shelfcheck modules are importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

# --------------------------------------------------------------------------- #
# Helper utilities
# --------------------------------------------------------------------------- #

def _json_response(body: Any, status: int = 200) -> Dict[str, Any]:
    """Return a Lambda‑proxy compatible JSON response."""
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False),
    }

def _html_response(html: str, status: int = 200) -> Dict[str, Any]:
    """Return a Lambda‑proxy compatible HTML response."""
    return {
        "statusCode": status,
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "body": html,
    }

def _error(message: str, status: int) -> Dict[str, Any]:
    """Convenient wrapper for JSON error responses."""
    return _json_response({"error": message}, status)

# --------------------------------------------------------------------------- #
# NEBIUS model request factory
# --------------------------------------------------------------------------- #

def make_ask_fns() -> Dict[str, Callable[[List[Dict[str, str]]], str]]:
    """Create a mapping ``judge → callable(messages)`` that invokes the NEBIUS API.

    The environment variable ``NEBIUS_API_KEY`` must be set. The function builds
    a small synchronous request using :mod:`urllib.request`. Each callable
    returns the raw ``content`` string from the first choice of the API response.
    """
    api_key = os.getenv("NEBIUS_API_KEY")
    if not api_key:
        raise RuntimeError("NEBIUS_API_KEY environment variable not set")

    endpoint = "https://api.tokenfactory.nebius.com/v1/chat/completions"
    judges = {
        "qwen3-30b": "Qwen/Qwen3-30B-A3B-Instruct-2507",
        "deepseek-v4.1-flash": "deepseek-ai/DeepSeek-V4.1-Flash-1017",
        "gpt-oss-120b": "openai/gpt-oss-120b",
    }

    def _make_caller(model_name: str) -> Callable[[List[Dict[str, str]]], str]:
        def _call(messages: List[Dict[str, str]]) -> str:
            payload = {
                "model": model_name,
                "messages": messages,
                "max_tokens": 6000,
                "temperature": 0,
            }
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                endpoint,
                data=data,
                method="POST",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=100) as resp:
                resp_data = json.load(resp)
            try:
                choice = resp_data["choices"][0]["message"]
                return choice.get("content") or choice.get("reasoning_content") or ""
            except Exception as exc:
                raise RuntimeError(f"Invalid response from NEBIUS API: {exc}") from exc

        return _call

    return {judge: _make_caller(model) for judge, model in judges.items()}

# --------------------------------------------------------------------------- #
# Lazy import helper for internal modules
# --------------------------------------------------------------------------- #

def _load_internal_modules() -> tuple[Any, Any]:
    """Import the internal panel and youcam modules."""
    import importlib

    try:
        panel_mod = importlib.import_module("shelfcheck.panel")
        youcam_mod = importlib.import_module("shelfcheck.youcam")
        return panel_mod, youcam_mod
    except ImportError as e:
        raise RuntimeError(f"Cannot import required module: {e}") from e

# --------------------------------------------------------------------------- #
# Lambda handler
# --------------------------------------------------------------------------- #

def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """AWS Lambda handler.

    Routes the incoming API-Gateway/Lambda‑Function‑URL request to the appropriate
    logic and returns a response dict compatible with Lambda proxy integration.
    Errors are captured and returned as JSON ``{"error": "msg"}`` with a suitable
    HTTP status code; stack traces are never exposed.
    """
    try:
        http = (event.get("requestContext") or {}).get("http") or {}   # Lambda function URL (payload v2)
        method = (event.get("httpMethod") or http.get("method") or "").upper()
        path = event.get("path") or event.get("rawPath") or http.get("path") or "/"

        # --------------------------------------------------------------- #
        # GET / and static assets
        # --------------------------------------------------------------- #
        if method == "GET" and path == "/":
            index_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
            try:
                with open(index_path, "r", encoding="utf-8") as f:
                    html = f.read()
                return _html_response(html)
            except FileNotFoundError:
                return _error("Static index.html not found", 500)

        if method == "GET" and path == "/demo-face.jpg":   # synthetic demo face (AI-generated, no real person)
            fp = os.path.join(os.path.dirname(__file__), "static", "demo-face.jpg")
            with open(fp, "rb") as f:
                img = base64.b64encode(f.read()).decode("ascii")
            return {"statusCode": 200, "headers": {"Content-Type": "image/jpeg", "Cache-Control": "max-age=3600"},
                    "body": img, "isBase64Encoded": True}

        if method == "GET" and path == "/shop":
            shop_path = os.path.join(os.path.dirname(__file__), "static", "shop.html")
            try:
                with open(shop_path, "r", encoding="utf-8") as f:
                    html = f.read()
                return _html_response(html)
            except FileNotFoundError:
                return _error("Static shop.html not found", 500)

        if method == "GET" and path == "/products.json":
            json_path = os.path.join(os.path.dirname(__file__), "static", "products.json")
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return _json_response(data)
            except FileNotFoundError:
                return _error("Static products.json not found", 500)
            except json.JSONDecodeError:
                return _error("Invalid JSON in products.json", 500)

        # --------------------------------------------------------------- #
        # POST /analyze
        # --------------------------------------------------------------- #
        if method == "POST" and path == "/analyze":
            raw_body = event.get("body", "")
            if event.get("isBase64Encoded"):
                try:
                    raw_body = base64.b64decode(raw_body).decode("utf-8")
                except Exception:
                    return _error("Invalid base64 payload", 400)

            try:
                data = json.loads(raw_body)
            except json.JSONDecodeError:
                return _error("Invalid JSON payload", 400)

            image_b64 = data.get("image_b64")
            if not isinstance(image_b64, str):
                return _error("Missing or invalid 'image_b64' field", 400)

            try:
                image_bytes = base64.b64decode(image_b64, validate=True)
            except Exception:
                return _error("Invalid base64 image data", 400)

            if len(image_bytes) > 8 * 1024 * 1024:
                return _error("Image exceeds 8 MiB size limit", 413)

            api_key = os.getenv("YOUCAM_API_KEY", "")

            try:
                _, youcam_mod = _load_internal_modules()
            except RuntimeError as e:
                return _error(str(e), 500)

            client = youcam_mod.YouCam(api_key)
            try:
                skin = client.analyze(image_bytes)
            except youcam_mod.YouCamError as exc:
                return _error(str(exc), 502)
            except Exception:
                return _error("Unexpected error during analysis", 500)

            return _json_response({"skin": skin})

        # --------------------------------------------------------------- #
        # POST /check
        # --------------------------------------------------------------- #
        if method == "POST" and path == "/check":
            start_time = time.time()
            raw_body = event.get("body", "")
            if event.get("isBase64Encoded"):
                try:
                    raw_body = base64.b64decode(raw_body).decode("utf-8")
                except Exception:
                    return _error("Invalid base64 payload", 400)

            try:
                data = json.loads(raw_body)
            except json.JSONDecodeError:
                return _error("Invalid JSON payload", 400)

            required_keys = {"skin", "product_name", "ingredients"}
            if not required_keys.issubset(data):
                missing = required_keys - set(data.keys())
                return _error(f"Missing required fields: {missing}", 400)

            skin = data["skin"]
            product_name = data["product_name"]
            ingredients = data["ingredients"]

            if not isinstance(skin, dict) or not isinstance(product_name, str) or not isinstance(ingredients, str):
                return _error("Invalid field types", 400)

            try:
                panel_mod, _ = _load_internal_modules()
            except RuntimeError as e:
                return _error(str(e), 500)

            messages = panel_mod.build_messages(skin, product_name, ingredients)

            try:
                ask_fns = make_ask_fns()
            except Exception as exc:
                return _error(f"Model configuration error: {str(exc)}", 500)

            raw_outputs: Dict[str, str] = {}
            with ThreadPoolExecutor(max_workers=len(ask_fns)) as executor:
                future_to_judge = {
                    executor.submit(fn, messages): judge for judge, fn in ask_fns.items()
                }
                for future in as_completed(future_to_judge):
                    judge = future_to_judge[future]
                    try:
                        raw_outputs[judge] = future.result()
                    except Exception:
                        raw_outputs[judge] = ""

            parsed: Dict[str, Any | None] = {}
            for judge, text in raw_outputs.items():
                try:
                    parsed[judge] = panel_mod.parse(text)
                except Exception:
                    parsed[judge] = None

            try:
                consensus = panel_mod.reconcile(parsed)
            except ValueError as exc:
                return _error(str(exc), 400)
            except Exception:
                return _error("Reconciliation error", 500)

            # Include raw parsed answers for each judge
            consensus["answers"] = parsed

            # Count judges that returned parsable output
            responded = sum(1 for v in parsed.values() if v is not None)
            total = len(ask_fns)
            elapsed = time.time() - start_time

            # Add timing field as required
            consensus["timing"] = {
                "responded": responded,
                "total": total,
                "seconds": elapsed
            }

            return _json_response(consensus)

        # --------------------------------------------------------------- #
        # Unsupported route
        # --------------------------------------------------------------- #
        return _error("Not found", 404)

    except Exception:
        return _error("Internal server error", 500)
