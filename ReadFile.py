# Import

from pdf2image import convert_from_path
import openai 
from openai import OpenAI
import json
import re
from dotenv import load_dotenv
import os
from typing import List, Tuple , Union
import numpy as np
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import as_completed
import easyocr
reader = easyocr.Reader(['th', 'en'], gpu=True)
import tempfile
from pymongo import MongoClient
import pymysql
from pymysql.cursors import DictCursor

from flask import Flask
from flask_cors import CORS
from flask import request
from flask import jsonify

# API key

load_dotenv(override=True)

api_key = os.getenv("OPENAI_API_KEY")
assert api_key, "Cannot find OPENAI_API_KEY in .env"
print("✅ OpenAI Key Loaded")

MYSQL_HOST     = os.getenv("MYSQL_HOST","localhost")
MYSQL_PORT     = int(os.getenv("MYSQL_PORT","3306"))
MYSQL_USER     = os.getenv("MYSQL_USER","root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD","")
MYSQL_DB       = os.getenv("MYSQL_DB","famis_db")

assert MYSQL_PASSWORD, "Cannot find MYSQL_PASSWORD in .env"
print("✅ MySQL Settings Loaded")

def get_mysql_connection():
    return pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        db=MYSQL_DB,
        charset="utf8mb4",
        cursorclass=DictCursor
    )

client = OpenAI(
    api_key=api_key,
    base_url="https://openrouter.ai/api/v1"
)

# Start flask

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "http://localhost:5173"}})

# Validate file format and size
# Upload and Validate

def Upload_And_Validate_File(file_path: str) -> dict:
    """
    ตอนนี้ข้ามเรื่องตรวจ UserAccount เพราะยังไม่มีระบบล็อกอิน Entra ID
    เฉพาะเช็คว่าไฟล์มีอยู่จริง, นามสกุลถูกต้อง, และขนาดไม่เกิน 25MB
    """
    # 1) ตรวจว่าไฟล์มีอยู่จริง
    if not os.path.exists(file_path):
        return {"status": "error", "message": "File does not exist."}

    # 2) ตรวจนามสกุลไฟล์
    allowed_extensions = {'.pdf', '.jpg', '.png', '.zip'}
    ext = os.path.splitext(file_path)[1].lower()
    if ext not in allowed_extensions:
        return {
            "status": "error",
            "message": "Unsupported file format. Accepted formats are .pdf, .jpg, .png, or .zip."
        }

    # 3) ตรวจขนาดไฟล์ไม่เกิน 25MB
    max_size_bytes = 25 * 1024 * 1024  # 25 MB
    file_size = os.path.getsize(file_path)
    if file_size > max_size_bytes:
        return {
            "status": "error",
            "message": "File is too large. File size should not exceed 25MB."
        }

    # ถ้าผ่านทุกข้อ ให้คืน success
    return {"status": "success", "filename": file_path}

# ### Import file to use EasyOCR
# - Change file -> img
# - Correction text by AI

def Read_Text_From_File(file_path: str) -> str | dict:
    logging.info(f"📥 Reading file: {file_path}")
    try:
        images = convert_from_path(file_path, dpi=200)
    except Exception as e:
        logging.error(f"❌ Failed to convert PDF: {e}")
        return {"Status": "error", "Message": f"Failed to convert PDF: {e}"}

    if not images:
        return {"Status": "error", "Message": "No extractable financial information found in the document."}

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
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            temperature=0
        )
        elapsed = time.time() - start_time
        logging.info(f"⏳ GPT responded in {elapsed:.2f} seconds")
        return response.choices[0].message.content.strip()

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
        return {"Status": "error", "Message": "No extractable financial information found in the document."}

    logging.info("✅ All done. Output saved to memory (not file)")
    logging.info(f"⏱️ Total processing time: {elapsed_total:.2f} seconds")

    return total_text

# Extract key field by use AI

