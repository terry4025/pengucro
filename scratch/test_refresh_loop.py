import asyncio
import os
import requests
import time
from PIL import Image

async def ocr_image(img_path):
    from winsdk.windows.storage import StorageFile
    from winsdk.windows.graphics.imaging import BitmapDecoder
    from winsdk.windows.media.ocr import OcrEngine
    from winsdk.windows.globalization import Language

    abs_path = os.path.abspath(img_path)
    file = await StorageFile.get_file_from_path_async(abs_path)
    stream = await file.open_async(1)
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    
    lang = Language("en-US")
    engine = OcrEngine.try_create_from_language(lang)
    if not engine:
        engine = OcrEngine.try_create_from_user_profile_languages()
        
    result = await engine.recognize_async(bitmap)
    return result.text

async def get_5_digit_captcha(session):
    temp_raw = "scratch/temp_loop_raw.jpg"
    temp_proc = "scratch/temp_loop_proc.png"
    
    for attempt in range(1, 51):
        # 1. Refresh CAPTCHA session
        session.post("https://xdungeon.net/core/captcha/session.php")
        ts = int(time.time() * 1000)
        
        # 2. Download captcha image
        r_img = session.get(f"https://xdungeon.net/core/captcha/image.php?t={ts}")
        with open(temp_raw, "wb") as f:
            f.write(r_img.content)
            
        # 3. Preprocess (4x upscale, binarize to strip background grids)
        img = Image.open(temp_raw)
        gray = img.convert("L")
        scaled = gray.resize((img.size[0] * 4, img.size[1] * 4), Image.Resampling.LANCZOS)
        
        # Binarize with a threshold to clean up grid lines
        binarized = scaled.point(lambda p: 255 if p > 130 else 0)
        binarized.save(temp_proc)
        
        # 4. Run Windows OCR
        text = await ocr_image(temp_proc)
        # Keep only digits
        digits = "".join(c for c in text if c.isdigit())
        
        if len(digits) == 5:
            print(f"SUCCESS on attempt {attempt}! Code: {digits} (Raw output: '{text}')")
            return digits
        else:
            # print(f"Attempt {attempt} failed: got '{digits}' (raw: '{text}')")
            pass
            
    print("FAILED to get a 5-digit CAPTCHA after 50 attempts")
    return None

async def main():
    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    session.headers.update(headers)
    session.get("https://xdungeon.net/layout/res/home.php?go=rev.main")
    
    print("Starting loop test...")
    start_time = time.perf_counter()
    code = await get_5_digit_captcha(session)
    elapsed = time.perf_counter() - start_time
    print(f"Elapsed time: {elapsed:.2f} seconds")

if __name__ == "__main__":
    asyncio.run(main())
