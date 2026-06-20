import base64
import binascii
import json
import os
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from flask import Flask, jsonify, render_template, request

BASE_DIR = Path(__file__).resolve().parent
for line in (BASE_DIR / ".env").read_text(encoding="utf-8").splitlines() if (BASE_DIR / ".env").exists() else []:
    if line.strip() and not line.lstrip().startswith("#") and "=" in line:
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 7_000_000

URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"']+", re.IGNORECASE)
PHONE_RE = re.compile(r"(?<!\d)(?:\+?84|0)(?:[\s.-]*\d){8,10}(?!\d)")
RISKS = {"safe", "suspicious", "danger"}
CHOICES = {"none", "clicked", "transferred", "otp"}
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_IMAGE_BYTES = 4 * 1024 * 1024


def load_json(name):
    return json.loads((BASE_DIR / "data" / name).read_text(encoding="utf-8"))


@app.get("/")
def index():
    return render_template("index.html", scam_types=load_json("scam_types.json"))


@app.post("/api/analyze")
def analyze():
    body = request.get_json(silent=True) or {}
    message = str(body.get("message", "")).strip()
    try:
        image = validate_image(body.get("image"))
    except ValueError as error:
        return jsonify(error=str(error)), 400
    if not message and not image:
        return jsonify(error="Bác hãy dán tin nhắn hoặc chọn ảnh chụp màn hình cần kiểm tra."), 400
    if len(message) > 5000:
        return jsonify(error="Tin nhắn dài quá 5.000 ký tự. Bác hãy rút gọn rồi thử lại."), 400

    try:
        detective = normalize_detective(call_gemini(detective_prompt(message, bool(image)), image))
    except ValueError as error:
        return jsonify(error=str(error)), 502

    psychology = None
    psychology_error = None
    if detective["risk"] in {"suspicious", "danger"}:
        try:
            psychology = normalize_psychology(
                call_gemini(psychology_prompt(message, detective))
            )
        except ValueError:
            psychology_error = "Cô tâm lý đang bận, vui lòng thử lại sau."

    return jsonify(
        detective=detective,
        psychology=psychology,
        psychologyError=psychology_error,
        links=inspect_links(message),
    )


@app.post("/api/rescue")
def rescue():
    body = request.get_json(silent=True) or {}
    choice = body.get("choice")
    if choice not in CHOICES:
        return jsonify(error="Bác hãy chọn đúng tình huống đã xảy ra."), 400
    if choice == "none":
        return jsonify(
            title="Bác đã dừng lại đúng lúc",
            steps=["Không bấm đường dẫn, không trả lời và xóa tin sau khi đã lưu bằng chứng."],
        )

    hotlines = [item for item in load_json("hotlines.json") if item.get("verified")]
    allowed_phones = {normalize_phone(item["phone"]) for item in hotlines}
    try:
        result = normalize_rescue(call_gemini(rescue_prompt(choice, hotlines)))
    except ValueError as error:
        return jsonify(error=str(error)), 502

    generated = {normalize_phone(value) for value in PHONE_RE.findall(json.dumps(result, ensure_ascii=False))}
    if generated - allowed_phones:
        return jsonify(error="AI đã trả về số chưa có trong bảng đã xác minh nên ScamCheck đã chặn kết quả."), 502
    return jsonify(result)


@app.get("/api/library")
def library():
    return jsonify(load_json("scam_types.json"))


