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
            "timestamp": utc_now_iso(),
            # Best-effort early linkage for debugging; finalized after uploader resolution
            "uploaded_by": None,
            "uploader_email": user_email,
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
            print(f"✗ [ERROR] Upload validation failed: {filename} - {result['message']}")
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

        # Update processing job with resolved uploader info
        try:
            job = job_store.get(task_id) or {}
            job["uploaded_by"] = uploader_id
            if user_email:
                job["uploader_email"] = user_email
            job_store[task_id] = job
        except Exception:
            pass

        try:
            conn = get_mysql_connection()
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO uploadfiles
                    (uploaded_by, reviewed_by, FileName, FileFormat, FileSize, UploadDatetime, FilePath, OCRText, Confirmed)
                    VALUES (%s, %s, %s, %s, %s, NOW(), %s, %s, %s)
                    """,
                    (
                        uploader_id, None, filename, file_ext, file_size_bytes,
                        saved_file_path, None, 'Unconfirmed'
                    )
                )
                file_id = cursor.lastrowid
            conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass

        # Attach file_id to the processing job so the task-board can hide DB rows while processing
        try:
            job = job_store.get(task_id) or {}
            job["file_id"] = file_id
            job_store[task_id] = job
        except Exception:
            pass

        # Notify: OCR processing started
        try:
            conn = get_mysql_connection()
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO notifications (event_type, title, body, actor_id, audience)
                    VALUES (%s, %s, %s, %s, 'direct')
                    """,
                    ('general', 'OCR started', f'OCR processing started for {filename}', uploader_id)
                )
                cursor.execute("SELECT LAST_INSERT_ID() AS nid")
                row = cursor.fetchone()
                nid = int(row.get('nid')) if row else None
                if nid:
                    cursor.execute(
                        "INSERT INTO notification_recipients (notification_id, user_id) VALUES (%s, %s)",
                        (nid, uploader_id)
                    )
            conn.commit()
            conn.close()
        except Exception:
            try:
                conn.rollback()
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

        # Persist extracted data immediately so it can be viewed later (even after server restarts)
        try:
            conn = get_mysql_connection()
            with conn.cursor() as cursor:
                # Ensure any old rows are cleared (should normally be none for a new upload)
                try:
                    cursor.execute("DELETE FROM ExtractedData WHERE FileID=%s", (file_id,))
                except Exception:
                    pass

                insert_sql = (
                    """
                    INSERT INTO ExtractedData
                    (FileID, BillNumber, Amount, SupplierName, PaymentDate, Signature, DocTypeID, PageNumber, ExtractedAt, FilePath)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s)
                    """
                )

                for doc in interpreted_data:
                    # Amount is already normalized to format like "20000.00"
                    raw_amt = doc.get("amount")
                    try:
                        # Convert to Decimal for database storage (preserves .00)
                        from decimal import Decimal
                        if raw_amt is not None:
                            # Remove commas if any and convert
                            amt_str = str(raw_amt).replace(",", "")
                            amt_val = Decimal(amt_str)
                        else:
                            amt_val = None
                    except Exception:
                        amt_val = None
                    
                    # Date is already normalized to format like "02 พฤษภาคม 2568"
                    payment_date = doc.get("payment_date")
                    
                    doc_type_id = resolve_doc_type_id(doc.get("document_type"))
                    cursor.execute(
                        insert_sql,
                        (
                            file_id,
                            doc.get("bill_number"),
                            amt_val,
                            doc.get("supplier_name"),
                            payment_date,
                            doc.get("signature"),
                            doc_type_id,
                            int(doc.get("page") or 1),
                            saved_file_path,
                        ),
                    )
            conn.commit()
            conn.close()
        except Exception:
            try:
                conn.rollback()
                conn.close()
            except Exception:
                pass

        # Notify: extraction completed successfully
        try:
            conn = get_mysql_connection()
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO notifications (event_type, title, body, actor_id, audience)
                    VALUES (%s, %s, %s, %s, 'direct')
                    """,
                    ('task_completed', 'Extract successful', f'Extract file ({filename}) successful', uploader_id)
                )
                cursor.execute("SELECT LAST_INSERT_ID() AS nid")
                row = cursor.fetchone()
                nid = int(row.get('nid')) if row else None
                if nid:
                    cursor.execute(
                        "INSERT INTO notification_recipients (notification_id, user_id) VALUES (%s, %s)",
                        (nid, uploader_id)
                    )
            conn.commit()
            conn.close()
        except Exception:
            try:
                conn.rollback()
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
            "file_id": file_id,
            "uploaded_by": uploader_id,
            "uploader_email": user_email,
        }
        completed_unconfirmed_tasks[task_id] = job_store[task_id]

        print(f"✓ [COMPLETE] File processed: {filename} (task_id: {task_id})")
        print(f"  - Pages extracted: {len(interpreted_data)}")
        print(f"  - File ID: {file_id}")

    except Exception as e:
        traceback.print_exc()
        job_store[task_id] = {
            "status": "error",
            "message": f"Server error: {str(e)}",
            "timestamp": utc_now_iso()
        }
        print(f"✗ [EXCEPTION] Background process failed: {str(e)}")


