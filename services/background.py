from threading import Thread
import os
import uuid
import base64
import traceback
from datetime import datetime, timezone

from .job_store import job_store, completed_unconfirmed_tasks, utc_now_iso
from .ocr import upload_and_validate_file, read_text_from_file
from .extraction import extract_financial_data, classify_document_type
from .database import get_mysql_connection
from .document_types import resolve_document_type_id as resolve_doc_type_id


def background_process(task_id, file_bytes, filename, user_id=None, user_email=None):
    try:
        job_store[task_id] = {
            "status": "processing",
            "message": f"{filename} is being processed.",
            "timestamp": utc_now_iso()
        }

        import tempfile
        temp_dir = tempfile.gettempdir()
        save_path = os.path.join(temp_dir, filename)

        with open(save_path, "wb") as f:
            f.write(file_bytes)

        result = upload_and_validate_file(save_path)
        if result["status"] != "success":
            os.remove(save_path)
            job_store[task_id] = {
                "status": "error",
                "message": result["message"],
                "timestamp": utc_now_iso()
            }
            print(f"---- Upload and validation done for {filename} ----")
            return

        uploader_id = None
        try:
            conn = get_mysql_connection()
            with conn.cursor() as cursor:
                if user_email:
                    cursor.execute(
                        """
                        SELECT id FROM users
                        WHERE LOWER(email)=LOWER(%s)
                        LIMIT 1
                        """,
                        (user_email,)
                    )
                    row = cursor.fetchone()
                    uploader_id = int(row['id']) if row else None
                if uploader_id is None and user_id:
                    cursor.execute("SELECT id FROM users WHERE id=%s LIMIT 1", (user_id,))
                    row = cursor.fetchone()
                    uploader_id = int(row['id']) if row else None
        finally:
            try:
                conn.close()
            except Exception:
                pass

        uploads_dir = os.path.join(os.getcwd(), "uploads")
        os.makedirs(uploads_dir, exist_ok=True)
        safe_name = (filename or "file").replace("/", "_").replace("\\", "_")
        unique_name = f"{uuid.uuid4().hex}_{safe_name}"
        abs_saved_path = os.path.join(uploads_dir, unique_name)
        with open(abs_saved_path, "wb") as outf:
            outf.write(file_bytes)
        saved_file_path = f"/uploads/{unique_name}"
        file_size_bytes = os.path.getsize(abs_saved_path)
        file_ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else 'bin'

        if not uploader_id:
            job_store[task_id] = {
                "status": "error",
                "message": "Uploader not resolved. Please login and try again.",
                "timestamp": utc_now_iso()
            }
            return

        try:
            conn = get_mysql_connection()
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO uploadfiles
                    (uploaded_by, reviewed_by, FileName, FileFormat, FileSize, UploadDatetime, FilePath, OCRText, UploadStatus)
                    VALUES (%s, %s, %s, %s, %s, NOW(), %s, %s, %s)
                    """,
                    (
                        uploader_id, None, filename, file_ext, file_size_bytes,
                        saved_file_path, None, 'pending'
                    )
                )
                file_id = cursor.lastrowid
            conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass

        ocr_text = read_text_from_file(save_path)

        with open(save_path, "rb") as f:
            pdf_bytes = f.read()
            encoded_pdf = base64.b64encode(pdf_bytes).decode("utf-8")

        os.remove(save_path)

        if isinstance(ocr_text, dict) and ocr_text.get("Status") == "error":
            job_store[task_id] = {
                "status": "error",
                "message": ocr_text["message"],
                "timestamp": datetime.now().isoformat()
            }
            return

        interpreted_data = extract_financial_data(ocr_text)
        if isinstance(interpreted_data, dict) and interpreted_data.get("status") == "error":
            job_store[task_id] = {
                "status": "error",
                "message": interpreted_data["message"],
                "timestamp": datetime.now().isoformat()
            }
            return

        for page_data in interpreted_data:
            classified_type = classify_document_type(page_data)
            page_data["document_type"] = classified_type

        # Build display name: <doc_type> : <description>
        display_name = filename
        try:
            primary = next((d for d in interpreted_data if d.get('description')), None) or (interpreted_data[0] if interpreted_data else None)
            if primary:
                dt = (primary.get('document_type') or '').strip()
                desc = (primary.get('description') or '').strip()
                if dt and desc:
                    display_name = f"{dt} : {desc}"
        except Exception:
            pass

        def parse_thai_date(date_text: str) -> str | None:
            if not date_text:
                return None
            try:
                months = {
                    'มกราคม': 1, 'กุมภาพันธ์': 2, 'มีนาคม': 3, 'เมษายน': 4, 'พฤษภาคม': 5, 'มิถุนายน': 6,
                    'กรกฎาคม': 7, 'สิงหาคม': 8, 'กันยายน': 9, 'ตุลาคม': 10, 'พฤศจิกายน': 11, 'ธันวาคม': 12
                }
                parts = str(date_text).strip().split()
                if len(parts) >= 3:
                    day = int(parts[0])
                    month = months.get(parts[1], None)
                    year = int(parts[2])
                    if year > 2400:
                        year -= 543
                    if month:
                        return f"{year:04d}-{month:02d}-{day:02d}"
                return None
            except Exception:
                return None

        try:
            conn = get_mysql_connection()
            with conn.cursor() as cursor:
                insert_sql = (
                    """
                    INSERT INTO StagedExtractedData
                    (FileID, PageNumber, BillNumber, SupplierName, Amount, PaymentDate, PaymentDateText, Signature, DocTypeID, FilePath, RawText, CreatedBy)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE
                      BillNumber=VALUES(BillNumber),
                      SupplierName=VALUES(SupplierName),
                      Amount=VALUES(Amount),
                      PaymentDate=VALUES(PaymentDate),
                      PaymentDateText=VALUES(PaymentDateText),
                      Signature=VALUES(Signature),
                      DocTypeID=VALUES(DocTypeID),
                      FilePath=VALUES(FilePath),
                      RawText=VALUES(RawText),
                      UpdatedAt=NOW(),
                      Version=Version+1
                    """
                )

                for doc in interpreted_data:
                    raw_amt = doc.get("amount")
                    try:
                        amt_val = float(str(raw_amt).replace(",", "")) if raw_amt else None
                    except ValueError:
                        amt_val = None

                    doc_type_id = resolve_doc_type_id(doc.get("document_type"))
                    pay_text = doc.get("payment_date")
                    pay_date = parse_thai_date(pay_text)

                    cursor.execute(
                        insert_sql,
                        (
                            file_id,
                            int(doc.get("page") or 1),
                            doc.get("bill_number"),
                            doc.get("supplier_name"),
                            amt_val,
                            pay_date,
                            pay_text,
                            doc.get("signature"),
                            doc_type_id,
                            saved_file_path,
                            doc.get("raw_text"),
                            uploader_id or None,
                        )
                    )
            conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass

        job_store[task_id] = {
            "status": "complete",
            "message": f"{filename} is successfully processed. Please confirm the information.",
            "timestamp": utc_now_iso(),
            "result": interpreted_data,
            "file_base64": encoded_pdf,
            "filename": filename,
            "display_name": display_name,
            "file_id": file_id
        }
        completed_unconfirmed_tasks[task_id] = job_store[task_id]

        print(f"---- Background job finished for task_id: {task_id} ----")

    except Exception as e:
        traceback.print_exc()
        job_store[task_id] = {
            "status": "error",
            "message": f"Server error: {str(e)}",
            "timestamp": utc_now_iso()
        }
        print(f"---- Exception in background_process: {e} ----")


