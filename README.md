# ComfyUI nodes for AI video APIs

Bring [Remerged](https://remerged.ai)'s AI video models into ComfyUI — and any
other service that describes itself with an OpenAPI 3.1 document.

Remerged sells AI video at cost: one credit is one US cent. A job made through
the API is the same job the website makes — the same models, the same library,
the same email when it is ready.

**Nine nodes, no extra Python packages required.**

Remerged is built in and works out of the box. Nothing in the pack is
Remerged specific: a *provider profile* says where the endpoints live and what
the request and response fields are called, so the same nodes can drive a
different API.

---

## Install

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/anr2me/comfyui-remerged.git
# or copy the comfyui-remerged folder here
```

Restart ComfyUI. The nodes appear under the **Remerged** category.

There is nothing to `pip install`. The client uses the standard library and
falls back to `requests` automatically when it happens to be available.

## Set the API key

Keys are made on your [account page](https://remerged.ai/account) and shown
once. The pack looks for one in this order:

| Where | How |
| --- | --- |
| The node's `api_key` field | Paste it into any Remerged node. Handy for one-off use. |
| `REMERGED_API_KEY` env var | `export REMERGED_API_KEY=rm_live_…` before starting ComfyUI. **Recommended.** |
| `<ComfyUI>/user/remerged/config.json` | Written for you by `python3 scripts/set_api_key.py rm_live_…`. |
| `<ComfyUI>/user/remerged_api_key.txt` | The key on one line. |

The node's own `api_key` field always wins, so you can use different keys on
different nodes.

```python
# <ComfyUI>/user/remerged/config.json
{
  "api_key": "rm_live_your_key_here"
}
```

Keys carry a scope: **read** (catalogues, quotes, account) and/or **generate**
(jobs, uploads). A generate-only key cannot use `Remerged Quote`; the node
tells you rather than failing silently.

---

## Using another API

Every node has a **provider** dropdown and a **base_url** field. Pick a
provider, and the node talks to that service instead.

### Automatic setup with the Remerged API Provider node

Drop **Remerged API Provider** on the canvas, fill in:

| Field | What to put |
| --- | --- |
| `provider_name` | A short name, e.g. `my-api` |
| `base_url` | The service's base URL, e.g. `https://api.example.com/v1` |
| `openapi_url` | Its OpenAPI document. Leave blank and a few likely URLs are tried. |
| `label` | A friendly name for the dropdown |
| `api_key` | Saved with the profile (the file is chmod 600) |

Run it. The node reads the OpenAPI document and works out, on its own:

- **the endpoints** — which path creates a job, which one reports status, which
  one quotes a price, which one lists models, which one accepts uploads;
- **the request fields** — `prompt` → `text` → `input_prompt`, `duration` →
  `duration_seconds`, `variations` → `batch_count` and so on;
- **the response fields** — where the job id lives, where the status word is,
  where the finished video URL is, and how deeply it is nested;
- **the status words** — `ready`, `succeeded`, `completed` and `done` all mean
  the same thing, so it maps them onto one vocabulary;
- **the authentication** — bearer, api key in a header, or a query parameter.

It saves the result as a profile and tells you what it could not work out.
Anything missing shows up in the `warnings` output — fill those in by editing
`<ComfyUI>/user/remerged/providers/<name>.json`.

The profile looks like this:

```json
{
  "name": "my-api",
  "base_url": "https://api.example.com/v1",
  "endpoints": {
    "create":  {"method": "POST", "path": "/renders"},
    "status":  {"method": "GET",  "path": "/renders/{id}"},
    "list":    {"method": "GET",  "path": "/renders"},
    "quote":   {"method": "POST", "path": "/estimate"},
    "models":  {"method": "GET",  "path": "/catalog"},
    "upload":  {"method": "POST", "path": "/files"},
    "account": {"method": "GET",  "path": "/wallet"}
  },
  "job_fields": {
    "prompt": "text",
    "duration": "duration_seconds",
    "variations": "batch_count"
  },
  "job_fields_out": {
    "id": "identifier",
    "status": "state",
    "video": "output_url"
  },
  "responses": {
    "status": {"envelope": "data"}
  },
  "status_values": {
    "ready": ["succeeded"],
    "generating": ["in_progress"],
    "failed": ["failed"]
  }
}
```

### Escaping the profile

Two escape hatches cover anything the mapping missed:

- **`extra_inputs`** on the main node takes a JSON object and merges it into the
  request body *last*, so it can override anything:

  ```json
  {"quality": "cinematic", "temperature": 0.8}
  ```

- **`base_url`** on any node overrides the provider's host for that run.

### Base URL precedence

1. The node's `base_url` field.
2. `<PROVIDER>_BASE_URL` in the environment (`MY_API_BASE_URL`).
3. `base_urls[<provider>]` in `<ComfyUI>/user/remerged/config.json`.
4. The provider profile's own base URL.
5. For the built-in Remerged provider only: `REMERGED_BASE_URL`, or `base_url`
   in `config.json`.

The global Remerged overrides deliberately do not reach other providers — a
Remerged proxy setting must not redirect somebody else's API.

---

## The nodes

### Remerged Video
The main node. Makes a video and waits for it.

| Output | Type | What it is |
| --- | --- | --- |
| `video` | `VIDEO` | The finished clip, ready for a video node. A file path on older ComfyUI builds. |
| `video_path` | `STRING` | Where the file was saved (a JSON list when there is more than one). |
| `job_ids` | `STRING` | JSON list of the job ids. |
| `credits` | `INT` | Credits charged. One credit is one US cent. |
| `status` | `STRING` | `ready`, `failed`, … |
| `info` | `STRING` | The full job JSON, including the quote and poster path. |

Finished videos are written to `<ComfyUI>/output/Remerged/`.

**Inputs worth knowing about**

- `mode` — the render quality, optionally with an upscale: `480p`, `720p`,
  `1080p`, `480p-1080p`, `720p-4k`, `1080p-4k`… Leave it on `default` and the
  model uses its own lowest quality, which is the cheapest.
- `seed` / `use_seed` — with `use_seed` **off** the model picks the seed, which
  is what you want for `variations`. Turn it **on** to pin the seed (one seed
  makes one video, so it cannot be combined with variations).
- `variations` — up to four takes at once.
- `start_image` / `end_image` — the frames the clip begins and ends on.
- `reference_images` — subject or style references.
- `image_urls` — public HTTPS images, one per line.
- `max_credits` — refuse the job if it would cost more than this. `0` means no
  limit. Nothing is charged when the limit is hit.
- `draft` — Seedance 2.5 only. A cheap 480p draft you can finish at 1080p.

The node checks the input rules **before** spending anything, so a request the
API would reject fails instantly with a plain English message rather than a
failed render.

### Remerged Quote
Prices a job without making it. Returns `credits`, `usd` and
`per_video_credits`.

### Remerged Upload
Uploads a local file (image, video or audio) or a connected `IMAGE` and returns
the `key` to use in a job. Feed the key into the main node's `uploaded_inputs`
field as JSON:

```json
[{"key": "uploads/abc/xyz", "role": "reference_video", "seconds": 5}]
```

Roles: `reference`, `first_frame`, `last_frame`, `reference_video`,
`reference_audio`, `upscale_source`, `character`, `costume`.

### Remerged Job Status
Looks up a job you made earlier and fetches a fresh video URL. Video URLs
last one hour; asking again gets a new one.

### Remerged Finish Draft
Finishes a Seedance 2.5 draft at 1080p — the same take, within seven days.

### Remerged Account
Balance, plan, top-up link and how many jobs are in flight.

### Remerged Models
Lists the models, their modes and their limits. Tick **refresh** to pull the
live catalogue from the API.

### Remerged Recent Jobs
Your recent jobs, newest first.

### Remerged API Provider
Describes another service and saves a profile from its OpenAPI document.

---

## The Remerged models

| Model | Length | Renders at | Takes |
| --- | --- | --- | --- |
| `seedance-2.5` | 4–30s | 480p, 720p, 1080p | Prompt, images, frames, reference video and audio, characters |
| `seedance-2.0` | 4–15s | 480p, 720p, 1080p | Prompt, images, frames, reference video and audio, characters |
| `seedance-2.0-fast` | 4–15s | 480p, 720p | Same as Seedance 2.0, cheaper and quicker |
| `seedance-2.0-mini` | 4–15s | 480p, 720p | Same again, best value |
| `minimax-h3` | 4–10s | 720p | Prompt, one starting image |
| `ltx-2.5` | 4–10s | 720p | Prompt, one starting image |
| `wan-2.6` | 5, 10, 15s | 720p, 1080p | Prompt, one starting image |

On the Free plan you can use `seedance-2.0-mini` (480p, up to 4s), `minimax-h3`
and `ltx-2.5`. The others need a paid plan.

Two rules the pack enforces for you, because the API refuses them anyway:

- A **Seedance** video starts from frames **or** from references, never both.
- **MiniMax H3**, **LTX-2.5** and **Wan 2.6** take one starting image at most.

These are Remerged-specific, so they only apply when the selected provider is
Remerged. Other providers are left to their own API.

---

## Prompting notes

Prompts reach the model exactly as written, except on Wan 2.6, which expands
each prompt itself and may cut a video made from words into several shots.

Refer to pictures as **Image 1**, **Image 2**… in the order you send them
(`images` first, then `image_urls`), and to clips and audio as **Video 1** and
**Audio 1**.

Pictures for Seedance must be 300–6000 pixels a side, and at most 2.5 times as
wide as tall. For Wan 2.6 it is 240–7680 pixels.

---

## Limits

Remerged, for reference:

- 60 requests a minute per key.
- Jobs in flight at once: Free 2, Standard 4, Pro 8, Max 16.
- Five keys per account.
- No credits, no job — a `402` carries the link to top up.

The pack polls every 10 seconds by default, well inside the rate limit.

## Troubleshooting

**"No API key for 'remerged'"** — set `REMERGED_API_KEY`, or paste the key into
the node's `api_key` field.

**402 insufficient credits** — the error carries a `topup_url`; the pack
surfaces it in the message.

**"Could not find a 'create job' endpoint"** — the OpenAPI document does not
look like the ones the pack recognises. Add the endpoint by hand in the
profile, or point `openapi_url` somewhere else.

**A field is not being sent** — the provider has no matching request field.
Check the `warnings` output of the provider node, and use `extra_inputs` for
anything it could not map.

**A render failed** — failed jobs are refunded in full by Remerged. The
`failure` object in the `info` output says whether to change the prompt, retry
later, or retry now.

**A job is still running after the timeout** — raise `timeout_minutes`, or use
**Remerged Job Status** with the job id to pick it up later.

---

## Development

```bash
python3 tests/test_remerged.py
```

The suite runs the real node code against a local mock of two services — one
Remerged-shaped, one with completely different paths, field names and status
words. No key, no network, no credits.

---

API reference: <https://remerged.ai/developers> ·
OpenAPI schema: <https://remerged.ai/openapi.json> ·
Plain-text guide: <https://remerged.ai/llms.txt>