def call_gemini(prompt, image=None):
    key = os.getenv("GEMINI_API_KEY", "").strip()
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
    if not key:
        raise ValueError("Máy chủ chưa được cấu hình GEMINI_API_KEY.")

    parts = [{"text": prompt}]
    if image:
        parts.append({"inline_data": {"mime_type": image["mimeType"], "data": image["data"]}})
    payload = json.dumps(
        {
            "contents": [{"parts": parts}],
            "generationConfig": {"responseMimeType": "application/json"},
        }
    ).encode()
    endpoint = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={key}"
    )
    try:
        response = urlopen(
            Request(endpoint, data=payload, headers={"Content-Type": "application/json"}),
            timeout=9,
        )
        data = json.loads(response.read().decode())
        raw = data["candidates"][0]["content"]["parts"][0]["text"]
        return parse_ai_json(raw)
    except HTTPError as error:
        try:
            detail = json.loads(error.read().decode()).get("error", {}).get("message", "")
        except (json.JSONDecodeError, UnicodeDecodeError):
            detail = ""
        if error.code == 429:
            raise ValueError("AI đang giới hạn lượt gọi. Bác vui lòng thử lại sau.") from error
        if error.code in {401, 403}:
            raise ValueError("Gemini API key không hợp lệ hoặc chưa được cấp quyền.") from error
        if error.code == 404:
            raise ValueError(f"Không tìm thấy model Gemini '{model}'.") from error
        if error.code == 400:
            raise ValueError(f"Gemini từ chối yêu cầu không hợp lệ: {detail[:240] or 'không có chi tiết' }.") from error
        raise ValueError(f"Gemini trả lỗi HTTP {error.code}. Bác vui lòng thử lại.") from error
    except (URLError, TimeoutError):
        raise ValueError("Không thể kết nối AI. Bác hãy kiểm tra mạng rồi thử lại.")
    except (KeyError, IndexError, json.JSONDecodeError) as error:
        raise ValueError("AI trả về dữ liệu không đúng định dạng. Bác vui lòng thử lại.") from error


def parse_ai_json(raw):
    if isinstance(raw, dict):
        return raw
    text = str(raw).strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("AI trả về dữ liệu không đúng định dạng. Bác vui lòng thử lại.")
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError as error:
        raise ValueError("AI trả về dữ liệu không đúng định dạng. Bác vui lòng thử lại.") from error
    if not isinstance(parsed, dict):
        raise ValueError("AI trả về dữ liệu không đúng định dạng. Bác vui lòng thử lại.")
    return parsed


