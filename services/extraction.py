import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from .ai_client import client


def extract_financial_data(ocr_text: str) -> list | dict:
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
            print(f"❌ AI service error: {e}")
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

    pages = re.split(r"\n📄 Page \d+\n", ocr_text)
    pages = [p.strip() for p in pages if p.strip()]
    if not pages:
        msg = {"status": "error", "message": "No pages detected in OCR text."}
        print(f"⚠️ {msg['message']}")
        return msg

    tasks = list(enumerate(pages, start=1))
    all_results: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(extract_fields_page, t): t for t in tasks}
        for fut in as_completed(futures):
            res = fut.result()
            if res:
                all_results.append(res)

    all_results.sort(key=lambda x: x["page"])
    if not all_results:
        msg = {"status": "error", "message": "No extractable financial information found in the document."}
        print(f"⚠️ {msg['message']}")
        return msg

    print("\n📑 Extracted All Financial Data:")
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


