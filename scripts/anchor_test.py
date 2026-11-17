"""Anchor-style render (reference-conditioned) at 2K vs 1K, via the real
pipeline image client. Uses the clean char ref as the identity reference."""
import asyncio
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(override=False)
from app.config import load_settings
from app.uploader import make_uploader
from app.image_gen import make_image_client

CHAR_REF = "https://upload-data-shoppin-as1.s3.ap-south-1.amazonaws.com/product_flatlays/crops/f7ca3628bbbd8b0c0fbc2195788056dc76a74a9c6fcaaa3689e573effcd437ed.jpg"
PROMPT = (
    "The exact same woman from the reference image (identical face, hair, burgundy "
    "mirror-work corset, gold jewelry), now seated on a warm-toned living-room couch in "
    "an Indian apartment, leaning slightly forward toward the camera with a knowing smile, "
    "one hand raised near her collarbone mid-gesture as she talks. Soft natural daylight "
    "from a window on camera-left. Candid UGC phone-video still, vertical 9:16, chest-to-head "
    "medium shot, sharp focus, true-to-life clarity, realistic skin texture."
)

async def main():
    s = load_settings()
    client = make_image_client(s, make_uploader(s))
    for size in ["2K", "1K"]:
        imgs = await client.generate_async(
            prompt=PROMPT, tier="hero", source="anchor-test",
            size=size, reference_image_urls=[CHAR_REF], reference_roles=["character"],
        )
        print(f"anchor size={size} -> {imgs[0].url}")

asyncio.run(main())
