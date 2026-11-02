# Shelf Check

**One selfie, then any skincare product: does it suit *your* skin before you buy?**

Built for the YouCam API Skin AI & eCommerce VTO Hackathon (submission period Sep 29 – Nov 2, 2026). All code in this repository was written during the submission period.

**Live demo:** https://rb3zrvsjhltwkfpadydcasehyu0iqlcu.lambda-url.us-east-2.on.aws/ — press "Use the demo face", then "Try a sample product", then "Check this product". No sign-in.

**Store demo:** https://rb3zrvsjhltwkfpadydcasehyu0iqlcu.lambda-url.us-east-2.on.aws/shop — a fictional demo store where each product card has a "Check fit for my skin" button that reads the ingredient list from the page, plus "Compare two" for a side-by-side verdict on the same skin. Scores live in the page for that visit only; nothing is stored.

## The problem

Online skincare shoppers buy from marketing copy written for everyone. They can't tell whether a product suits their own skin until after they've bought it, and they often return it or give up on it. Retailers pay for those wrong purchases in returns, refunds and lost trust.

## What Shelf Check does

1. **Measure.** The shopper takes one selfie. The **YouCam AI Skin Analysis API** scores 8 concerns from 0 to 100, where higher is healthier: acne, moisture, oiliness, pores, redness, texture, wrinkles and dark circles.
2. **Paste.** The shopper pastes a product's ingredient list from any product page.
3. **Judge.** Three independent AI models from three different makers read the ingredient list against *that* skin, concern by concern. Each says whether every concern is *helped*, *neutral* or *worsened*, and cites the ingredients behind it.
4. **Decide.** One verdict (**Good fit / Use with care / Poor fit**), how many models agree, a **SPLIT** badge wherever the models disagree on a concern, and the dissenting model's verdict.
5. **Shop.** On the store page, the same check runs from a button on each product card, and two products can be compared side by side for the same skin. Every result shows a timing line such as "3/3 models responded in 18.4 s".

Timing, measured on the live demo: skin analysis usually 5–7 s; a product check usually 15–30 s, sometimes over a minute when the models are busy.

Showing where independent models disagree is the point. A single model gives one confident answer. Three models from different makers show which parts are settled and which still need a closer look.

## YouCam API used

- **AI Skin Analysis** (server-to-server API v2.0, `https://yce-api-01.makeupar.com/s2s/v2.0`), SD mode, 8 concerns per call (`acne, moisture, oiliness, pore, redness, texture, wrinkle, dark_circle_v2`). That is 12 units per successful analysis; failed tasks are not charged.
- Flow (`src/shelfcheck/youcam.py`): `POST /file/skin-analysis` → `PUT` the image to the pre-signed URL → `POST /task/skin-analysis` → poll `GET /task/skin-analysis/{task_id}` → read `results.output[].ui_score / raw_score`.
- Framing: the API needs the face to fill 60–80% of the frame. The page resizes the photo in the browser. If the API reports `error_src_face_out_of_bound` or `error_src_face_too_small`, it re-frames the photo (pads or crops it) and retries automatically, so shoppers don't have to.

## Architecture

```
Browser (single HTML page, no external JS)
   │  POST /analyze {image}           POST /check {skin, product, ingredients}
   ▼
AWS Lambda, Python 3.12, stdlib only (src/shelfcheck/app.py)
   ├─► YouCam AI Skin Analysis API ──► 8 concern scores
   └─► 3 judges in parallel (Nebius Token Factory, OpenAI-compatible API)
         Qwen3-235B (Alibaba) · DeepSeek-V4-Flash (DeepSeek) · gpt-oss-120b (OpenAI open-weight)
         └─► panel.py: parse JSON → majority verdict, agreement, per-concern split, dissent
```

| File | Role |
|---|---|
| `src/shelfcheck/youcam.py` | YouCam client: upload, task, poll, parse |
| `src/shelfcheck/panel.py` | Judge prompt, robust JSON parsing, concern-name normalisation, reconciliation |
| `src/shelfcheck/app.py` | Lambda handler (works with Function URLs and API Gateway events) |
| `src/shelfcheck/static/index.html` | Mobile-first UI |
| `src/shelfcheck/static/shop.html` | Fictional demo store: per-card check and compare two |
| `src/shelfcheck/static/products.json` | Three fictional products, no real brands |
| `src/shelfcheck/static/demo-face.jpg` | Demo face: **AI-generated (synthetic), not a real person** |
| `tests/test_shelfcheck.py`, `tests/test_upgrade.py` | 10 tests, no network |
| `tools/local_server.py` | Run the same handler locally |

## Run it locally

Requires Python 3.12, a YouCam API key and a Nebius Token Factory API key.

```bash
export YOUCAM_API_KEY=...      # from the YouCam API console
export NEBIUS_API_KEY=...
PYTHONPATH=src python tools/local_server.py        # open http://localhost:8000
PYTHONPATH=src python -m pytest tests/ -q          # 5 passed
```

## Deploy (AWS Lambda)

```bash
(cd src && zip -r ../shelfcheck.zip shelfcheck)
```
Create a Python 3.12 Lambda with handler `shelfcheck.app.lambda_handler`, 512 MB memory, a 120 s timeout, and the two environment variables above. Then turn on a Function URL.

## Privacy and limits

- The selfie is sent only to the YouCam API for analysis. This app stores no photos and no results.
- The ingredient judgement comes from AI models reading an ingredient list. It is shopping guidance, **not medical advice**.

## Team

Alan Finney and Tammy Finney, 3Dogs Nexus (https://3dogs.ai).

## License

Apache-2.0. See [LICENSE](LICENSE).
