import os
import base64
import traceback
from flask import Flask, request, jsonify, send_from_directory, abort
from flask_cors import CORS
from threading import Thread

from .job_store import job_store, completed_unconfirmed_tasks
from .database import get_mysql_connection
from .background import background_process
from .ocr import upload_and_validate_file, read_text_from_file
from .extraction import extract_financial_data, classify_document_type
from .document_types import resolve_document_type_id as resolve_doc_type_id


def create_app() -> Flask:
    app = Flask(__name__)
    CORS(app, resources={r"/*": {"origins": ["http://localhost:5173","http://127.0.0.1:5173","http://localhost:3000","http://127.0.0.1:3000"]}})

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

    @app.route('/admin/users', methods=['GET'])
    def admin_list_users():
        try:
            # Demo error/unauthorized toggles for testing
            if request.headers.get('X-Demo-Force-Unauthorized') == '1':
                return jsonify({"status": "error", "message": "Unauthorized access."}), 403
            if request.headers.get('X-Demo-Force-DbError') == '1':
                return jsonify({"status": "error", "message": "Unable to retrieve user list from database. Please try again later."}), 500

            role_filter = (request.args.get('role') or '').strip().lower()
            page_param = request.args.get('page')

            conn = get_mysql_connection()
            with conn.cursor() as cursor:
                base_sql = (
                    """
                    SELECT id   AS id,
                           email AS email,
                           role  AS role,
                           department AS department,
                           is_approved AS isApproved
                    FROM users
                    """
                )
                params = []
                if role_filter in ('admin','staff','pending'):
                    base_sql += " WHERE role=%s"
                    params.append(role_filter)
                base_sql += " ORDER BY id ASC"
                cursor.execute(base_sql, params)
                rows = cursor.fetchall()
            conn.close()

            users = [
                {
                    "id": r["id"],
                    "email": r["email"],
                    "role": r["role"],
                    "department": (r.get("department") or "Student"),
                    "is_approved": bool(r["isApproved"]) if r.get("isApproved") is not None else None,
                }
                for r in rows
            ]

            if request.headers.get('X-Demo-Force-Empty') == '1':
                users = []

            resp = {"status": "success", "users": users}
            if page_param is not None:
                try:
                    resp["page"] = [int(page_param)]
                except Exception:
                    resp["page"] = [page_param]
            return jsonify(resp)
        except Exception:
            return jsonify({"status": "error", "message": "Unable to retrieve user list from database. Please try again later."}), 500

    @app.route('/admin/users', methods=['POST'])
    def admin_create_user():
        import pymysql
        try:
            if request.headers.get('X-Demo-Force-DbError') == '1':
                return jsonify({"status": "error", "message": "Unable to store data due to server error. Please try again later."}), 500

            payload = request.get_json(silent=True) or {}
            email = (payload.get('email') or '').strip()
            role = (payload.get('role') or 'staff').lower()
            department = (payload.get('department') or 'Student')
            if not email:
                return jsonify({"status": "error", "message": "email required"}), 400
            if not email.endswith('@cmu.ac.th'):
                return jsonify({"status": "error", "message": "Invalid email format. Must end with @cmu.ac.th."}), 400
            if role not in ('admin', 'staff', 'pending'):
                role = 'staff'

            conn = get_mysql_connection()
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO users (email, role, department, is_approved) VALUES (%s, %s, %s, %s)",
                    (email, role, department, 1)
                )
                new_id = cursor.lastrowid
            conn.commit()
            conn.close()
            return jsonify({"status": "success", "message": "The email and role has been stored in the database", "id": new_id}), 201
        except pymysql.err.IntegrityError:
            return jsonify({"status": "error", "message": "This email is already registered."}), 409
        except Exception:
            return jsonify({"status": "error", "message": "Unable to store data due to server error. Please try again later."}), 500

    @app.route('/admin/users/<int:user_id>/role', methods=['PATCH'])
    def admin_change_role(user_id: int):
        try:
            if request.headers.get('X-Demo-Force-Unauthorized') == '1':
                return jsonify({"status": "error", "message": "Unauthorized access."}), 403
            if request.headers.get('X-Demo-Force-DbError') == '1':
                return jsonify({"status": "error", "message": "Unable to update user role due to server error. Please try again later."}), 500

            payload = request.get_json(silent=True) or {}
            role = (payload.get('role') or '').lower()
            # Resolve actor (admin performing the change)
            actor_id = None
            try:
                header_actor = request.headers.get('X-Actor-Id')
                if header_actor is not None:
                    actor_id = int(header_actor)
            except Exception:
                actor_id = None
            if actor_id is None:
                try:
                    body_actor = payload.get('actor_id')
                    if body_actor is not None:
                        actor_id = int(body_actor)
                except Exception:
                    actor_id = None
            if role not in ('admin', 'staff', 'pending'):
                return jsonify({"status": "error", "message": "Invalid role specified."}), 400
            conn = get_mysql_connection()
            with conn.cursor() as cursor:
                # Fetch current user to obtain old role and email
                cursor.execute("SELECT id, email, role FROM users WHERE id=%s LIMIT 1", (user_id,))
                current_user = cursor.fetchone()
                if not current_user:
                    conn.close()
                    return jsonify({"status": "error", "message": "Selected user cannot be found. Please refresh the list and try again."}), 404
                old_role = (current_user.get('role') or '').lower() or None
                cursor.execute("UPDATE users SET role=%s WHERE id=%s", (role, user_id))
            conn.commit()
            conn.close()

            # Write audit log and notification (best-effort)
            try:
                conn2 = get_mysql_connection()
                with conn2.cursor() as cursor:
                    # Audit
                    cursor.execute(
                        """
                        INSERT INTO role_change_logs (user_id, old_role, new_role, changed_by, reason)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (user_id, old_role, role, actor_id, None)
                    )
                    # Resolve actor email for message
                    actor_email = 'Admin'
                    if actor_id is not None:
                        cursor.execute("SELECT email FROM users WHERE id=%s LIMIT 1", (actor_id,))
                        a = cursor.fetchone()
                        if a and a.get('email'):
                            actor_email = a.get('email')
                    # Direct notification to affected user
                    notify_title = 'Role updated'
                    notify_body = f"Your role has been changed from {old_role or '-'} to {role} by {actor_email}"
                    cursor.execute(
                        """
                        INSERT INTO notifications (event_type, title, body, actor_id, audience)
                        VALUES ('role_changed', %s, %s, %s, 'direct')
                        """,
                        (notify_title, notify_body, actor_id)
                    )
                    cursor.execute("SELECT LAST_INSERT_ID() AS nid")
                    nid_row = cursor.fetchone()
                    notification_id = int(nid_row.get('nid')) if nid_row else None
                    if notification_id is not None:
                        cursor.execute(
                            """
                            INSERT INTO notification_recipients (notification_id, user_id)
                            VALUES (%s, %s)
                            """,
                            (notification_id, user_id)
                        )
                conn2.commit()
                conn2.close()
            except Exception:
                try:
                    conn2.rollback()
                    conn2.close()
                except Exception:
                    pass

            if request.headers.get('X-Demo-Force-EmailFail') == '1':
                return jsonify({
                    "status": "partial_success",
                    "user_id": user_id,
                    "new_role": role,
                    "message": "Role updated, but failed to send notification email."
                })

            return jsonify({"status": "ok"})
        except Exception:
            return jsonify({"status": "error", "message": "Unable to update user role due to server error. Please try again later."}), 500

    @app.route('/admin/users/<int:user_id>', methods=['DELETE'])
    def admin_delete_user(user_id: int):
        try:
            if request.headers.get('X-Demo-Force-Unauthorized') == '1':
                return jsonify({"status": "error", "message": "Unauthorized access."}), 403
            if request.headers.get('X-Demo-Force-DbError') == '1':
                return jsonify({"status": "error", "message": "Unable to delete user due to server error. Please try again later."}), 500

            conn = get_mysql_connection()
            with conn.cursor() as cursor:
                cursor.execute("SELECT id FROM users WHERE id=%s LIMIT 1", (user_id,))
                found = cursor.fetchone()
                if not found:
                    conn.close()
                    return jsonify({"status": "error", "message": "Selected user cannot be found."}), 404
                cursor.execute("DELETE FROM users WHERE id=%s", (user_id,))
            conn.commit()
            conn.close()
            return jsonify({"status": "ok"})
        except Exception:
            return jsonify({"status": "error", "message": "Unable to delete user due to server error. Please try again later."}), 500

    @app.route('/auth/authorize', methods=['POST'])
    def authorize_user():
        try:
            payload = request.get_json(silent=True) or {}
            email = payload.get('email')
            incoming_department = (payload.get('department') or None)
            if not email:
                return jsonify({"status": "error", "message": "Missing email"}), 400

            conn = get_mysql_connection()
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id        AS UserID,
                           email     AS Email,
                           role      AS Role,
                           department AS Department,
                           is_approved AS IsApproved
                    FROM users
                    WHERE LOWER(email) = LOWER(%s)
                    LIMIT 1
                    """,
                    (email,)
                )
                row = cursor.fetchone()
            conn.close()

            if not row:
                return jsonify({
                    "status": "pending",
                    "message": "Your account is not authorized yet. Please contact administrator.",
                }), 403

            is_approved = bool(row.get("IsApproved"))
            role = (row.get("Role") or "").lower()
            current_department = row.get("Department")

            if not is_approved or role == "pending":
                return jsonify({
                    "status": "pending",
                    "message": "Your account is pending approval.",
                }), 403

            if incoming_department and (incoming_department != current_department):
                conn = get_mysql_connection()
                with conn.cursor() as cursor:
                    cursor.execute(
                        "UPDATE users SET department=%s WHERE LOWER(email)=LOWER(%s)",
                        (incoming_department, email)
                    )
                conn.commit()
                conn.close()
                current_department = incoming_department

            return jsonify({
                "status": "success",
                "user": {
                    "user_id": row.get("UserID"),
                    "email": row.get("Email"),
                    "role": role,
                    "department": current_department or "Student",
                }
            }), 200

        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

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
        user_email = request.form.get("email")
        user_id = request.form.get("user_id")
        import uuid as _uuid
        task_id = str(_uuid.uuid4())
        job_store[task_id] = {"status": "processing"}

        thread = Thread(target=background_process, args=(task_id, file_bytes, file.filename, user_id, user_email))
        thread.start()

        print(f">>> Started background thread with task_id: {task_id}")

        return jsonify({"status": "submitted", "task_id": task_id})

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
                "file_base64": job["file_base64"],
                "filename": job["filename"],
                "display_name": job.get("display_name"),
                "file_id": job.get("file_id")
            })

        return jsonify(job)

    @app.route("/task-board", methods=["GET"])
    def get_pending_tasks_for_user():
        # In-memory tasks (pre-confirm, from current process lifetime)
        memory_data = []
        for task_id, payload in completed_unconfirmed_tasks.items():
            item = {"task_id": task_id, **payload}
            if payload.get('display_name'):
                item['filename'] = payload.get('display_name')
            memory_data.append(item)

        # DB-backed staged tasks (persist across restarts)
        # Optional filter by user
        q_user_id = request.args.get('user_id')
        q_email = request.args.get('email')
        db_data = []
        try:
            conn = get_mysql_connection()
            with conn.cursor() as cursor:
                # Resolve user from email if provided
                resolved_id = None
                if q_email and not q_user_id:
                    cursor.execute("SELECT id FROM users WHERE LOWER(email)=LOWER(%s) LIMIT 1", (q_email,))
                    r = cursor.fetchone()
                    resolved_id = int(r['id']) if r else None
                elif q_user_id:
                    try:
                        resolved_id = int(q_user_id)
                    except Exception:
                        resolved_id = None

                base_sql = (
                    """
                    SELECT uf.FileID AS file_id,
                           uf.FileName AS file_name,
                           DATE_FORMAT(CONVERT_TZ(uf.UploadDatetime, @@session.time_zone, '+00:00'), '%Y-%m-%dT%H:%i:%sZ') AS uploaded_at
                    FROM UploadFiles uf
                    INNER JOIN StagedExtractedData s ON s.FileID = uf.FileID
                    WHERE uf.UploadStatus = 'pending'
                    {USER_FILTER}
                    GROUP BY uf.FileID
                    ORDER BY uf.UploadDatetime DESC
                    """
                )
                if resolved_id is not None:
                    sql = base_sql.replace('{USER_FILTER}', 'AND uf.uploaded_by = %s')
                    cursor.execute(sql, (resolved_id,))
                else:
                    sql = base_sql.replace('{USER_FILTER}', '')
                    cursor.execute(sql)
                rows = cursor.fetchall()
                for r in rows:
                    db_data.append({
                        "task_id": f"file:{int(r['file_id'])}",
                        "status": "complete",
                        "message": "This file is processed and waiting for confirmation.",
                        "timestamp": r.get('uploaded_at'),
                        "filename": r.get('file_name')
                    })
            conn.close()
        except Exception:
            try:
                conn.close()
            except Exception:
                pass

        # Merge memory first (newer tasks), then DB-backed staged items
        data = memory_data + db_data
        return jsonify({"status": "success", "data": data})

    @app.route("/task-result/<task_id>", methods=["GET"])
    def get_task_result_details(task_id):
        job = job_store.get(task_id)
        if not job:
            return jsonify({"status": "error", "message": "Task not found"}), 404
        if job.get("status") != "complete":
            return jsonify({"status": "error", "message": "Task not complete"}), 400
        return jsonify({"status": "success", "data": job["result"]}), 200

    @app.route('/tasks/<task_id>', methods=['DELETE'])
    def delete_task_and_data(task_id: str):
        job = job_store.get(task_id) or completed_unconfirmed_tasks.get(task_id)
        if not job:
            return jsonify({"status": "error", "message": "Task not found"}), 404

        file_id = job.get('file_id')
        filename = job.get('filename')
        # Fallback resolution for legacy tasks without file_id
        if not file_id and filename:
            try:
                conn0 = get_mysql_connection()
                with conn0.cursor() as c0:
                    c0.execute("SELECT FileID FROM UploadFiles WHERE FileName=%s ORDER BY UploadDatetime DESC LIMIT 1", (filename,))
                    r0 = c0.fetchone()
                    if r0:
                        file_id = int(r0.get('FileID'))
                conn0.close()
            except Exception:
                file_id = None
        if not file_id:
            # Remove stale memory even if DB delete is skipped
            completed_unconfirmed_tasks.pop(task_id, None)
            job_store.pop(task_id, None)
            return jsonify({"status": "success", "message": "Task cleared from memory (no file found)"})

        try:
            conn = get_mysql_connection()
            file_path = None
            with conn.cursor() as cursor:
                cursor.execute("SELECT FilePath FROM UploadFiles WHERE FileID=%s LIMIT 1", (file_id,))
                row = cursor.fetchone()
                if row:
                    file_path = row.get('FilePath')

                # Remove staged and approved data, then the upload record
                cursor.execute("DELETE FROM StagedExtractedData WHERE FileID=%s", (file_id,))
                cursor.execute("DELETE FROM ExtractedData WHERE FileID=%s", (file_id,))
                cursor.execute("DELETE FROM UploadFiles WHERE FileID=%s", (file_id,))
            conn.commit()
            conn.close()

            # Delete file on disk
            if file_path:
                try:
                    fname = os.path.basename(file_path)
                    abs_dir = os.path.join(os.getcwd(), 'uploads')
                    abs_path = os.path.join(abs_dir, fname)
                    if abs_path.startswith(abs_dir) and os.path.exists(abs_path):
                        os.remove(abs_path)
                except Exception:
                    pass

            # Remove from memory stores
            job_store.pop(task_id, None)
            completed_unconfirmed_tasks.pop(task_id, None)

            return jsonify({"status": "success"})
        except Exception as e:
            traceback.print_exc()
            try:
                conn.rollback()
                conn.close()
            except Exception:
                pass
            return jsonify({"status": "error", "message": str(e)}), 500

    @app.route("/confirm-task", methods=["POST"])
    def confirm_task_data():
        payload = request.get_json()
        task_id = payload.get("task_id")
        if not task_id:
            return jsonify({"status": "error", "message": "No task_id"}), 400

        task = completed_unconfirmed_tasks.get(task_id)
        if not task:
            return jsonify({"status": "error", "message": "Task not found"}), 404

        try:
            conn = get_mysql_connection()
            with conn.cursor() as cursor:
                sql = """
                    INSERT INTO ConfirmedTasks
                    (TaskID, FileName, ConfirmedAt)
                    VALUES (%s, %s, NOW())
                """
                cursor.execute(sql, (task_id, task["filename"]))

            conn.commit()
            conn.close()

            completed_unconfirmed_tasks.pop(task_id, None)
            job_store.pop(task_id, None)

            return jsonify({"status": "success", "message": "Success record"}), 200

        except Exception as e:
            print("❌ Confirm Task Error:", e)
            return jsonify({"status": "error", "message": "Failed to store data. Please try again later."}), 500

    @app.route("/update", methods=["POST"])
    def update_extracted_data():
        payload = request.get_json()
        if not isinstance(payload, dict):
            return jsonify({"status": "error", "message": "Invalid payload"}), 400

        task_id     = payload.get("task_id")
        file_id     = payload.get("file_id")
        edited_data = payload.get("edited_data")

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

    @app.route("/save", methods=["POST"])
    def save_extracted_data():
        payload = request.get_json()
        if not isinstance(payload, dict):
            return jsonify({"status": "error", "message": "Invalid payload"}), 400

        user_id         = payload.get("user_id")
        user_email      = payload.get("email")
        filename        = payload.get("filename")
        image_path      = payload.get("image_path")
        structured_data = payload.get("structured_data")
        task_id         = payload.get("task_id")

        if isinstance(structured_data, str):
            try:
                import json as _json
                structured_data = _json.loads(structured_data)
            except Exception:
                return jsonify({"status": "error", "message": "structured_data must be JSON array"}), 400

        if not (filename and image_path and isinstance(structured_data, list)):
            return jsonify({"status": "error", "message": "Missing or invalid keys"}), 400

        print(f"/save: payload -> user_id={user_id}, email={user_email}, filename={filename}")

        try:
            conn = get_mysql_connection()
            with conn.cursor() as cursor:
                resolved = None
                if user_email:
                    cursor.execute(
                        """
                        SELECT id, email, role, is_approved
                        FROM users
                        WHERE LOWER(email)=LOWER(%s)
                        LIMIT 1
                        """,
                        (user_email,)
                    )
                    resolved = cursor.fetchone()
                elif user_id:
                    cursor.execute(
                        "SELECT id, email, role, is_approved FROM users WHERE id=%s LIMIT 1",
                        (user_id,)
                    )
                    resolved = cursor.fetchone()

            conn.close()
            if not resolved:
                return jsonify({"status": "error", "message": "Uploader not found"}), 400
            if not bool(resolved.get("is_approved")):
                return jsonify({"status": "error", "message": "Uploader not approved"}), 403
            if (resolved.get("role") or "").lower() not in ("staff", "admin"):
                return jsonify({"status": "error", "message": "Uploader role not allowed"}), 403

            if user_id and user_email and int(user_id) != int(resolved.get("id")):
                return jsonify({"status": "error", "message": "Uploader mismatch"}), 400

            uploader_id = int(resolved.get("id"))
            print(f"/save: verified uploader -> id={uploader_id}, email={resolved.get('email')}, role={resolved.get('role')}")
        except Exception as e:
            return jsonify({"status": "error", "message": f"Uploader verification failed: {e}"}), 500

        try:
            saved_file_path = None
            file_size_bytes = None
            try:
                if isinstance(image_path, str) and image_path.startswith("data:") and "," in image_path:
                    header, b64data = image_path.split(",", 1)
                    decoded = base64.b64decode(b64data)
                    file_size_bytes = len(decoded)

                    uploads_dir = os.path.join(os.getcwd(), "uploads")
                    os.makedirs(uploads_dir, exist_ok=True)
                    safe_name = (filename or "file").replace("/", "_").replace("\\", "_")
                    import uuid as _uuid
                    unique_name = f"{_uuid.uuid4().hex}_{safe_name}"
                    abs_path = os.path.join(uploads_dir, unique_name)
                    with open(abs_path, "wb") as f:
                        f.write(decoded)
                    saved_file_path = f"/uploads/{unique_name}"
                else:
                    saved_file_path = image_path
            except Exception as e:
                print("❌ Failed to decode/save uploaded file:", e)
                return jsonify({"status": "error", "message": "Invalid image_path; expected data URI"}), 400

            file_ext = None
            if isinstance(filename, str) and "." in filename:
                file_ext = filename.rsplit(".", 1)[-1].lower()
            if not file_ext:
                file_ext = "bin"

            conn = get_mysql_connection()
            with conn.cursor() as cursor:
                file_id = None
                # If this save came from a background task, reuse the original UploadFiles row
                if task_id:
                    job = job_store.get(task_id) or completed_unconfirmed_tasks.get(task_id)
                    if job and job.get('file_id'):
                        file_id = int(job.get('file_id'))
                        # Optionally update stored file path on uploadfiles
                        try:
                            cursor.execute("UPDATE UploadFiles SET FilePath=%s WHERE FileID=%s", (saved_file_path or "", file_id))
                        except Exception:
                            pass

                # If no existing file_id, create a new UploadFiles row (manual save flow)
                if not file_id:
                    insert_upload_sql = """
                        INSERT INTO uploadfiles
                        (uploaded_by, reviewed_by, FileName, FileFormat, FileSize, UploadDatetime, FilePath, OCRText, UploadStatus)
                        VALUES (%s, %s, %s, %s, %s, NOW(), %s, %s, %s)
                    """
                    cursor.execute(
                        insert_upload_sql,
                        (
                            uploader_id,
                            None,
                            filename,
                            file_ext,
                            file_size_bytes or 0,
                            saved_file_path or "",
                            None,
                            "pending"
                        )
                    )
                    file_id = cursor.lastrowid
                    print(f"/save: created uploadfiles row -> FileID={file_id}, uploaded_by={uploader_id}")

                insert_data_sql = """
                    INSERT INTO ExtractedData
                    (PageNumber, BillNumber, DocTypeID, SupplierName, Amount, PaymentDate, Signature, FileID, FilePath)
                    VALUES (%s, %s, %s,        %s,           %s,     %s,          %s,        %s,       %s)
                """

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
                        saved_file_path
                    ))

                if values:
                    cursor.executemany(insert_data_sql, values)

                # If came from staged task, clear staged rows so it won't show again in staff waiting list
                if task_id and file_id:
                    try:
                        cursor.execute("DELETE FROM StagedExtractedData WHERE FileID=%s", (file_id,))
                    except Exception:
                        pass

            conn.commit()
            conn.close()

            if task_id:
                completed_unconfirmed_tasks.pop(task_id, None)
                job_store.pop(task_id, None)

            preview = {
                "file_id": file_id,
                "filename": filename,
                "page_count": len(structured_data),
                "preview_data": structured_data[:2],
                "uploader_id": uploader_id
            }

            return jsonify({"status": "success", "preview": preview}), 200

        except Exception as e:
            print("❌ Error saving to MySQL:", e)
            return jsonify({"status": "error", "message": "Failed to store data."}), 500

    @app.route('/admin/uploads/pending', methods=['GET'])
    def list_pending_uploads():
        try:
            conn = get_mysql_connection()
            with conn.cursor() as cursor:
                cursor.execute("""
                                SELECT uf.FileID           AS file_id,
                                       uf.FileName         AS file_name,
                                       DATE_FORMAT(CONVERT_TZ(uf.UploadDatetime, @@session.time_zone, '+00:00'), '%Y-%m-%dT%H:%i:%sZ') AS uploaded_at,
                                       uf.UploadStatus     AS status,
                                       uf.uploaded_by      AS uploaded_by,
                                       u.email             AS uploader_email,
                                       u.department        AS uploader_department
                                FROM UploadFiles uf
                                LEFT JOIN users u ON u.id = uf.uploaded_by
                                WHERE uf.UploadStatus = 'pending'
                                ORDER BY uf.UploadDatetime DESC
                            """)
                rows = cursor.fetchall()
            conn.close()
            return jsonify({"status": "success", "uploads": rows})
        except Exception as e:
            traceback.print_exc()
            return jsonify({"status": "error", "message": str(e)}), 500

    @app.route('/admin/uploads/<int:file_id>/approve', methods=['POST'])
    def approve_upload(file_id: int):
        try:
            payload = request.get_json(silent=True) or {}
            reviewer_email = payload.get('reviewer_email')
            reviewer_id = payload.get('reviewer_id')

            conn = get_mysql_connection()
            with conn.cursor() as cursor:
                # Check current status for idempotency
                cursor.execute("SELECT UploadStatus FROM UploadFiles WHERE FileID=%s LIMIT 1", (file_id,))
                cur = cursor.fetchone()
                if not cur:
                    conn.close()
                    return jsonify({"status": "error", "message": "File not found"}), 404
                current_status = (cur.get('UploadStatus') or '').lower()
                if current_status in ('approved', 'rejected'):
                    conn.close()
                    return jsonify({"status": "noop", "current_status": current_status}), 200
                if reviewer_id is None and reviewer_email:
                    cursor.execute("SELECT id FROM users WHERE LOWER(email)=LOWER(%s) LIMIT 1", (reviewer_email,))
                    row = cursor.fetchone()
                    reviewer_id = int(row['id']) if row else None

                cursor.execute(
                    """
                    UPDATE UploadFiles
                    SET UploadStatus = 'approved',
                        RejectReason = NULL,
                        reviewed_by = COALESCE(%s, reviewed_by)
                    WHERE FileID = %s
                    """,
                    (reviewer_id, file_id)
                )
                cursor.execute(
                    """
                    INSERT INTO upload_review_logs (file_id, action, reason, reviewer_id, created_at)
                    VALUES (%s, 'approved', NULL, %s, NOW())
                    """,
                    (file_id, reviewer_id)
                )
            conn.commit()
            conn.close()
            return jsonify({"status": "success"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    @app.route('/admin/uploads/<int:file_id>/reject', methods=['POST'])
    def reject_upload(file_id: int):
        try:
            payload = request.get_json(silent=True) or {}
            reason = payload.get('reason')
            reviewer_email = payload.get('reviewer_email')
            reviewer_id = payload.get('reviewer_id')

            conn = get_mysql_connection()
            with conn.cursor() as cursor:
                # Check current status for idempotency
                cursor.execute("SELECT UploadStatus FROM UploadFiles WHERE FileID=%s LIMIT 1", (file_id,))
                cur = cursor.fetchone()
                if not cur:
                    conn.close()
                    return jsonify({"status": "error", "message": "File not found"}), 404
                current_status = (cur.get('UploadStatus') or '').lower()
                if current_status in ('approved', 'rejected'):
                    conn.close()
                    return jsonify({"status": "noop", "current_status": current_status}), 200
                if reviewer_id is None and reviewer_email:
                    cursor.execute("SELECT id FROM users WHERE LOWER(email)=LOWER(%s) LIMIT 1", (reviewer_email,))
                    row = cursor.fetchone()
                    reviewer_id = int(row['id']) if row else None

                cursor.execute(
                    """
                    UPDATE UploadFiles
                    SET UploadStatus = 'rejected',
                        RejectReason = %s,
                        reviewed_by = COALESCE(%s, reviewed_by)
                    WHERE FileID = %s
                    """,
                    (reason, reviewer_id, file_id)
                )
                cursor.execute(
                    """
                    INSERT INTO upload_review_logs (file_id, action, reason, reviewer_id, created_at)
                    VALUES (%s, 'rejected', %s, %s, NOW())
                    """,
                    (file_id, reason, reviewer_id)
                )
            conn.commit()
            conn.close()
            return jsonify({"status": "success"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    @app.route('/admin/uploads/archive', methods=['GET'])
    def list_archived_uploads():
        try:
            conn = get_mysql_connection()
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT uf.FileID           AS file_id,
                           uf.FileName         AS file_name,
                           DATE_FORMAT(CONVERT_TZ(uf.UploadDatetime, @@session.time_zone, '+00:00'), '%Y-%m-%dT%H:%i:%sZ') AS uploaded_at,
                           uf.UploadStatus     AS status,
                           uf.reviewed_by      AS reviewed_by,
                           u_rev.email         AS reviewer_email,
                           u_rev.department    AS reviewer_department,
                           u_up.email          AS uploader_email,
                           u_up.department     AS uploader_department
                    FROM UploadFiles uf
                    LEFT JOIN users u_rev ON u_rev.id = uf.reviewed_by
                    LEFT JOIN users u_up  ON u_up.id  = uf.uploaded_by
                    WHERE uf.UploadStatus IN ('approved','rejected')
                    ORDER BY uf.UploadDatetime DESC
                    """
                )
                rows = cursor.fetchall()
            conn.close()
            return jsonify({"status": "success", "uploads": rows})
        except Exception as e:
            traceback.print_exc()
            return jsonify({"status": "error", "message": str(e)}), 500

    @app.route('/uploads/by-user', methods=['POST'])
    def list_uploads_by_user():
        try:
            payload = request.get_json(silent=True) or {}
            user_id = payload.get('user_id')
            user_email = payload.get('email')

            conn = get_mysql_connection()
            with conn.cursor() as cursor:
                resolved = None
                if user_email:
                    cursor.execute(
                        """
                        SELECT id, email, role, is_approved
                        FROM users
                        WHERE LOWER(email)=LOWER(%s)
                        LIMIT 1
                        """,
                        (user_email,)
                    )
                    resolved = cursor.fetchone()
                elif user_id:
                    cursor.execute(
                        "SELECT id, email, role, is_approved FROM users WHERE id=%s LIMIT 1",
                        (user_id,)
                    )
                    resolved = cursor.fetchone()
            if not resolved:
                conn.close()
                return jsonify({"status": "error", "message": "User not found"}), 404

            resolved_id = int(resolved.get('id'))

            with conn.cursor() as cursor:
                # Only show uploads that have been confirmed/saved by staff (exist in ExtractedData)
                cursor.execute(
                    """
                    SELECT uf.FileID         AS file_id,
                           uf.FileName       AS file_name,
                           uf.FileFormat     AS file_format,
                           uf.FileSize       AS file_size,
                           DATE_FORMAT(CONVERT_TZ(uf.UploadDatetime, @@session.time_zone, '+00:00'), '%%Y-%%m-%%dT%%H:%%i:%%sZ') AS uploaded_at,
                           uf.UploadStatus   AS status,
                           uf.FilePath       AS file_path,
                           uf.RejectReason   AS reject_reason
                    FROM uploadfiles uf
                    INNER JOIN ExtractedData ed ON ed.FileID = uf.FileID
                    WHERE uf.uploaded_by = %s
                    GROUP BY uf.FileID
                    ORDER BY uf.UploadDatetime DESC
                    """,
                    (resolved_id,)
                )
                rows = cursor.fetchall()
            conn.close()

            return jsonify({"status": "success", "uploads": rows})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    @app.route('/uploads/<int:file_id>/extracted', methods=['GET'])
    def get_extracted_by_file(file_id: int):
        try:
            conn = get_mysql_connection()
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT ed.DataID       AS data_id,
                           ed.PageNumber   AS page,
                           ed.BillNumber   AS bill_number,
                           ed.SupplierName AS supplier_name,
                           ed.Amount       AS amount,
                           ed.PaymentDate  AS payment_date,
                           ed.Signature    AS signature,
                           ed.DocTypeID    AS doc_type_id,
                           dt.DocTypeName  AS doc_type_name,
                           uf.FilePath     AS file_path,
                           uf.UploadStatus AS upload_status,
                           uf.RejectReason AS reject_reason
                    FROM ExtractedData ed
                    LEFT JOIN DocType dt     ON dt.DocTypeID = ed.DocTypeID
                    LEFT JOIN uploadfiles uf ON uf.FileID    = ed.FileID
                    WHERE ed.FileID = %s
                    ORDER BY ed.PageNumber ASC
                    """,
                    (file_id,)
                )
                rows = cursor.fetchall()

                if not rows:
                    cursor.execute(
                        """
                        SELECT NULL             AS data_id,
                               s.PageNumber     AS page,
                               s.BillNumber     AS bill_number,
                               s.SupplierName   AS supplier_name,
                               s.Amount         AS amount,
                               s.PaymentDate    AS payment_date,
                               s.Signature      AS signature,
                               s.DocTypeID      AS doc_type_id,
                               dt.DocTypeName   AS doc_type_name,
                               s.FilePath       AS file_path,
                               uf.UploadStatus  AS upload_status,
                               uf.RejectReason  AS reject_reason
                        FROM StagedExtractedData s
                        LEFT JOIN DocType dt ON dt.DocTypeID = s.DocTypeID
                        LEFT JOIN uploadfiles uf ON uf.FileID = s.FileID
                        WHERE s.FileID = %s
                        ORDER BY s.PageNumber ASC
                        """,
                        (file_id,)
                    )
                    rows = cursor.fetchall()
            conn.close()

            if rows and rows[0].get("file_path"):
                file_name = os.path.basename(rows[0]["file_path"])
                file_url = request.host_url.rstrip("/") + "/uploads/" + file_name
                for r in rows:
                    r["file_url"] = file_url

            return jsonify({"status": "success", "items": rows})

        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    @app.route('/uploads/<int:file_id>', methods=['DELETE'])
    def delete_upload(file_id: int):
        try:
            conn = get_mysql_connection()
            file_path = None
            file_name = None
            with conn.cursor() as cursor:
                cursor.execute("SELECT FilePath, FileName FROM UploadFiles WHERE FileID=%s LIMIT 1", (file_id,))
                row = cursor.fetchone()
                if not row:
                    conn.close()
                    return jsonify({"status": "error", "message": "File not found"}), 404
                file_path = row.get('FilePath')
                file_name = row.get('FileName')

                cursor.execute("DELETE FROM StagedExtractedData WHERE FileID=%s", (file_id,))
                cursor.execute("DELETE FROM ExtractedData WHERE FileID=%s", (file_id,))
                cursor.execute("DELETE FROM UploadFiles WHERE FileID=%s", (file_id,))
            conn.commit()
            conn.close()

            if file_path:
                try:
                    fname = os.path.basename(file_path)
                    abs_dir = os.path.join(os.getcwd(), 'uploads')
                    abs_path = os.path.join(abs_dir, fname)
                    if abs_path.startswith(abs_dir) and os.path.exists(abs_path):
                        os.remove(abs_path)
                except Exception:
                    pass

            # Cleanup any in-memory tasks referencing this file_id (safety)
            try:
                stale_task_ids = [tid for tid, payload in completed_unconfirmed_tasks.items() if int(payload.get('file_id') or -1) == int(file_id)]
                for tid in stale_task_ids:
                    completed_unconfirmed_tasks.pop(tid, None)
                    job_store.pop(tid, None)
                # Fallback: also clear tasks by filename match if file_id is absent in old payloads
                if file_name:
                    stale_by_name = [tid for tid, payload in completed_unconfirmed_tasks.items() if (payload.get('filename') or '').strip() == (file_name or '').strip()]
                    for tid in stale_by_name:
                        completed_unconfirmed_tasks.pop(tid, None)
                        job_store.pop(tid, None)
            except Exception:
                pass

            return jsonify({"status": "success"})
        except Exception as e:
            traceback.print_exc()
            try:
                conn.rollback()
                conn.close()
            except Exception:
                pass
            return jsonify({"status": "error", "message": str(e)}), 500

    @app.route("/uploads/<path:filename>")
    def serve_uploaded_file(filename):
        uploads_dir = os.path.join(os.getcwd(), "uploads")
        safe_path = os.path.normpath(os.path.join(uploads_dir, filename))
        if not safe_path.startswith(uploads_dir):
            abort(403)
        if not os.path.exists(safe_path):
            abort(404)
        return send_from_directory(uploads_dir, filename)

    @app.route('/access/check', methods=['POST'])
    def check_access():
        try:
            payload = request.get_json(silent=True) or {}
            email = payload.get('email')
            if not email:
                return jsonify({"status": "error", "message": "Missing email"}), 400

            conn = get_mysql_connection()
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id        AS UserID,
                           email     AS Email,
                           role      AS Role,
                           is_approved AS IsApproved
                    FROM users
                    WHERE LOWER(email) = LOWER(%s)
                    LIMIT 1
                    """,
                    (email,)
                )
                row = cursor.fetchone()
            conn.close()

            if not row:
                return jsonify({"status": "denied", "message": "User not found"}), 403

            is_approved = bool(row.get("IsApproved"))
            role = (row.get("Role") or "").lower()

            if not is_approved or role == "pending":
                return jsonify({"status": "pending", "message": "User pending approval"}), 403

            return jsonify({
                "status": "success",
                "user": {
                    "user_id": row.get("UserID"),
                    "email": row.get("Email"),
                    "role": row.get("Role"),
                }
            })

        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    # ---------------- Notifications API ----------------
    @app.route('/notifications', methods=['GET'])
    def list_notifications():
        try:
            user_id = request.args.get('user_id')
            email = request.args.get('email')
            only_unread = (request.args.get('only_unread') or '0') in ('1','true','yes')

            if not user_id and not email:
                return jsonify({"status": "error", "message": "user_id or email required"}), 400

            conn = get_mysql_connection()
            with conn.cursor() as cursor:
                if not user_id and email:
                    cursor.execute("SELECT id FROM users WHERE LOWER(email)=LOWER(%s) LIMIT 1", (email,))
                    row = cursor.fetchone()
                    if not row:
                        conn.close()
                        return jsonify({"status": "success", "notifications": []})
                    user_id = int(row['id'])

                base_sql = (
                    """
                    SELECT n.id                 AS id,
                           n.event_type         AS event_type,
                           n.title              AS title,
                           n.body               AS body,
                           n.actor_id           AS actor_id,
                           u.email              AS actor_email,
                           n.created_at         AS created_at,
                           nr.is_read           AS is_read,
                           nr.read_at           AS read_at
                    FROM notification_recipients nr
                    JOIN notifications n ON n.id = nr.notification_id
                    LEFT JOIN users u ON u.id = n.actor_id
                    WHERE nr.user_id = %s
                    """
                )
                params = [int(user_id)]
                if only_unread:
                    base_sql += " AND nr.is_read = 0"
                base_sql += " ORDER BY n.created_at DESC LIMIT 100"
                cursor.execute(base_sql, params)
                rows = cursor.fetchall()
            conn.close()
            return jsonify({"status": "success", "notifications": rows})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    @app.route('/notifications/mark-read', methods=['POST'])
    def mark_notification_read():
        try:
            payload = request.get_json(silent=True) or {}
            notification_id = payload.get('notification_id')
            user_id = payload.get('user_id')
            email = payload.get('email')
            if not notification_id:
                return jsonify({"status": "error", "message": "notification_id required"}), 400

            conn = get_mysql_connection()
            with conn.cursor() as cursor:
                if not user_id and email:
                    cursor.execute("SELECT id FROM users WHERE LOWER(email)=LOWER(%s) LIMIT 1", (email,))
                    row = cursor.fetchone()
                    if not row:
                        conn.close()
                        return jsonify({"status": "error", "message": "user not found"}), 404
                    user_id = int(row['id'])

                cursor.execute(
                    """
                    UPDATE notification_recipients
                    SET is_read=1, read_at=NOW()
                    WHERE notification_id=%s AND user_id=%s
                    """,
                    (int(notification_id), int(user_id))
                )
            conn.commit()
            conn.close()
            return jsonify({"status": "success"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    return app