def Interpret_Financial_Fields(ocr_text: str) -> list | dict:
    system_prompt = (
        "You are an AI assistant that extracts key information from Thai government "
        "financial documents using OCR text. Please extract the following fields and "
        "return only in JSON format:\n\n"
        "- bill_number: เลขที่ใบขอซื้อ เช่น 10778\n"
        "- supplier_name: หน่วยงานหรือชื่อผู้ขาย หรือ ชื่อซัพพลายเออร์\n"
        "- amount: ยอดรวมสุทธิที่อยู่ใกล้คำว่า 'รวมทั้งสิ้น', 'ยอดรวม', 'รวมจำนวนเงิน', 'รวม' เอาแต่ตัวเลขเท่านั้น\n"
        "- payment_date: วันที่ใด ๆ ในเอกสาร \n"
        "- signature: ชื่อจริงนามสกุลที่อยู่ใกล้คำว่า 'อนุมัติ' หรือ 'ผู้เบิก' และตัดคำนำหน้าชื่อออก ให้เหลือแต่ชื่อและนามสกุลเท่านั้น\n\n"
        "If any field is not found, use null. Respond in JSON format only without any explanation."
    )

    def extract_fields(text: str) -> dict:
        user_prompt = f"OCR Text in Thai:\n{text}\n\nPlease return the result in JSON only."
        try:
            response = client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0
            )
        except Exception as e:
            print(f"❌ AI service error: {e}")
            return {"status": "error", "message": "AI service unavailable."}

        content = response.choices[0].message.content.strip()
        print(f"📥 AI response:\n{content}\n")

        try:
            data = json.loads(content)
            if not any(data.values()):
                return {"status": "error", "message": "No extractable financial information found in the document."}
            return data
        except json.JSONDecodeError as e:
            print(f"❌ JSON parsing failed: {e}")
            return {"status": "error", "message": "AI service unavailable."}

    def extract_fields_page(args):
        idx, page_text = args
        print(f"🔍 Extracting from Page {idx}...")
        result = extract_fields(page_text)
        if not isinstance(result, dict) or result.get("status") == "error":
            print(f"⚠️ Page {idx} extraction error: {result.get('message', 'unknown')}")
            return None
        result["page"] = idx
        result["raw_text"] = page_text  # เพิ่มข้อความดิบ
        return result

    pages = re.split(r"\n📄 Page \d+\n", ocr_text)
    pages = [p.strip() for p in pages if p.strip()]

    if not pages:
        error_msg = {"status": "error", "message": "No pages detected in OCR text."}
        print(f"⚠️ {error_msg['message']}")
        return error_msg

    tasks = list(enumerate(pages, start=1))
    all_results = []

    # ---- ThreadPoolExecutor ----
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(extract_fields_page, t): t for t in tasks}
        for fut in as_completed(futures):
            res = fut.result()
            if res:
                all_results.append(res)

    all_results.sort(key=lambda x: x["page"])

    if not all_results:
        error_msg = {"status": "error", "message": "No extractable financial information found in the document."}
        print(f"⚠️ {error_msg['message']}")
        return error_msg

    # Display result
    print("\n📑 Extracted All Financial Data:")
    for receipt in all_results:
        print(json.dumps(receipt, ensure_ascii=False, indent=2))

    return all_results

# POST method for process file
# Document type

def Classify_Document_Type(structured_data: dict) -> str:
    text = structured_data.get("raw_text", "").lower()
    """
    Analyzes the structured financial data and assigns a document bill type
    (e.g. “ใบเสร็จ”, “ใบแจ้งหนี้”) based on content or patterns such as keywords
    in OCR text. If no category matches, it defaults to “เอกสารที่เกี่ยวข้อง”.
    """
    
    if any(keyword in text for keyword in ['ตั้งหนี้', 'ตั้งเบิก', 'ขอเบิก']):
        return 'ใบตั้งหนี้'
    elif any(keyword in text for keyword in ['ส่งของ', 'ใบกำกับภาษี']):
        return 'ใบส่งของ/ใบกำกับภาษี'
    elif any(keyword in text for keyword in ['สัญญา', 'ว่าจ้าง', 'เงื่อนไขการจ้าง']):
        return 'สัญญาจ้าง'
    elif any(keyword in text for keyword in ['โอนสิทธิ', 'สิทธิเรียกร้อง']):
        return 'เอกสารการโอนสิทธิ์'
    elif any(keyword in text for keyword in ['อนุมัติ', 'เสนอเพื่ออนุมัติ']):
        return 'เอกสารการขออนุมัติ'
    elif any(keyword in text for keyword in ['ใบเสร็จ', 'รับเงิน', 'ชำระเงิน']):
        return 'ใบเสร็จรับเงิน/หลักฐานการจ่ายเงิน'
    elif any(keyword in text for keyword in ['แจ้งหนี้', 'ต้องชำระ']):
        return 'ใบแจ้งหนี้'
    else:
        return 'Unknown'

