# -*- coding: utf-8 -*-
"""YouCam API client wrapper.

Implements the minimal flow required for the hackathon: uploading a selfie,
starting a skin‑analysis task, polling for completion and returning the
concern scores. All network interaction is delegated to the injected ``opener``
and ``sleep`` callables.
"""

from __future__ import annotations

import json
import time
import urllib.request
from typing import Any, Callable, Sequence, Union

class YouCamError(Exception):
    """Exception raised when the YouCam API returns an error response.

    The string representation contains the error code returned by the API.
    """

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}" if message else code)

    def __str__(self) -> str:
        if self.message:
            return f"{self.code}: {self.message}"
        return self.code

class YouCam:
    """Client for the Perfect Corp. YouCam skin‑analysis service.

    Parameters
    ----------
    api_key: str
        Bearer token used for authentication.
    base: str, default as defined by the spec
        Root URL for the YouCam S2S endpoints.
    opener: callable, default ``urllib.request.urlopen``
        Function used to perform HTTP requests; injected for testing.
    sleep: callable, default ``time.sleep``
        Sleep function used between polling attempts; injected for testing.
    """

    DEFAULT_ACTIONS = [
        "acne",
        "moisture",
        "oiliness",
        "pore",
        "redness",
        "texture",
        "wrinkle",
        "dark_circle_v2",
    ]

    def __init__(
        self,
        api_key: str,
        base: str = "https://yce-api-01.makeupar.com/s2s/v2.0",
        opener: Callable[[urllib.request.Request], Any] = urllib.request.urlopen,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Create a new YouCam client.

        Stores the provided credentials and helper callables. No network activity
        is performed at construction time.
        """
        self._api_key = api_key
        self._base = base.rstrip("/")
        self._opener = opener
        self._sleep = sleep

    def _request(self, method: str, url: str, headers: dict | None = None, body: bytes | None = None) -> dict:
        """Perform an HTTP request and return the parsed JSON response."""
        if headers is None:
            headers = {}
        if "Authorization" not in headers:
            headers["Authorization"] = f"Bearer {self._api_key}"
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        response = self._opener(req)
        with response:
            data = response.read()
            return json.loads(data)

    def _upload_file(
        self, image_bytes: bytes, content_type: str
    ) -> tuple[str, str, dict, str]:
        """Step 1: POST /file/skin-analysis to obtain a presigned upload URL."""
        url = f"{self._base}/file/skin-analysis"
        body_dict = {
            "files": [
                {
                    "content_type": content_type,
                    "file_name": "selfie.jpg",
                    "file_size": len(image_bytes),
                }
            ]
        }
        body = json.dumps(body_dict).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        data = self._request("POST", url, headers=headers, body=body)
        file_info = data["data"]["files"][0]
        # live API: presigned request lives at data.files[0].requests[0]; tolerate a flat "url" too
        reqs = file_info.get("requests") or data.get("requests") or [{"url": file_info.get("url"), "method": "PUT"}]
        upload_info = reqs[0]
        file_id = file_info["file_id"]
        upload_url = upload_info.get("url")
        upload_headers = dict(upload_info.get("headers") or {})
        method = upload_info.get("method", "PUT")
        return file_id, upload_url, upload_headers, method

    def _put_bytes(
        self, upload_url: str, upload_headers: dict, method: str, image_bytes: bytes, content_type: str
    ) -> None:
        """Step 2: PUT image bytes to the presigned URL."""
        if "Content-Type" not in upload_headers:
            upload_headers["Content-Type"] = content_type
        req = urllib.request.Request(
            upload_url, data=image_bytes, headers=upload_headers, method=method
        )
        # Presigned upload may respond without JSON; we just need to ensure the upload happened.
        self._opener(req)

    def _create_task(self, file_id: str, actions: Sequence[str]) -> str:
        """Step 3: POST /task/skin-analysis to start the analysis task."""
        url = f"{self._base}/task/skin-analysis"
        body_dict = {
            "src_file_id": file_id,
            "dst_actions": list(actions),
            "format": "json",
        }
        body = json.dumps(body_dict).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        data = self._request("POST", url, headers=headers, body=body)
        return data["data"]["task_id"]

    def _poll_task(self, task_id: str, poll_every: int, max_polls: int) -> dict:
        """Step 4: GET /task/skin-analysis/<task_id> until status changes from 'running'."""
        url = f"{self._base}/task/skin-analysis/{task_id}"
        for _ in range(max_polls):
            data = self._request("GET", url)
            status = data["data"]["task_status"]
            if status == "success":
                return data["data"]
            if status == "error":
                error = data["data"].get("error", "unknown_error")
                error_message = data["data"].get("error_message")
                raise YouCamError(error, error_message)
            self._sleep(poll_every)
        raise YouCamError("timeout", f"Task {task_id} did not complete within {max_polls * poll_every} seconds")

    def _extract_results(self, task_data: dict) -> dict[str, dict[str, Union[int, float]]]:
        """Extract per-concern scores from the task result data."""
        results = task_data.get("results", {})
        # Accept both data.results.output and data.results (list) as per spec
        output = results.get("output", results) if isinstance(results, dict) else results
        result_dict = {}
        for item in output or []:
            if not isinstance(item, dict) or "type" not in item or item.get("ui_score") is None:
                continue  # some concerns (e.g. composite types) come back without a ui_score
            concern = item["type"]
            result_dict[concern] = {
                "ui_score": int(item["ui_score"]),
                "raw_score": float(item.get("raw_score") or 0.0),
            }
        return result_dict

    def analyze(
        self,
        image_bytes: bytes,
        content_type: str = "image/jpeg",
        actions: Sequence[str] = DEFAULT_ACTIONS,
        poll_every: int = 3,
        max_polls: int = 40,
    ) -> dict[str, dict[str, Union[int, float]]]:
        """Analyse a selfie image and return per‑concern scores.

        Performs the multi‑step API flow (file upload, task creation, polling)
        and returns a mapping ``concern -> {"ui_score": int, "raw_score": float}``.

        Raises
        ------
        YouCamError
            If the remote task ends with an error status.
        """
        file_id, upload_url, upload_headers, method = self._upload_file(image_bytes, content_type)
        self._put_bytes(upload_url, upload_headers, method, image_bytes, content_type)
        task_id = self._create_task(file_id, actions)
        task_data = self._poll_task(task_id, poll_every, max_polls)
        return self._extract_results(task_data)
