from .database import get_mysql_connection


def resolve_document_type_id(document_type_name: str) -> int | None:
    conn = get_mysql_connection()
    with conn.cursor() as cursor:
        cursor.execute("SELECT DocTypeID FROM DocType WHERE DocTypeName = %s", (document_type_name,))
        result = cursor.fetchone()
    conn.close()
    return result["DocTypeID"] if result else None


