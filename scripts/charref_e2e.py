"""True end-to-end: drive the ACTUAL pipeline image client (generate_async,
tier=hero, default size) with the exact stored 0619 prompt. Proves the wired
production default now produces a clean image."""
import asyncio
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(override=False)
from app.config import load_settings
from app.uploader import make_uploader
from app.image_gen import make_image_client, DEFAULT_IMAGE_SIZE

async def main():
    prompt = Path("charref-eval/prompt-0619-ORIG.txt").read_text()
    s = load_settings()
    client = make_image_client(s, make_uploader(s))
    print("DEFAULT_IMAGE_SIZE wired =", DEFAULT_IMAGE_SIZE)
    imgs = await client.generate_async(prompt=prompt, tier="hero", source="charref-e2e")
    print("RESULT url:", imgs[0].url, "size:", imgs[0].size)

asyncio.run(main())
