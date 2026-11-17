import os, base64
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(override=False)
from google import genai
from google.genai import types
OUT=Path("charref-eval")
prompt=Path("charref-eval/prompt-MODEST.txt").read_text()
client=genai.Client(api_key=os.environ["GEMINI_API_KEY"].strip())
def save(r,o):
    for c in (r.candidates or []):
        for p in (getattr(getattr(c,"content",None),"parts",None) or []):
            d=getattr(getattr(p,"inline_data",None),"data",None)
            if d:
                if isinstance(d,str): d=base64.b64decode(d)
                o.write_bytes(d); return True
    return False
for size in ["4K"]:
    cfg=types.GenerateContentConfig(response_modalities=["IMAGE"],
        image_config=types.ImageConfig(aspect_ratio="9:16",image_size=size))
    try:
        r=client.models.generate_content(model="gemini-3-pro-image-preview",contents=prompt,config=cfg)
        ok=save(r,OUT/f"mtx-pro-{size}-9x16.png")
        print(f"pro-{size}-9x16 -> {'saved' if ok else 'NO IMAGE'}")
    except Exception as e:
        print(f"pro-{size} ERROR {type(e).__name__}: {str(e)[:100]}")
