import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from .ai_client import client


def normalize_thai_date(date_str: str | None) -> str | None:
    """
    Normalize Thai date to format: 'DD เดือน YYYY' (e.g., '02 พฤษภาคม 2568')
    Handles various input formats like: 01/10/2568, 01/ตุลาคม/2568, 1 ตุลาคม 2568
    """
    if not date_str or date_str == "null":
        return None
    
    # Thai month names
    thai_months = {
        'มกราคม': '01', 'มค': '01', 'ม.ค.': '01',
        'กุมภาพันธ์': '02', 'กพ': '02', 'ก.พ.': '02',
        'มีนาคม': '03', 'มีค': '03', 'มี.ค.': '03',
        'เมษายน': '04', 'เมย': '04', 'เม.ย.': '04',
        'พฤษภาคม': '05', 'พค': '05', 'พ.ค.': '05',
        'มิถุนายน': '06', 'มิย': '06', 'มิ.ย.': '06',
        'กรกฎาคม': '07', 'กค': '07', 'ก.ค.': '07',
        'สิงหาคม': '08', 'สค': '08', 'ส.ค.': '08',
        'กันยายน': '09', 'กย': '09', 'ก.ย.': '09',
        'ตุลาคม': '10', 'ตค': '10', 'ต.ค.': '10',
        'พฤศจิกายน': '11', 'พย': '11', 'พ.ย.': '11',
        'ธันวาคม': '12', 'ธค': '12', 'ธ.ค.': '12',
    }
    
    month_full_names = [
        'มกราคม', 'กุมภาพันธ์', 'มีนาคม', 'เมษายน', 'พฤษภาคม', 'มิถุนายน',
        'กรกฎาคม', 'สิงหาคม', 'กันยายน', 'ตุลาคม', 'พฤศจิกายน', 'ธันวาคม'
    ]
    
    date_str = str(date_str).strip()
    
    # Check if already in correct format: 'DD เดือน YYYY'
    pattern = r'^\d{1,2}\s+(' + '|'.join(month_full_names) + r')\s+\d{4}$'
    if re.match(pattern, date_str):
        # Ensure DD is zero-padded
        parts = date_str.split()
        if len(parts) == 3:
            day = parts[0].zfill(2)
            return f"{day} {parts[1]} {parts[2]}"
        return date_str
    
    # Try to parse date formats
    day, month_num, year = None, None, None
    
    # Format: DD/MM/YYYY or D/M/YYYY
    if '/' in date_str:
        parts = date_str.split('/')
        if len(parts) == 3:
            day = parts[0].strip()
            month_part = parts[1].strip()
            year = parts[2].strip()
            
            # Check if month is Thai name
            for thai_month, num in thai_months.items():
                if thai_month in month_part:
                    month_num = num
                    break
            
            # If not found, try as number
            if not month_num and month_part.isdigit():
                month_num = month_part.zfill(2)
    
    # Format: 'DD เดือนไทย YYYY' or 'D เดือนไทย YYYY'
    else:
        # Extract Thai month
        for thai_month, num in thai_months.items():
            if thai_month in date_str:
                month_num = num
                # Extract day and year
                parts = date_str.replace(thai_month, '|').split('|')
                if len(parts) == 2:
                    day = re.search(r'\d+', parts[0])
                    year = re.search(r'\d+', parts[1])
                    if day:
                        day = day.group()
                    if year:
                        year = year.group()
                break
    
    # If we got all parts, format them
    if day and month_num and year:
        try:
            day = str(int(day)).zfill(2)  # Ensure 01, 02, etc.
            month_idx = int(month_num) - 1
            if 0 <= month_idx < 12:
                month_name = month_full_names[month_idx]
                return f"{day} {month_name} {year}"
        except (ValueError, IndexError):
            pass
    
    # If normalization failed, return original
    return date_str


def normalize_amount(amount: str | float | int | None) -> str:
    """
    Normalize amount to always have 2 decimal places (e.g., '20000.00')
    """
    if amount is None or amount == "null":
        return "0.00"
    
    try:
        # Remove commas and convert to float
        if isinstance(amount, str):
            amount = amount.replace(',', '').strip()
        
        amount_float = float(amount)
        # Format with 2 decimal places
        return f"{amount_float:.2f}"
    except (ValueError, TypeError):
        return "0.00"


