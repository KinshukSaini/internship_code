import os, sys, base64
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(override=False)
from google import genai
from google.genai import types

OUT = Path("charref-eval"); OUT.mkdir(exist_ok=True)
prompt = Path("charref-eval/prompt-MODEST.txt").read_text()
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"].strip())

def save(resp, out):
    for c in (resp.candidates or []):
        for p in (getattr(getattr(c,"content",None),"parts",None) or []):
            d = getattr(getattr(p,"inline_data",None),"data",None)
            if d:
                if isinstance(d,str): d=base64.b64decode(d)
                out.write_bytes(d); return True
    return False

def run(tag, model, image_config):
    cfg = types.GenerateContentConfig(response_modalities=["IMAGE"],
            **({"image_config":image_config} if image_config else {}))
    try:
        r = client.models.generate_content(model=model, contents=prompt, config=cfg)
        ok = save(r, OUT/f"mtx-{tag}.png")
        print(f"  {tag:24} -> {'saved' if ok else 'NO IMAGE'}  fr={getattr((r.candidates or [None])[0],'finish_reason',None)}")
    except Exception as e:
        print(f"  {tag:24} -> ERROR {type(e).__name__}: {str(e)[:120]}")

IC = types.ImageConfig
# A: NB Pro, no image_config at all (model default)
run("pro-default",   "gemini-3-pro-image-preview", None)
# B: NB Pro, 1K 9:16
run("pro-1K-9x16",   "gemini-3-pro-image-preview", IC(aspect_ratio="9:16", image_size="1K"))
# C: NB Pro, 2K 9:16 (current prod config — control)
run("pro-2K-9x16",   "gemini-3-pro-image-preview", IC(aspect_ratio="9:16", image_size="2K"))
# D: NB2 (flash) 2K 9:16 — different model
run("nb2-2K-9x16",   "gemini-3.1-flash-image-preview", IC(aspect_ratio="9:16", image_size="2K"))
