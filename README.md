---
title: Video Gen
emoji: 🎬
colorFrom: indigo
colorTo: purple
sdk: gradio
sdk_version: 6.14.0
python_version: "3.11"
app_file: main.py
pinned: false
---

# Video-Gen

Gradio frontend for EvoLink Seedance 2.0 video generation + Gemini Nano Banana
character images, ElevenLabs voiceover, and Sync.so lipsync.

## Required secrets

Set these in the Space **Settings → Variables and secrets** (as *Secrets*):

| Secret | Purpose |
|---|---|
| `EVOLINK_API_KEY` | Seedance video generation |
| `GEMINI_API_KEY` | Nano Banana images + (optional) Gemini text |
| `ELEVENLABS_API_KEY` | Voiceover |
| `SYNC_API_KEY` | Lipsync |
| `ANTHROPIC_API_KEY` | Script / prompt LLM (default provider) |
| `AWS_ACCESS_KEY_ID` | S3 uploads (deliverables + lipsync inputs) |
| `AWS_SECRET_ACCESS_KEY` | S3 uploads |
| `AWS_BUCKET_NAME` | S3 bucket (default `upload-data-shoppin-as1`) |
| `AWS_REGION` | S3 region (default `ap-south-1`) |

System dependency `ffmpeg` is installed via `packages.txt`.
