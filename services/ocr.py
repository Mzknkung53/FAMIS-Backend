from pdf2image import convert_from_path
import numpy as np
import logging
import time
from typing import List, Tuple
import re
from concurrent.futures import ThreadPoolExecutor
import easyocr

from .config import POPPLER_PATH
from .ai_client import client


reader = easyocr.Reader(['th', 'en'], gpu=True)


def upload_and_validate_file(file_path: str) -> dict:
    import os
    try:
        if not os.path.exists(file_path):
            return {"status": "error", "message": "File does not exist."}

        allowed_extensions = {'.pdf', '.jpg', '.png', '.zip'}
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in allowed_extensions:
            return {
                "status": "error",
                "message": "Unsupported file format. Accepted formats are .pdf, .jpg, .png, or .zip."
            }

        max_size_bytes = 25 * 1024 * 1024
        file_size = os.path.getsize(file_path)
        if file_size > max_size_bytes:
            return {
                "status": "error",
                "message": "File is too large. File size should not exceed 25MB."
            }

        return {"status": "success", "filename": file_path}

    except Exception:
        return {
            "status": "error",
            "message": "Upload failed due to server error. Possible causes include connection timeout, internal server failure, or file overload. Please try again later."
        }


def read_text_from_file(file_path: str) -> str | dict:
    logging.info(f"📥 Reading file: {file_path}")
    try:
        images = convert_from_path(file_path, dpi=200, poppler_path=POPPLER_PATH)
    except Exception as e:
        logging.error(f"❌ Failed to convert PDF: {e}")
        return {"Status": "error", "Message": f"Failed to convert PDF: {e}"}

    if not images:
        return {"Status": "error", "Message": "No uploaded document"}

    start_time_all = time.time()

    def correct_text_with_ai(text: str) -> str:
        prompt = (
            "คุณเป็นผู้เชี่ยวชาญด้านการแก้ข้อความที่ได้จาก OCR ภาษาไทย ซึ่งมักมีข้อผิดพลาด "
            "เช่น ตัวอักษรผิด ตัวอักษรตกหล่น เว้นวรรคผิด และการสะกดคำผิดในบริบทของเอกสารราชการหรือการเงิน\n\n"
            "กรุณาแก้ไขข้อความให้ถูกต้องโดยไม่เปลี่ยนความหมายดั้งเดิม ห้ามตัดคำ ห้ามแปลความใหม่ หรือย่อข้อความ\n"
            "ให้เว้นวรรคให้เหมาะสมตามหลักภาษาไทย เช่น คำว่า 'เลขที่บัญชี' อย่าเว้นเป็น 'เลขที่ บัญชี'\n\n"
            "ให้คงรูปแบบการเว้นบรรทัดไว้ตามต้นฉบับ และให้ตอบกลับเฉพาะข้อความที่แก้ไขแล้วเท่านั้น โดยไม่ต้องมีคำอธิบาย\n\n"
            "และช่วยทำให้วันที่เป็นรูปแบบเดียวกัน เช่น 01 มกราคม 2568\n"
            "ตัวอย่าง:\n"
            "อินว๊อยช์เลขที่ 1234 → อินวอยซ์เลขที่ 1234\n"
            "จำนวนเ งิน 3,500 บาท → จำนวนเงิน 3,500 บาท\n"
            "ธนาคา กสิกรไทย → ธนาคารกสิกรไทย\n"
            "ข้อความที่ต้องแก้:\n"
            f"{text}"
        )

        start_time = time.time()
        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                timeout=60
            )
            elapsed = time.time() - start_time
            logging.info(f"⏳ GPT responded in {elapsed:.2f} seconds")
            return response.choices[0].message.content.strip()

        except Exception as e:
            logging.error(f"❌ GPT API failed: {e}")
            return text

    def batch_correct_with_ai(texts: List[str]) -> List[str]:
        def correct_chunk(chunk):
            return correct_text_with_ai("\n".join(chunk)).split("\n")

        all_fixed = []
        chunks = [texts[i:i+30] for i in range(0, len(texts), 30)]

        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(correct_chunk, chunks))

        for corrected_lines in results:
            all_fixed.extend(corrected_lines)

        return all_fixed

    def process_page(pg_img_tuple: Tuple[int, np.ndarray]) -> str:
        pg, img = pg_img_tuple
        start_time = time.time()
        logging.info(f"🟡 Processing Page {pg}")

        results = reader.readtext(np.array(img))
        texts = [raw_text for _, raw_text, _ in results]
        logging.info(f"🔍 Found {len(texts)} text items on Page {pg}")

        if not texts:
            return f"\n📄 Page {pg}\n⚠️ No text found\n"

        fixed_texts = batch_correct_with_ai(texts)

        text_output = f"\n📄 Page {pg}\n"
        for raw_text, fixed_text in zip(texts, fixed_texts):
            text_output += f"❌ {raw_text}\n"
            text_output += f"✅ {fixed_text}\n\n"

        elapsed = time.time() - start_time
        logging.info(f"✅ Finished Page {pg} in {elapsed:.2f} seconds")
        return text_output

    with ThreadPoolExecutor(max_workers=6) as executor:
        outputs = list(executor.map(process_page, enumerate(images, start=1)))

    total_text = "".join(outputs).strip()
    elapsed_total = time.time() - start_time_all

    if not re.search(r"✅ .+", total_text):
        return {"Status": "error", "Message": "OCR failed to detect or recognize any text in the uploaded document"}

    logging.info("✅ All done. Output saved to memory (not file)")
    logging.info(f"⏱️ Total processing time: {elapsed_total:.2f} seconds")

    return total_text


