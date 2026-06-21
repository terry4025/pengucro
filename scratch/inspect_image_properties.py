from PIL import Image

img_path = "scratch/temp_captcha.png"
try:
    img = Image.open(img_path)
    print("Format:", img.format)
    print("Size:", img.size)
    print("Mode:", img.mode)
    
    # Save a version with white background if it has transparency
    if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
        # Create a white background
        bg = Image.new("RGBA", img.size, (255, 255, 255))
        # Paste the image onto the background
        bg.paste(img, (0, 0), img.convert("RGBA"))
        bg.convert("RGB").save("scratch/temp_captcha_processed.png")
        print("Processed version saved as scratch/temp_captcha_processed.png (White background)")
    else:
        # Just convert to grayscale and binarize
        gray = img.convert("L")
        # Resize to 2x larger to make OCR easier
        resized = gray.resize((img.size[0] * 2, img.size[1] * 2), Image.Resampling.LANCZOS)
        resized.save("scratch/temp_captcha_processed.png")
        print("Processed version saved (Grayscale 2x resize)")
except Exception as e:
    print("Error:", e)