# POST method for process file

@app.route("/process", methods=["POST"])
def process_file():
    if "file" not in request.files:
        return jsonify({"status": "error", "message": "No file part"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"status": "error", "message": "No selected file"}), 400

    # Save ไฟล์ชั่วคราว
    temp_dir = tempfile.gettempdir()
    save_path = os.path.join(temp_dir, file.filename)
    file.save(save_path)

    # เรียก validate ส่วนที่เหลือ (ไม่ต้อง pass user_id, email)
    result = Upload_And_Validate_File(save_path)
    if result["status"] != "success":
        os.remove(save_path)
        return jsonify(result), 400

    # 2. OCR + Text Correction
    ocr_text = Read_Text_From_File(save_path)
    os.remove(save_path)

    if isinstance(ocr_text, dict) and ocr_text.get("Status") == "error":
        return jsonify(ocr_text), 500

    # 3. Interpret structured financial fields
    interpreted_data = Interpret_Financial_Fields(ocr_text)
    if isinstance(interpreted_data, dict) and interpreted_data.get("status") == "error":
        return jsonify(interpreted_data), 500

    # 4. Classify document type
    for page_data in interpreted_data:
        classified_type = Classify_Document_Type(page_data)
        page_data["document_type"] = classified_type

    return jsonify(interpreted_data), 200


def resolve_doc_type_id(doc_type_name: str) -> int | None:
    conn = get_mysql_connection()
    with conn.cursor() as cursor:
        cursor.execute("SELECT DocTypeID FROM DocType WHERE DocTypeName = %s", (doc_type_name,))
        result = cursor.fetchone()
    conn.close()
    return result["DocTypeID"] if result else None

@app.route("/save", methods=["POST"])
def save_extracted():
    """
    Save only to ExtractedData table (not UploadFiles)
    """
    payload = request.get_json()
    if not isinstance(payload, dict):
        return jsonify({"status": "error", "message": "Invalid payload"}), 400

    user_id         = payload.get("user_id")
    filename        = payload.get("filename")
    image_path      = payload.get("image_path")
    structured_data = payload.get("structured_data")

    if not (user_id and filename and image_path and isinstance(structured_data, list)):
        return jsonify({"status": "error", "message": "Missing or invalid keys"}), 400

    try:
        conn = get_mysql_connection()
        with conn.cursor() as cursor:
            # ❗ไม่บันทึก UploadFiles แล้ว

            # ✅ บันทึก ExtractedData พร้อม FilePath
            insert_data_sql = """
                INSERT INTO ExtractedData
                (PageNumber, BillNumber, DocTypeID, SupplierName, Amount, PaymentDate, Signature, FileID, FilePath)
                VALUES (%s, %s, %s,        %s,           %s,     %s,          %s,        %s,       %s)
            """
            file_id = 1  # ใช้ mock ไปก่อน (เช่น demo.pdf ที่อยู่ใน UploadFiles)

            values = []
            for doc in structured_data:
                raw_amt = doc.get("amount")
                try:
                    amt_val = float(str(raw_amt).replace(",", "")) if raw_amt else None
                except ValueError:
                    amt_val = None

                doc_type_id = resolve_doc_type_id(doc.get("document_type"))
                if doc_type_id is None:
                    doc_type_id = None

                values.append((
                    doc.get("page"),
                    doc.get("bill_number"),
                    doc_type_id,
                    doc.get("supplier_name"),
                    amt_val,
                    doc.get("payment_date"),
                    doc.get("signature"),
                    file_id,
                    image_path  # ✅ เก็บ path ไว้ตรงนี้
                ))

            if values:
                cursor.executemany(insert_data_sql, values)

        conn.commit()
        conn.close()

        preview = {
            "file_id": file_id,
            "filename": filename,
            "page_count": len(structured_data),
            "preview_data": structured_data[:2]
        }
        return jsonify({"status": "success", "preview": preview}), 200

    except Exception as e:
        print("❌ Error saving to MySQL:", e)
        return jsonify({"status": "error", "message": "Failed to store data."}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5001, use_reloader=False)

