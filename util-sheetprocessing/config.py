"""
ETL + AI Pipeline Configuration
.env 파일에서 모든 설정을 로드한다.
"""
import os
from dotenv import load_dotenv

load_dotenv()


# --- Google Sheets ---
GCP_SERVICE_ACCOUNT_FILE = os.getenv("GCP_SERVICE_ACCOUNT_FILE", "./credentials/service_account.json")
SHEET_NAME = os.getenv("SHEET_NAME", "My_Database")
INPUT_WORKSHEET = os.getenv("INPUT_WORKSHEET", "Input_Data")
OUTPUT_WORKSHEET = os.getenv("OUTPUT_WORKSHEET", "Output_Data")

# --- LLM ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gemini-2.5-flash")

# --- Pipeline Parameters ---
GROUP_COLUMN = os.getenv("GROUP_COLUMN", "Group_ID")
TEXT_COLUMN = os.getenv("TEXT_COLUMN", "Raw_Text")
API_DELAY_SECONDS = float(os.getenv("API_DELAY_SECONDS", "1.0"))
