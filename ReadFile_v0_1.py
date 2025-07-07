from pdf2image import convert_from_path
from openai import OpenAI
import json
import re
from dotenv import load_dotenv
import os
from typing import List, Tuple
import numpy as np
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import as_completed
import easyocr
reader = easyocr.Reader(['th', 'en'], gpu=True)
import tempfile
import pymysql
from pymysql.cursors import DictCursor

from flask import Flask
from flask_cors import CORS
from flask import request
from flask import jsonify

from threading import Thread
import uuid, traceback
from datetime import datetime
import base64

# %% [markdown]
# #### API key

# %%
# API_KEY AI
load_dotenv(override=True)

api_key = os.getenv("OPENAI_API_KEY")
assert api_key, "Cannot find OPENAI_API_KEY in .env"
print("OpenAI Key Loaded")

# DATABASE
MYSQL_HOST     = os.getenv("MYSQL_HOST","localhost")
MYSQL_PORT     = int(os.getenv("MYSQL_PORT","3306"))
MYSQL_USER     = os.getenv("MYSQL_USER","root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD","")
MYSQL_DB       = os.getenv("MYSQL_DB","famis_db")

assert MYSQL_PASSWORD, "Cannot find MYSQL_PASSWORD in .env"
print("MySQL Settings Loaded")

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
    api_key=api_key
)

# ทดสอบเรียกร้องโมเดลดูว่าได้สิทธิ์ไหม
response = client.chat.completions.create(
    model="gpt-4o",  
    messages=[{"role":"user","content":"สวัสดี"}],
    temperature=0,
    timeout=60
)
print(response.choices[0].message.content)

# %%
POPPLER_PATH = r"C:/Users/Ned/Desktop/Poppler/poppler-24.08.0/Library/bin"

# %% [markdown]
# ## Start flask

# %%
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "http://localhost:5173"}})
job_store = {}
completed_unconfirmed_tasks = {}

# %% [markdown]
# ### Validate file format and size
# - Upload and Validate

# %%
def upload_and_validate_file(file_path: str) -> dict:
    try:
        # 1. Check file existence
        if not os.path.exists(file_path):
            return {"status": "error", "message": "File does not exist."}

        # 2. Check file extension
        allowed_extensions = {'.pdf', '.jpg', '.png', '.zip'}
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in allowed_extensions:
            return {
                "status": "error",
                "message": "Unsupported file format. Accepted formats are .pdf, .jpg, .png, or .zip."
            }

        # 3. Check file size ≤ 25MB
        max_size_bytes = 25 * 1024 * 1024
        file_size = os.path.getsize(file_path)
        if file_size > max_size_bytes:
            return {
                "status": "error",
                "message": "File is too large. File size should not exceed 25MB."
            }

        return {"status": "success", "filename": file_path}

    except Exception as e:
        # Catch for unexpected server-side issues
        return {
            "status": "error",
            "message": "Upload failed due to server error. Possible causes include connection timeout, internal server failure, or file overload. Please try again later."
        }

# %% [markdown]
# ### Import file to use EasyOCR
# - Change file -> img
# - Correction text by AI

# %%
def read_text_from_file(file_path: str) -> str | dict:
    logging.info(f"📥 Reading file: {file_path}")
    try:
        images = convert_from_path(file_path, dpi=200,poppler_path=POPPLER_PATH)
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
                model="gpt-4o",  # ← เปลี่ยนตรงนี้
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

# %% [markdown]
# ### Extract key field by use AI

