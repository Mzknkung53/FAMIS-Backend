import pymysql
from pymysql.cursors import DictCursor
from .config import MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DB


def get_mysql_connection():
    conn = pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        db=MYSQL_DB,
        charset="utf8mb4",
        cursorclass=DictCursor
    )
    try:
        with conn.cursor() as cursor:
            cursor.execute("SET time_zone = '+00:00'")
    except Exception:
        pass
    return conn


