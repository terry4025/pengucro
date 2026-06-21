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

async def main():
    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    session.headers.update(headers)
    session.get("https://xdungeon.net/layout/res/home.php?go=rev.main")
    
    with open("scratch/batch_ocr_log.txt", "w", encoding="utf-8") as out:
        for i in range(10):
            session.post("https://xdungeon.net/core/captcha/session.php")
            ts = int(time.time() * 1000)
            r_img = session.get(f"https://xdungeon.net/core/captcha/image.php?t={ts}")
            
            raw_path = f"scratch/test_cap_{i}.jpg"
            with open(raw_path, "wb") as f:
                f.write(r_img.content)
                
            img = Image.open(raw_path)
            # Try converting to grayscale, thresholding, and slightly resizing
            gray = img.convert("L")
            scaled = gray.resize((img.size[0] * 4, img.size[1] * 4), Image.Resampling.LANCZOS)
            
            # Save different processed versions
            proc_path = f"scratch/test_cap_proc_{i}.png"
            scaled.point(lambda p: 255 if p > 125 else 0).save(proc_path)
            
            text = await ocr_image(proc_path)
            clean = "".join(c for c in text if c.isalnum())
            out.write(f"Sample {i}: Raw='{text}', Clean='{clean}'\n")
            print(f"Sample {i} written to log")

if __name__ == "__main__":
    asyncio.run(main())
