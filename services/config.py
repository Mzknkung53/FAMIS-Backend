from dotenv import load_dotenv
import os

load_dotenv(override=True)

# OpenAI API Key
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
assert OPENAI_API_KEY, "Cannot find OPENAI_API_KEY in .env"
print("OpenAI Key Loaded")

# MySQL Settings
MYSQL_HOST     = os.getenv("MYSQL_HOST", "localhost")
MYSQL_PORT     = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER     = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
MYSQL_DB       = os.getenv("MYSQL_DB", "famis_db")

assert MYSQL_PASSWORD, "Cannot find MYSQL_PASSWORD in .env"
print("MySQL Settings Loaded")

# External tools
POPPLER_PATH = r"C:/Users/Ned/Desktop/Poppler/poppler-24.08.0/Library/bin"


