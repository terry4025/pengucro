import requests
import asyncio
import os
import time

async def ocr_image(img_path):
    from winsdk.windows.storage import StorageFile
    from winsdk.windows.graphics.imaging import BitmapDecoder
    from winsdk.windows.media.ocr import OcrEngine
    from winsdk.windows.globalization import Language

    abs_path = os.path.abspath(img_path)
    file = await StorageFile.get_file_from_path_async(abs_path)
    stream = await file.open_async(1) # Read mode
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    
    lang = Language("ko-KR")
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
    
    print("1. Accessing main reservation page...")
    session.get("https://xdungeon.net/layout/res/home.php?go=rev.main")
    
    print("2. Requesting captcha session key...")
    r_sess = session.post("https://xdungeon.net/core/captcha/session.php")
    print("Session Key:", r_sess.text)
    
    print("3. Downloading captcha image...")
    ts = int(time.time() * 1000)
    r_img = session.get(f"https://xdungeon.net/core/captcha/image.php?t={ts}")
    
    img_path = "scratch/temp_captcha.png"
    with open(img_path, "wb") as f:
        f.write(r_img.content)
    print("Captcha image saved to:", img_path)
    
    print("4. Running local Windows OCR...")
    try:
        text = await ocr_image(img_path)
        # Strip spaces and keep only alphanumeric characters
        clean_text = "".join(c for c in text if c.isalnum())
        print(f"Recognized Text Raw: '{text}'")
        print(f"Cleaned Text (5 digits): '{clean_text}'")
    except Exception as e:
        print("OCR Error:", e)

if __name__ == "__main__":
    asyncio.run(main())