def validate_image(value):
    if not value:
        return None
    if not isinstance(value, dict) or value.get("mimeType") not in IMAGE_TYPES or not isinstance(value.get("data"), str):
        raise ValueError("Ảnh phải là tệp PNG, JPG hoặc WebP.")
    try:
        decoded = base64.b64decode(value["data"], validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("Dữ liệu ảnh không hợp lệ.") from error
    if not decoded:
        raise ValueError("Ảnh chụp màn hình đang trống.")
    if len(decoded) > MAX_IMAGE_BYTES:
        raise ValueError("Ảnh lớn quá 4 MB. Bác hãy chọn ảnh nhỏ hơn.")
    return {"mimeType": value["mimeType"], "data": value["data"]}


def normalize_phone(value):
    raw = str(value).strip()
    digits = re.sub(r"\D", "", raw)
    return f"0{digits[2:]}" if raw.startswith("+84") else digits


def normalize_detective(data):
    risk = data.get("risk") if data.get("risk") in RISKS else "suspicious"
    signs = data.get("signs") if isinstance(data.get("signs"), list) else []
    signs = [
        {"reason": str(item.get("reason", "Dấu hiệu cần kiểm tra thêm.")), "quote": str(item.get("quote", ""))}
        for item in signs[:5]
        if isinstance(item, dict)
    ]
    actions = [str(item) for item in data.get("actions", [])[:3]] if isinstance(data.get("actions"), list) else []
    defaults = [
        "Không bấm đường dẫn và không cung cấp mã xác thực.",
        "Gọi tổng đài chính thức của ngân hàng được in trên thẻ.",
        "Lưu lại tin nhắn làm bằng chứng.",
    ]
    actions.extend(defaults[len(actions) :])
    return {
        "risk": risk,
        "summary": str(data.get("summary") or "Nội dung này cần được kiểm tra thêm."),
        "signs": signs,
        "actions": actions[:3],
    }


def normalize_psychology(data):
    message = str(data.get("message") or "").strip()
    if not message:
        raise ValueError("Thiếu phản hồi")
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", message) if part.strip()]
    return " ".join(sentences[:3])


def normalize_rescue(data):
    steps = data.get("steps") if isinstance(data.get("steps"), list) else []
    cleaned = []
    for item in steps[:6]:
        if not isinstance(item, dict):
            continue
        cleaned.append(
            {
                "action": str(item.get("action") or "Thực hiện bước này ngay."),
                "script": str(item.get("script") or "Tôi cần được hỗ trợ xử lý một vụ việc nghi lừa đảo."),
            }
        )
    if not cleaned:
        raise ValueError("Người ứng cứu trả về dữ liệu không đúng định dạng.")
    return {"title": str(data.get("title") or "Các bước cần làm ngay"), "steps": cleaned}


def inspect_links(message):
    results = []
    spoof_patterns = ("vietcornbank", "vietc0mbank", "vietcom-bank", "vietcombank-secure", "vietcombank-verify")
    for raw in URL_RE.findall(message):
        clean = raw.rstrip(".,;:!?)]}")
        parsed = urlparse(clean if clean.startswith("http") else f"https://{clean}")
        host = (parsed.hostname or "").lower()
        reasons = []
        if host.startswith("xn--") or ".xn--" in host:
            reasons.append("Tên miền dùng ký tự mã hóa dễ gây nhầm lẫn.")
        if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", host):
            reasons.append("Đường dẫn dùng địa chỉ số thay vì tên miền tổ chức.")
        if any(pattern in host for pattern in spoof_patterns):
            reasons.append("Tên miền có dấu hiệu thay ký tự hoặc thêm từ để giả mạo Vietcombank.")
        results.append({"url": clean, "domain": host, "warning": reasons or ["Chưa thấy mẫu giả mạo cục bộ; vẫn cần kiểm tra qua kênh chính thức."]})
    return results


def detective_prompt(message, has_image=False):
    return f'''Bạn là Thám tử ScamCheck. Giọng khô khan, lý tính. Phân tích kỹ thuật tin nhắn tiếng Việt.
Trả về duy nhất JSON: {{"risk":"safe|suspicious|danger","summary":"1-2 câu","signs":[{{"reason":"lý do","quote":"trích nguyên văn từ tin"}}],"actions":["đúng 3 hành động"]}}.
Không bịa chi tiết. Mỗi quote phải có nguyên văn trong tin hoặc đọc được rõ trong ảnh.
Ảnh chụp màn hình được gửi kèm: {"có" if has_image else "không"}.
Nội dung người dùng nhập: {message or "(không có; hãy đọc chữ trong ảnh chụp màn hình)"}'''


def psychology_prompt(message, detective):
    return f'''Bạn là Cô tâm lý. Xưng “cô”, gọi người dùng là “bác”. Viết 2-3 câu gần gũi, không hù dọa, không dạy dỗ; giải thích chiêu thức cảm xúc khiến người đọc suýt tin.
Trả về duy nhất JSON: {{"message":"2-3 câu"}}.
Tin: {message}
Kết luận Thám tử: {json.dumps(detective, ensure_ascii=False)}'''


def rescue_prompt(choice, hotlines):
    labels = {
        "clicked": "đã bấm vào đường dẫn",
        "transferred": "đã chuyển khoản",
        "otp": "đã cung cấp mã xác thực",
    }
    return f'''Bạn là Người ứng cứu. Người dùng {labels[choice]}. Giọng bình tĩnh, dứt khoát; không phân tích, không an ủi, chỉ nêu hành động.
Mỗi bước phải có câu nói mẫu để bác đọc khi gọi điện. Chỉ được dùng số trong bảng sau; nếu bảng trống, không viết bất kỳ số điện thoại nào và hướng dẫn gọi số chính thức in trên thẻ ngân hàng: {json.dumps(hotlines, ensure_ascii=False)}
Trả về duy nhất JSON: {{"title":"tiêu đề","steps":[{{"action":"việc làm","script":"câu nói mẫu"}}]}}.'''


if __name__ == "__main__":
    app.run(port=int(os.getenv("PORT", "5000")), debug=os.getenv("FLASK_DEBUG") == "1")
