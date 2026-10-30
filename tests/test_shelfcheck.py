import sys, os, json, base64, time, urllib.request
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from shelfcheck import youcam, panel, app

# ---------- Helper classes for fakes ----------
class _FakeResponse:
    def __init__(self, data_bytes: bytes):
        self._data = data_bytes

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

# ---------- Test 1 ----------
def test_youcam_analyze_flow_and_parse():
    """Validate the multipart YouCam flow and the final parsing."""
    requests = []

    # state for polling
    poll_counter = {"count": 0}

    def fake_opener(req: urllib.request.Request):
        # record request details
        method = req.get_method()
        url = req.full_url
        headers = dict(req.header_items())
        requests.append((method, url, headers))

        # step 1: request file upload presigned URL
        if url.endswith("/file/skin-analysis") and method == "POST":
            payload = {
                "data": {
                    "files": [
                        {"file_id": "fid123", "url": "https://presigned.example/upload"}
                    ]
                }
            }
            return _FakeResponse(json.dumps(payload).encode())

        # step 2: PUT to presigned upload URL
        if url.startswith("https://presigned.example/upload") and method == "PUT":
            return _FakeResponse(b"")

        # step 3: create task
        if url.endswith("/task/skin-analysis") and method == "POST":
            payload = {"data": {"task_id": "tid456"}}
            return _FakeResponse(json.dumps(payload).encode())

        # step 4: polling GET
        if url.endswith("/task/skin-analysis/tid456") and method == "GET":
            if poll_counter["count"] == 0:
                poll_counter["count"] += 1
                payload = {"data": {"task_status": "running"}}
            else:
                payload = {
                    "data": {
                        "task_status": "success",
                        "results": {
                            "output": [
                                {"type": "acne", "ui_score": 80, "raw_score": 70.1},
                                {"type": "pore", "ui_score": 55, "raw_score": 40.0},
                            ]
                        }
                    }
                }
            return _FakeResponse(json.dumps(payload).encode())

        raise AssertionError(f"Unexpected request: {method} {url}")

    sleep_calls = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)

    yc = youcam.YouCam(
        api_key="k",
        opener=fake_opener,
        sleep=fake_sleep,
    )
    result = yc.analyze(b"dummybytes")

    # ---- assertions ----
    assert isinstance(result, dict), "Result should be a dict"
    expected = {
        "acne": {"ui_score": 80, "raw_score": 70.1},
        "pore": {"ui_score": 55, "raw_score": 40.0},
    }
    assert result == expected, f"Unexpected analyze output: {result}"
    assert [r[0] for r in requests] == ["POST", "PUT", "POST", "GET", "GET"], "POST file, PUT, POST task, two GET polls"
    auth_header = requests[0][2].get("Authorization")
    assert auth_header == "Bearer k", f"Authorization header wrong: {auth_header}"
    assert sleep_calls, "Fake sleep should have been called at least once"

# ---------- Test 2 ----------
def test_youcam_error_raises():
    """When the task finishes with error status YouCamError must be raised."""
    call_phase = {"stage": 0}

    def fake_opener(req: urllib.request.Request):
        method = req.get_method()
        url = req.full_url

        if url.endswith("/file/skin-analysis") and method == "POST":
            payload = {
                "data": {
                    "files": [
                        {"file_id": "fidX", "url": "https://presigned.example/uploadX"}
                    ]
                }
            }
            return _FakeResponse(json.dumps(payload).encode())

        if url.startswith("https://presigned.example/uploadX") and method == "PUT":
            return _FakeResponse(b"")

        if url.endswith("/task/skin-analysis") and method == "POST":
            payload = {"data": {"task_id": "tidX"}}
            return _FakeResponse(json.dumps(payload).encode())

        if url.endswith("/task/skin-analysis/tidX") and method == "GET":
            payload = {
                "data": {
                    "task_status": "error",
                    "error": "error_src_face_too_small",
                    "error_message": "Face is too small",
                }
            }
            return _FakeResponse(json.dumps(payload).encode())

        raise AssertionError("Unexpected request in error flow")

    yc = youcam.YouCam(
        api_key="any",
        opener=fake_opener,
        sleep=lambda s: None,
    )
    try:
        yc.analyze(b"xx")
        raise AssertionError("YouCamError was not raised on error status")
    except youcam.YouCamError as e:
        msg = str(e)
        assert "error_src_face_too_small" in msg, f"Error code missing in exception: {msg}"

# ---------- Test 3 ----------
def test_panel_parse_and_normalise():
    """Parse must locate the first JSON object with a verdict and normalise it."""
    # case 1: prose before JSON, verdict upper‑case
    text1 = "Some intro text. {\"verdict\":\"GOOD\",\"summary\":\"ok\"}"
    parsed1 = panel.parse(text1)
    assert isinstance(parsed1, dict), "parse should return a dict when JSON is present"
    assert parsed1.get("verdict") == "good", "Verdict should be lower‑cased"

    # case 2: invalid verdict value
    text2 = "{\"verdict\":\"maybe\"}"
    parsed2 = panel.parse(text2)
    assert parsed2 is None, "parse should return None for invalid verdict"

    # case 3: no JSON at all
    text3 = "Just plain text without any braces"
    parsed3 = panel.parse(text3)
    assert parsed3 is None, "parse should return None when no JSON found"