def extract_financial_data(ocr_text: str) -> list | dict:
    system_prompt = (
        "You are an AI assistant that extracts key information from Thai government "
        "financial documents using OCR text. Please extract the following fields and "
        "return only in JSON format:\n\n"
        "- bill_number: เลขที่ใบขอซื้อหรือเลขที่ มักจะอยู่มุมขวา เช่น 10778\n"
        "- supplier_name: หน่วยงานหรือชื่อผู้ขาย หรือ ชื่อซัพพลายเออร์\n"
        "- amount: ยอดรวมสุทธิที่อยู่ใกล้คำว่า 'รวมทั้งสิ้น', 'ยอดรวม', 'รวมจำนวนเงิน', 'รวม' "
        "เอาแต่ตัวเลขเท่านั้น ต้องมีทศนิยม 2 ตำแหน่งเสมอ เช่น 20000.00 ไม่ใช่ 20000\n"
        "- payment_date: วันที่ในเอกสาร ดึงออกมาเป็น format 'DD เดือน YYYY' เช่น '02 พฤษภาคม 2568' "
        "โดยใช้ชื่อเดือนภาษาไทยเต็ม (มกราคม, กุมภาพันธ์, มีนาคม, เมษายน, พฤษภาคม, มิถุนายน, "
        "กรกฎาคม, สิงหาคม, กันยายน, ตุลาคม, พฤศจิกายน, ธันวาคม) และปี พ.ศ.\n"
        "- signature: ชื่อจริงนามสกุลที่อยู่ใกล้คำว่า 'อนุมัติ' หรือ 'ผู้เบิก' ตัดคำนำหน้าชื่อเหลือแค่ชื่อและนามสกุล\n"
        "- description: ค่าที่ตามหลังคำว่า 'คำอธิบาย' หรือ 'รายละเอียด' ในเอกสาร ถ้าไม่มีให้ใช้ null (ห้ามเดา)\n\n"
        "If any field is not found, use null. Respond in JSON format only without any explanation."
    )

    def extract_fields(text: str) -> dict:
        user_prompt = f"OCR Text in Thai:\n{text}\n\nPlease return the result in JSON only."
        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_prompt}
                ],
                temperature=0
            )
        except Exception as e:
            print(f"[ERROR] AI service error: {e}")
            return {"status": "error", "message": "AI service unavailable."}

        content = response.choices[0].message.content.strip()
        if content.startswith("```json"):
            content = content.removeprefix("```json").removesuffix("```").strip()
        elif content.startswith("```"):
            content = content.removeprefix("```").removesuffix("```").strip()

        try:
            data = json.loads(content)
            if not any(data.values()):
                return {"status": "error", "message": "No extractable financial information found in the document."}
            return data
        except json.JSONDecodeError as e:
            print(f"[ERROR] JSON parsing failed: {e}")
            return {"status": "error", "message": "AI service unavailable."}

    def extract_fields_page(args: tuple[int, str]) -> dict | None:
        idx, page_text = args
        print(f"[EXTRACT] Extracting from Page {idx}...")
        result = extract_fields(page_text)
        if not isinstance(result, dict) or result.get("status") == "error":
            print(f"[WARN] Page {idx} extraction error: {result.get('message', 'unknown')}")
            return None
        
        # Normalize date and amount
        if "payment_date" in result:
            result["payment_date"] = normalize_thai_date(result["payment_date"])
        if "amount" in result:
            result["amount"] = normalize_amount(result["amount"])
        
        result["page"]     = idx
        result["raw_text"] = page_text
        return result

    pages = re.split(r"\n📄 Page \d+\n", ocr_text)
    pages = [p.strip() for p in pages if p.strip()]
    if not pages:
        msg = {"status": "error", "message": "No pages detected in OCR text."}
        print(f"[WARN] {msg['message']}")
        return msg

    tasks = list(enumerate(pages, start=1))
    all_results: list[dict] = []
    
    # Process pages sequentially to avoid overwhelming the system
    for task in tasks:
        res = extract_fields_page(task)
        if res:
            all_results.append(res)

    all_results.sort(key=lambda x: x["page"])
    if not all_results:
        msg = {"status": "error", "message": "No extractable financial information found in the document."}
        print(f"[WARN] {msg['message']}")
        return msg

    print("\n[DATA] Extracted All Financial Data:")
    for receipt in all_results:
        import json as _json
        print(_json.dumps(receipt, ensure_ascii=False, indent=2))

    return all_results


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


