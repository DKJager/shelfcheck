import os
import sys
import json
import base64

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from shelfcheck import youcam, app


def test_shop_route_banner_and_buttons():
    event = {"httpMethod": "GET", "path": "/shop"}
    resp = app.lambda_handler(event, None)
    assert resp.get("statusCode") == 200
    assert "text/html" in resp.get("headers", {}).get("Content-Type", "")
    body = resp.get("body", "")
    assert "FICTIONAL PRODUCTS ONLY" in body
    assert "Check fit for my skin" in body
    assert "Compare two" in body


def test_products_json_three_fictional():
    event = {"httpMethod": "GET", "path": "/products.json"}
    resp = app.lambda_handler(event, None)
    assert resp.get("statusCode") == 200
    products = json.loads(resp.get("body", "[]"))
    assert len(products) == 3
    for p in products:
        assert p.get("name")
        assert p.get("ingredients")
        assert isinstance(p["ingredients"], list)
        assert len(p["ingredients"]) > 0


def test_check_timing_additive():
    # Save original
    orig_make_ask_fns = app.make_ask_fns

    # Fake judges: all valid
    def fake_make_ask_fns_all_valid():
        return {
            "j1": lambda msgs: json.dumps({"verdict": "good"}),
            "j2": lambda msgs: json.dumps({"verdict": "good"}),
            "j3": lambda msgs: json.dumps({"verdict": "good"}),
        }

    app.make_ask_fns = fake_make_ask_fns_all_valid
    try:
        check_event = {
            "httpMethod": "POST",
            "path": "/check",
            "body": json.dumps({
                "skin": {"acne": {"ui_score": 80, "raw_score": 70.0}},
                "product_name": "Sample",
                "ingredients": "Water, Glycerin"
            }),
        }
        resp = app.lambda_handler(check_event, None)
        assert resp.get("statusCode") == 200
        body = json.loads(resp.get("body", "{}"))
        assert "verdict" in body
        assert "agreement" in body
        assert "judges" in body
        assert "per_concern" in body
        assert "dissent" in body
        assert "timing" in body
        timing = body["timing"]
        assert timing["responded"] == 3
        assert timing["total"] == 3
        assert timing["seconds"] >= 0
    finally:
        app.make_ask_fns = orig_make_ask_fns

    # Fake judges: one invalid
    def fake_make_ask_fns_one_invalid():
        return {
            "j1": lambda msgs: json.dumps({"verdict": "good"}),
            "j2": lambda msgs: "not json",
            "j3": lambda msgs: json.dumps({"verdict": "good"}),
        }

    app.make_ask_fns = fake_make_ask_fns_one_invalid
    try:
        check_event = {
            "httpMethod": "POST",
            "path": "/check",
            "body": json.dumps({
                "skin": {"acne": {"ui_score": 80, "raw_score": 70.0}},
                "product_name": "Sample",
                "ingredients": "Water, Glycerin"
            }),
        }
        resp = app.lambda_handler(check_event, None)
        assert resp.get("statusCode") == 200
        body = json.loads(resp.get("body", "{}"))
        assert "timing" in body
        timing = body["timing"]
        assert timing["responded"] == 2
        assert timing["total"] == 3
        assert timing["seconds"] >= 0
    finally:
        app.make_ask_fns = orig_make_ask_fns


def test_no_storage_no_cart():
    event = {"httpMethod": "GET", "path": "/shop"}
    resp = app.lambda_handler(event, None)
    assert resp.get("statusCode") == 200
    body = resp.get("body", "")
    forbidden = ["localStorage", "sessionStorage", "document.cookie", "checkout", "Add to cart", "testimonial"]
    for term in forbidden:
        assert term not in body, f"shop.html must not contain '{term}'"
    # BINDING: source must join ingredients with ", " before POST /check
    assert 'join(", ")' in body or "join(', ')" in body
    _src = app.lambda_handler({"httpMethod": "GET", "path": "/shop"}, None)["body"]
    assert 'join(", ")' in _src or "join(', ')" in _src, "shop.html must send ingredients as one comma-separated string"

def test_shop_happy_path_with_fakes():
    # Save originals
    orig_youcam = youcam.YouCam
    orig_make_ask_fns = app.make_ask_fns

    # Fake YouCam
    class FakeYouCam:
        def __init__(self, *a, **kw):
            pass
        def analyze(self, *a, **kw):
            return {
                "acne": {"ui_score": 80, "raw_score": 70.0},
                # other skin attributes can be minimal
            }

    youcam.YouCam = FakeYouCam

    # Fake judges
    def fake_make_ask_fns():
        return {
            "j1": lambda msgs: json.dumps({"verdict": "good"}),
            "j2": lambda msgs: json.dumps({"verdict": "good"}),
            "j3": lambda msgs: json.dumps({"verdict": "good"}),
        }

    app.make_ask_fns = fake_make_ask_fns

    try:
        # GET /shop
        get_event = {"httpMethod": "GET", "path": "/shop"}
        resp_get = app.lambda_handler(get_event, None)
        assert resp_get.get("statusCode") == 200

        # GET /products.json
        products_event = {"httpMethod": "GET", "path": "/products.json"}
        resp_products = app.lambda_handler(products_event, None)
        assert resp_products.get("statusCode") == 200
        products = json.loads(resp_products.get("body", "[]"))
        assert len(products) >= 1
        first_product = products[0]
        ingredients_str = ", ".join(first_product["ingredients"])

        # POST /analyze
        analyze_event = {
            "httpMethod": "POST",
            "path": "/analyze",
            "body": json.dumps({"image_b64": base64.b64encode(b"fakeimg").decode()}),
        }
        resp_analyze = app.lambda_handler(analyze_event, None)
        assert resp_analyze.get("statusCode") == 200
        body_analyze = json.loads(resp_analyze.get("body", "{}"))
        assert "skin" in body_analyze
        assert body_analyze["skin"]["acne"]["ui_score"] == 80

        # POST /check
        check_event = {
            "httpMethod": "POST",
            "path": "/check",
            "body": json.dumps({
                "skin": body_analyze["skin"],
                "product_name": first_product["name"],
                "ingredients": ingredients_str
            }),
        }
        resp_check = app.lambda_handler(check_event, None)
        assert resp_check.get("statusCode") == 200
        check_body = json.loads(resp_check.get("body", "{}"))
        assert "verdict" in check_body
        assert check_body["verdict"] == "good"
        assert "judges" in check_body
    finally:
        youcam.YouCam = orig_youcam
        app.make_ask_fns = orig_make_ask_fns