# ---------- Test 4 ----------
def test_reconcile_majority_split_and_dissent():
    """Reconcile must compute majority verdict, split flags and dissent list."""
    # three judges, two agree on 'good'
    results = {
        "judgeA": {"verdict": "good", "concerns": [{"concern": "acne", "effect": "helps"}]},
        "judgeB": {"verdict": "good", "concerns": [{"concern": "acne", "effect": "helps"}]},
        "judgeC": {"verdict": "poor", "concerns": [{"concern": "acne", "effect": "worsens"}]},
    }
    out = panel.reconcile(results)
    assert out["verdict"] == "good", "Majority verdict should be good"
    assert out["judges"] == 3, "All three judges are valid"
    assert abs(out["agreement"] - (2 / 3)) < 1e-9, "Agreement fraction mismatch"
    assert "acne" in out["per_concern"], "Concern key missing"
    assert out["per_concern"]["acne"]["split"] is True, "Split should be True when effects differ"
    assert out["dissent"] == ["judgeC"], "Dissent list should contain the disagreeing judge"

    # tie case: one good, one poor, one None -> verdict 'care'
    tie_results = {
        "j1": {"verdict": "good"},
        "j2": {"verdict": "poor"},
        "j3": None,
    }
    tie_out = panel.reconcile(tie_results)
    assert tie_out["verdict"] == "care", "Tie should yield 'care' verdict"
    assert tie_out["judges"] == 2, "Only two valid judges should be counted"

    # all None -> ValueError
    try:
        panel.reconcile({"a": None, "b": None})
        raise AssertionError("ValueError not raised when all judges are invalid")
    except ValueError:
        pass

# ---------- Test 5 ----------
def test_app_routes_with_fakes():
    """The Lambda handler must correctly route GET, POST /analyze and POST /check."""
    # monkey‑patch YouCam.analyze
    original_youcam = youcam.YouCam

    class _FakeYouCam:
        def __init__(self, *a, **kw):
            pass

        def analyze(self, *a, **kw):
            return {"acne": {"ui_score": 80, "raw_score": 70.0}}

    youcam.YouCam = _FakeYouCam

    # monkey‑patch make_ask_fns to return deterministic judge callables
    def fake_make_ask_fns():
        return {
            "j1": lambda msgs: json.dumps({"verdict": "good", "concerns": [], "summary": ""}),
            "j2": lambda msgs: json.dumps({"verdict": "good", "concerns": [], "summary": ""}),
            "j3": lambda msgs: json.dumps({"verdict": "poor", "concerns": [], "summary": ""}),
        }

    # inject into app module (even if it doesn't exist yet)
    setattr(app, "make_ask_fns", fake_make_ask_fns)

    # -------- GET '/' ----------
    get_event = {"httpMethod": "GET", "path": "/"}
    resp_get = app.lambda_handler(get_event, None)
    assert resp_get.get("statusCode") == 200, "GET / should return 200"
    body_get = resp_get.get("body", "")
    assert "Shelf Check" in body_get, "HTML page should contain 'Shelf Check'"

    # -------- POST '/analyze' ----------
    analyze_event = {
        "httpMethod": "POST",
        "path": "/analyze",
        "body": json.dumps({"image_b64": base64.b64encode(b"fakeimg").decode()}),
    }
    resp_analyze = app.lambda_handler(analyze_event, None)
    assert resp_analyze.get("statusCode") == 200, "POST /analyze should return 200"
    body_analyze = json.loads(resp_analyze.get("body", "{}"))
    assert "skin" in body_analyze, "Response must contain 'skin' key"
    assert body_analyze["skin"]["acne"]["ui_score"] == 80, "Skin result must match fake YouCam output"

    # -------- POST '/check' ----------
    check_event = {
        "httpMethod": "POST",
        "path": "/check",
        "body": json.dumps({
            "skin": {"acne": {"ui_score": 80, "raw_score": 70.0}},
            "product_name": "Sample",
            "ingredients": "Water, Glycerin"
        }),
    }
    resp_check = app.lambda_handler(check_event, None)
    assert resp_check.get("statusCode") == 200, "POST /check should return 200"
    check_body = json.loads(resp_check.get("body", "{}"))
    assert "verdict" in check_body, "Reconciliation result must contain verdict"
    assert check_body["verdict"] == "good", "Majority verdict should be good"
    assert "judges" in check_body, "Result must include judges count"
    # ensure judges' raw answers are present
    assert "j1" in check_body.get("answers", {}), "Judge j1 parsed answer should be present under answers"

    # -------- malformed JSON ----------
    bad_event = {"httpMethod": "POST", "path": "/analyze", "body": "not a json"}
    bad_resp = app.lambda_handler(bad_event, None)
    assert bad_resp.get("statusCode") == 400, "Malformed JSON should yield 400"
    err_body = json.loads(bad_resp.get("body", "{}"))
    assert "error" in err_body, "Error response must contain 'error' key"

    # restore original class
    youcam.YouCam = original_youcam

# ---------- Main runner ----------
if __name__ == "__main__":
    test_youcam_analyze_flow_and_parse()
    test_youcam_error_raises()
    test_panel_parse_and_normalise()
    test_reconcile_majority_split_and_dissent()
    test_app_routes_with_fakes()
    print("PASS: all shelfcheck tests passed")
