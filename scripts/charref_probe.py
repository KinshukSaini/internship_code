import os, sys
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(override=False)
from google import genai
from google.genai import types

prompt = Path(sys.argv[1]).read_text()
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"].strip())
cfg = types.GenerateContentConfig(
    response_modalities=["IMAGE"],
    image_config=types.ImageConfig(aspect_ratio="9:16", image_size="2K"),
)
resp = client.models.generate_content(model="gemini-3-pro-image-preview", contents=prompt, config=cfg)
print("prompt_feedback:", getattr(resp, "prompt_feedback", None))
for i, c in enumerate(resp.candidates or []):
    print(f"cand[{i}] finish_reason:", getattr(c, "finish_reason", None))
    print(f"cand[{i}] finish_message:", getattr(c, "finish_message", None))
    print(f"cand[{i}] safety_ratings:", getattr(c, "safety_ratings", None))