# %%
def extract_financial_data(ocr_text: str) -> list | dict:
    # System prompt for extraction
    system_prompt = (
        "You are an AI assistant that extracts key information from Thai government "
        "financial documents using OCR text. Please extract the following fields and "
        "return only in JSON format:\n\n"
        "- bill_number: เลขที่ใบขอซื้อหรือเลขที่ มักจะอยู่มุมขวา เช่น 10778\n"
        "- supplier_name: หน่วยงานหรือชื่อผู้ขาย หรือ ชื่อซัพพลายเออร์\n"
        "- amount: ยอดรวมสุทธิที่อยู่ใกล้คำว่า 'รวมทั้งสิ้น', 'ยอดรวม', 'รวมจำนวนเงิน', 'รวม' เอาแต่ตัวเลขเท่านั้น\n"
        "- payment_date: วันที่ใด ๆ ในเอกสาร ดึงออกมาเป็น format เช่น 01 มกราคม 2568\n"
        "- signature: ชื่อจริงนามสกุลที่อยู่ใกล้คำว่า 'อนุมัติ' หรือ 'ผู้เบิก' ตัดคำนำหน้าชื่อเหลือแค่ชื่อและนามสกุล\n\n"
        "If any field is not found, use null. Respond in JSON format only without any explanation."
    )

    def extract_fields(text: str) -> dict:
        # User prompt with the raw OCR text
        user_prompt = f"OCR Text in Thai:\n{text}\n\nPlease return the result in JSON only."
        try:
            response = client.chat.completions.create(
                model="gpt-4o",    # ← use GPT-4o
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_prompt}
                ],
                temperature=0
            )
        except Exception as e:
            print(f"❌ AI service error: {e}")
            return {"status": "error", "message": "AI service unavailable."}

        # Get content and strip markdown fences if present
        content = response.choices[0].message.content.strip()
        if content.startswith("```json"):
            content = content.removeprefix("```json").removesuffix("```").strip()
        elif content.startswith("```"):
            content = content.removeprefix("```").removesuffix("```").strip()

        # Parse JSON
        try:
            data = json.loads(content)
            if not any(data.values()):
                return {"status": "error", "message": "No extractable financial information found in the document."}
            return data
        except json.JSONDecodeError as e:
            print(f"❌ JSON parsing failed: {e}")
            return {"status": "error", "message": "AI service unavailable."}

    def extract_fields_page(args: tuple[int, str]) -> dict | None:
        idx, page_text = args
        print(f"🔍 Extracting from Page {idx}...")
        result = extract_fields(page_text)
        if not isinstance(result, dict) or result.get("status") == "error":
            print(f"⚠️ Page {idx} extraction error: {result.get('message', 'unknown')}")
            return None
        result["page"]     = idx
        result["raw_text"] = page_text
        return result

    # Split OCR output into pages
    pages = re.split(r"\n📄 Page \d+\n", ocr_text)
    pages = [p.strip() for p in pages if p.strip()]
    if not pages:
        msg = {"status": "error", "message": "No pages detected in OCR text."}
        print(f"⚠️ {msg['message']}")
        return msg

    # Run extraction in parallel
    tasks      = list(enumerate(pages, start=1))
    all_results: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(extract_fields_page, t): t for t in tasks}
        for fut in as_completed(futures):
            res = fut.result()
            if res:
                all_results.append(res)

    # Sort by page number
    all_results.sort(key=lambda x: x["page"])
    if not all_results:
        msg = {"status": "error", "message": "No extractable financial information found in the document."}
        print(f"⚠️ {msg['message']}")
        return msg

    # Debug print
    print("\n📑 Extracted All Financial Data:")
    for receipt in all_results:
        print(json.dumps(receipt, ensure_ascii=False, indent=2))

    return all_results

# %% [markdown]
# ### Document type

# %%
def classify_document_type(structured_data: dict) -> str:
    text = structured_data.get("raw_text", "").lower()

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

# %%
def background_process(task_id, file_bytes, filename):
    try:

        job_store[task_id] = {
            "status": "processing",
            "message": f"{filename} is being processed.",
            "timestamp": datetime.now().isoformat()
        }

        temp_dir = tempfile.gettempdir()
        save_path = os.path.join(temp_dir, filename)

        with open(save_path, "wb") as f:
            f.write(file_bytes)

        # 1. Upload_And_Validate_File
        result = upload_and_validate_file(save_path)
        if result["status"] != "success":
            os.remove(save_path)
            job_store[task_id] = {
                "status": "error",
                "message": result["message"],
                "timestamp": datetime.now().isoformat()
            }
            print(f"---- Upload and validation done for {filename} ----")
            return

        # 2. Read_Text_From_File (OCR)
        ocr_text = read_text_from_file(save_path)

        # อ่านไฟล์ PDF เป็น base64
        with open(save_path, "rb") as f:
            pdf_bytes = f.read()
            encoded_pdf = base64.b64encode(pdf_bytes).decode("utf-8")

        # ลบไฟล์หลังใช้งาน
        os.remove(save_path)

        if isinstance(ocr_text, dict) and ocr_text.get("Status") == "error":
            job_store[task_id] = {
                "status": "error",
                "message": ocr_text["message"],
                "timestamp": datetime.now().isoformat()
            }
            return

        # 3. Interpret_Financial_Fields
        interpreted_data = extract_financial_data(ocr_text)
        if isinstance(interpreted_data, dict) and interpreted_data.get("status") == "error":
            job_store[task_id] = {
                "status": "error",
                "message": interpreted_data["message"],
                "timestamp": datetime.now().isoformat()
            }
            return

        # 4. Classify_Document_Type
        for page_data in interpreted_data:
            classified_type = classify_document_type(page_data)
            page_data["document_type"] = classified_type

        job_store[task_id] = {
            "status": "complete",
            "message": f"{filename} is successfully processed. Please confirm the information.",
            "timestamp": datetime.now().isoformat(),
            "result": interpreted_data,
            "file_base64": encoded_pdf,
            "filename": filename
        }
        completed_unconfirmed_tasks[task_id] = job_store[task_id]

        print(f"---- Background job finished for task_id: {task_id} ----")

    except Exception as e:
        traceback.print_exc()
        job_store[task_id] = {
            "status": "error",
            "message": f"Server error: {str(e)}",
            "timestamp": datetime.now().isoformat()
        }
        print(f"---- Exception in background_process: {e} ----")


# %%
# Find DocTypeID from document type name from MySQL database.
def resolve_doc_type_id(doc_type_name: str) -> int | None:
    conn = get_mysql_connection()
    with conn.cursor() as cursor:
        cursor.execute("SELECT DocTypeID FROM DocType WHERE DocTypeName = %s", (doc_type_name,))
        result = cursor.fetchone()
    conn.close()
    return result["DocTypeID"] if result else None

# %%
@app.route('/doc-types', methods=['GET'])
def get_doc_types():
    try:
        conn = get_mysql_connection()
        with conn.cursor() as cursor:
            cursor.execute("SELECT DocTypeID, DocTypeName FROM DocType")
            result = cursor.fetchall()
        conn.close()
        return jsonify({
            "status": "success",
            "doc_types": result
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

# %% [markdown]
# ## POST method for process file

# %%
@app.route("/process", methods=["POST"])
def process_file():
    print(">>> /process endpoint called")
    if "file" not in request.files:
        print(">>> No file part in request")
        return jsonify({"status": "error", "message": "No file part"}), 400

    file = request.files["file"]
    if file.filename == "":
        print(">>> No selected file")
        return jsonify({"status": "error", "message": "No selected file"}), 400

    file_bytes = file.read()
    task_id = str(uuid.uuid4())
    job_store[task_id] = {"status": "processing"}

    thread = Thread(target=background_process, args=(task_id, file_bytes, file.filename))
    thread.start()

    print(f">>> Started background thread with task_id: {task_id}")

    return jsonify({"status": "submitted", "task_id": task_id})


# %%
@app.route("/status/<task_id>", methods=["GET"])
def get_task_status(task_id):
    job = job_store.get(task_id)
    print(f"STATUS CHECK for {task_id}: {job}")

    if not job:
        return jsonify({"status": "error", "message": "Task not found"}), 404

    if job["status"] == "complete":
        return jsonify({
            "status": "complete",
            "message": job["message"],
            "timestamp": job["timestamp"],
            "result": job["result"],
            #"file_base64": job["file_base64"],
            "filename": job["filename"]
        })

    return jsonify(job)

# %%
@app.route("/task-board", methods=["GET"])
def get_pending_tasks_for_user():
    return jsonify({
        "status": "success",
        "data": list(completed_unconfirmed_tasks.values())
    })

# %%
@app.route("/task-result/<task_id>", methods=["GET"])
def get_task_result_details(task_id):
    job = job_store.get(task_id)
    if not job:
        return jsonify({"status": "error", "message": "Task not found"}), 404
    if job.get("status") != "complete":
        return jsonify({"status": "error", "message": "Task not complete"}), 400
    # ส่งกลับเฉพาะข้อมูลที่ extract มาแล้ว
    return jsonify({"status": "success", "data": job["result"]}), 200

# %%
@app.route("/confirm-task", methods=["POST"])
def confirm_task_data():
    payload = request.get_json()
    task_id = payload.get("task_id")
    if not task_id:
        return jsonify({"status": "error", "message": "No task_id"}), 400

    # หา task ใน completed_unconfirmed_tasks
    task = completed_unconfirmed_tasks.get(task_id)
    if not task:
        return jsonify({"status": "error", "message": "Task not found"}), 404

    try:
        conn = get_mysql_connection()
        with conn.cursor() as cursor:
            # สมมุติว่ามี Table ConfirmedTasks
            sql = """
                INSERT INTO ConfirmedTasks
                (TaskID, FileName, ConfirmedAt)
                VALUES (%s, %s, NOW())
            """
            cursor.execute(sql, (task_id, task["filename"]))

        conn.commit()
        conn.close()

        # ลบจาก memory หลัง save DB สำเร็จ
        completed_unconfirmed_tasks.pop(task_id, None)
        job_store.pop(task_id, None)

        return jsonify({"status": "success", "message": "Success record"}), 200

    except Exception as e:
        print("❌ Confirm Task Error:", e)
        return jsonify({"status": "error", "message": "Failed to store data. Please try again later."}), 500


# %%
@app.route("/update", methods=["POST"])
def update_extracted_data():
    payload = request.get_json()
    if not isinstance(payload, dict):
        return jsonify({"status": "error", "message": "Invalid payload"}), 400

    task_id     = payload.get("task_id")
    file_id     = payload.get("file_id")
    edited_data = payload.get("edited_data")   # คาดว่าเป็น list ของ dict ที่มี keys: page, bill_number, document_type, supplier_name, amount, payment_date, signature

    if not (task_id and file_id and isinstance(edited_data, list)):
        return jsonify({"status": "error", "message": "Missing or invalid keys"}), 400

    try:
        conn = get_mysql_connection()
        with conn.cursor() as cursor:
            update_sql = """
                UPDATE ExtractedData
                SET BillNumber     = %s,
                    DocTypeID      = %s,
                    SupplierName   = %s,
                    Amount         = %s,
                    PaymentDate    = %s,
                    Signature      = %s
                WHERE FileID = %s AND PageNumber = %s
            """
            for doc in edited_data:
                bill         = doc.get("bill_number")
                doc_type_id  = resolve_doc_type_id(doc.get("document_type"))
                supplier     = doc.get("supplier_name")
                raw_amt      = doc.get("amount")
                try:
                    amt_val = float(str(raw_amt).replace(",", "")) if raw_amt is not None else None
                except ValueError:
                    amt_val = None
                payment_date = doc.get("payment_date")
                signature    = doc.get("signature")
                page         = doc.get("page")

                cursor.execute(
                    update_sql,
                    (bill, doc_type_id, supplier, amt_val, payment_date, signature, file_id, page)
                )

        conn.commit()
        conn.close()

        return jsonify({"status": "success", "message": "Data updated"}), 200

    except Exception as e:
        print("❌ Error updating data:", e)
        return jsonify({"status": "error", "message": "Failed to update data"}), 500

# %% [markdown]
# ## POST method for save only to ExtractedData table (not UploadFiles)
# 

# %%
@app.route("/save", methods=["POST"])
def save_extracted_data():

    payload = request.get_json()
    if not isinstance(payload, dict):
        return jsonify({"status": "error", "message": "Invalid payload"}), 400

    user_id         = payload.get("user_id")
    filename        = payload.get("filename")
    image_path      = payload.get("image_path")
    structured_data = payload.get("structured_data")
    task_id         = payload.get("task_id")

    if not (user_id and filename and image_path and isinstance(structured_data, list)):
        return jsonify({"status": "error", "message": "Missing or invalid keys"}), 400

    try:
        conn = get_mysql_connection()
        with conn.cursor() as cursor:


            # SQL for insert data -> ExtractedData
            insert_data_sql = """
                INSERT INTO ExtractedData
                (PageNumber, BillNumber, DocTypeID, SupplierName, Amount, PaymentDate, Signature, FileID, FilePath)
                VALUES (%s, %s, %s,        %s,           %s,     %s,          %s,        %s,       %s)
            """

            # ----- Use mock data -------
            file_id = 1
            # ---------------------------

            values = []
            for doc in structured_data:
                raw_amt = doc.get("amount")

                # String -> Float
                try:
                    amt_val = float(str(raw_amt).replace(",", "")) if raw_amt else None
                except ValueError:
                    amt_val = None

                # Find DocTypeID from document type name
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
                    image_path
                ))

            if values:
                cursor.executemany(insert_data_sql, values)

        conn.commit()
        conn.close()

        if task_id:
            completed_unconfirmed_tasks.pop(task_id, None)
            job_store.pop(task_id, None)

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

# %%
if __name__ == "__main__":
    app.run(debug=False, port=5000, use_reloader=False)


