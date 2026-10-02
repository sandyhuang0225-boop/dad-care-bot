import os
import re
import json
import threading
from datetime import datetime
from flask import Flask, request, abort, render_template_string, jsonify
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, FlexSendMessage,
    BubbleContainer, BoxComponent, TextComponent, ButtonComponent,
    SeparatorComponent, URIAction, MessageAction, ImageMessage
)
import gspread

app = Flask(__name__)

# ==================== LINE 金鑰與常數 ====================
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "f55dd65b985ddbf08b49e186c852807c")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "mvPNDntah+ry3krkhP0XYN/Ex3952Fqo6H454nwYTwGPlyOPUcx8dhW/708V3nalXVLLWi8Sqt/zfiXZ/WkaBNftApzEbiN1n8XOTZoMvMKGTyYLtoG674w+dukYmjU52n/7Pe+FTmNc77Xymlx5FwdB04t89/1O/w1cDnyilFU=")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

SPREADSHEET_NAME = "爸爸照顧專戶收支與照護總表"
CREDENTIALS_FILE = os.path.join(os.path.dirname(__file__), "credentials.json")

# 確保對外網址一律為 https:// (LINE Flex 要求)
BASE_URL = os.getenv("RENDER_EXTERNAL_URL", "https://dad-care-bot.onrender.com").rstrip('/')
if not BASE_URL.startswith("https://"):
    BASE_URL = "https://" + BASE_URL.split("://")[-1]

import base64
import urllib.request

def analyze_receipt_with_gemini(image_bytes):
    """
    使用 Google Gemini Flash 視覺模型辨識發票或收據中的品項與總金額
    """
    if not GEMINI_API_KEY:
        return None
    try:
        b64_img = base64.b64encode(image_bytes).decode('utf-8')
        prompt = """
你是一個專精於台灣各類發票與收據的超高準確率視覺辨識 AI 專家。
面對可能有些微晃動、模糊、光影或折痕的照片，請遵循以下【台灣發票結構判讀法則】進行深度分析：

【重要：金額判讀法則】
1. 尋找「最終實付總金額」：
   - 台灣傳統或電子發票、全聯/家樂福/大樹藥局/超商明細單上，請優先鎖定標有【總計】、【合計】、【實收】、【實付】、【應付金額】、【TOTAL】旁邊的數字。
   - 通常位於發票中下段，字體最大、最粗，或帶有粗黑框線。
2. 絕對要排除的干擾數字（千萬不要誤判為總金額）：
   - 排除：課稅別（如 TX, 1, 0）、數量（如 1, 2）、單價、序號、折扣金額。
   - 排除：應稅銷售額、營業稅（如 5%）、零稅率、免稅。
   - 排除：發票號碼（如 AB-12345678）、日期時間、隨機碼、找零金額。
3. 交叉比對驗證：
   - 如果照片模糊，請觀察發票上的購買明細清單，將購買項目的金額進行心算相加，對照是否與辨識出的「總計」相符，以確保金額百分之百精確！

【品項與分類判讀】
1. 品項名稱：請挑選 1~2 個主要購買的代表性商品（如：成人尿布、水果、衛生紙、血壓藥、午餐等，10字以內），不要把商品編號放進去。
2. 分類嚴格 5 選 1：
   - 伙食餐飲 (蔬菜水果、便當餐點、飲料食物)
   - 生活耗品 (尿布、濕紙巾、衛生紙、沐浴乳、清潔用品、看護耗品)
   - 醫療藥費 (藥局買藥、高醫門診掛號費、醫療器材)
   - 交通接送 (計程車、加油、高鐵捷運)
   - 日常雜支 (其他雜支)

請嚴格只輸出 JSON 格式，絕對不要包含任何 markdown 標籤或額外文字：
{"item": "品項名稱", "amount": 850, "category": "生活耗品"}
"""
        req_body = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                        {
                            "inline_data": {
                                "mime_type": "image/jpeg",
                                "data": b64_img
                            }
                        }
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.1,
                "response_mime_type": "application/json"
            }
        }
        models_to_try = ["gemini-3.1-flash-lite", "gemini-3.1-flash-lite-preview", "gemini-flash-latest"]
        for m in models_to_try:
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={GEMINI_API_KEY}"
                req = urllib.request.Request(
                    url,
                    data=json.dumps(req_body).encode('utf-8'),
                    headers={'Content-Type': 'application/json'}
                )
                with urllib.request.urlopen(req, timeout=12) as resp:
                    res_data = json.loads(resp.read().decode('utf-8'))
                    candidates = res_data.get("candidates", [])
                    if candidates:
                        text_out = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                        text_out = re.sub(r'```json\s*|\s*```', '', text_out).strip()
                        data = json.loads(text_out)
                        amt = int(data.get("amount", 0))
                        item = str(data.get("item", "日常採買")).strip()
                        cat = str(data.get("category", "日常雜支")).strip()
                        if amt > 0:
                            return {"amount": amt, "item": item, "category": cat}
            except Exception as ex_m:
                print(f"嘗試模型 {m} 失敗: {ex_m}")
                continue
    except Exception as e:
        print(f"Gemini 圖片辨識失敗: {e}")

def get_gc():
    try:
        # 1. 優先從環境變數讀取 (避免 GitHub 攔截撤銷)
        creds_json = os.getenv("GOOGLE_CREDENTIALS")
        if creds_json:
            creds_dict = json.loads(creds_json)
            return gspread.service_account_from_dict(creds_dict)
        # 2. Render Secret File 預設路徑
        secret_file = "/etc/secrets/credentials.json"
        if os.path.exists(secret_file):
            return gspread.service_account(secret_file)
        # 3. 本地開發讀取 credentials.json
        if os.path.exists(CREDENTIALS_FILE):
            return gspread.service_account(CREDENTIALS_FILE)
    except Exception as e:
        print(f"無法載入 Google 憑證: {e}")
    return None

def get_sheet():
    gc = get_gc()
    if not gc:
        return None
    try:
        return gc.open(SPREADSHEET_NAME)
    except Exception as e:
        print(f"無法開啟試算表 {SPREADSHEET_NAME}: {e}")
        return None

def get_worksheet(title):
    sh = get_sheet()
    if not sh:
        return None
    try:
        return sh.worksheet(title)
    except Exception as e:
        print(f"無法獲取工作表 {title}: {e}")
        return None

def clean_num(val):
    if not val:
        return 0
    s = re.sub(r'[^\d]', '', str(val).strip())
    return int(s) if s else 0

# ==================== 家人 LINE ID 綁定與名稱標準化 ====================
USER_BINDINGS_CACHE = {
    "Uc63f60ac0469e5f5a684081e20814dff": {"name": "黃蕙芬", "role": "妹妹"}
}

def load_user_bindings():
    try:
        ws = get_worksheet("成員綁定")
        if ws:
            rows = ws.get_all_values()
            if len(rows) > 1:
                for r in rows[1:]:
                    uid = r[0].strip() if len(r) > 0 else ""
                    name = r[1].strip() if len(r) > 1 else ""
                    role = r[2].strip() if len(r) > 2 else ""
                    if uid and name:
                        USER_BINDINGS_CACHE[uid] = {"name": name, "role": role}
    except Exception as e:
        print(f"載入成員綁定失敗: {e}")

def get_user_name_by_id(user_id, display_name=""):
    if user_id in USER_BINDINGS_CACHE:
        return USER_BINDINGS_CACHE[user_id]["name"]
    # 嘗試從快取讀取不到時更新一次
    load_user_bindings()
    if user_id in USER_BINDINGS_CACHE:
        return USER_BINDINGS_CACHE[user_id]["name"]
    return clean_user_name(display_name)

def bind_user(user_id, target_name):
    roles = {
        "黃志嘉": "哥哥",
        "黃志龍": "弟弟",
        "黃芊甄": "姊姊",
        "黃蕙芬": "妹妹",
        "塗雅芳": "弟妹"
    }
    role = roles.get(target_name, "家人")
    USER_BINDINGS_CACHE[user_id] = {"name": target_name, "role": role}
    try:
        ws = get_worksheet("成員綁定")
        if ws:
            now_time = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
            # 檢查是否已存在
            rows = ws.get_all_values()
            found_idx = -1
            if len(rows) > 1:
                for idx, r in enumerate(rows[1:], start=2):
                    if len(r) > 0 and r[0].strip() == user_id:
                        found_idx = idx
                        break
            if found_idx > 0:
                ws.update(f"A{found_idx}:E{found_idx}", [[user_id, target_name, role, now_time, "手動重新綁定"]])
            else:
                ws.append_row([user_id, target_name, role, now_time, "LINE點選綁定"])
            return True
    except Exception as e:
        print(f"綁定失敗: {e}")
    return False

def clean_user_name(name):
    if not name:
        return "家人"
    if any(k in name for k in ["黃志嘉", "志嘉"]):
        return "黃志嘉"
    if any(k in name for k in ["黃志龍", "志龍"]):
        return "黃志龍"
    if any(k in name for k in ["黃芊甄", "芊甄"]):
        return "黃芊甄"
    if any(k in name for k in ["黃蕙芬", "蕙芬", "Sandy", "sandy", "先迪", "諾華"]):
        return "黃蕙芬"
    if any(k in name for k in ["塗雅芳", "雅芳"]):
        return "塗雅芳"
    return name

def create_binding_flex():
    members = [
        ("黃志龍", "弟弟", "#1d3557"),
        ("塗雅芳", "弟妹", "#e63946"),
        ("黃志嘉", "哥哥", "#457b9d"),
        ("黃芊甄", "姊姊", "#2a9d8f"),
        ("黃蕙芬", "妹妹", "#6a4c93")
    ]
    buttons = []
    for name, role, color in members:
        buttons.append(
            ButtonComponent(
                action=MessageAction(label=f"我是 {role}【{name}】", text=f"綁定身分 {name}"),
                style='secondary',
                height='sm',
                margin='xs'
            )
        )

    return FlexSendMessage(
        alt_text="【爸爸照顧小秘書】請綁定您的家人身分",
        contents=BubbleContainer(
            body=BoxComponent(layout='vertical', padding_all='none', contents=[
                BoxComponent(layout='vertical', padding_all='lg', background_color='#1d3557', contents=[
                    TextComponent(text="👤 歡迎！請綁定您的家人身分", weight='bold', size='md', color='#ffffff'),
                    TextComponent(text="僅需點選一次，永久綁定您的 LINE 帳號！", size='xs', color='#a8dadc', margin='xs'),
                ]),
                BoxComponent(layout='vertical', padding_all='lg', spacing='sm', contents=[
                    TextComponent(text="綁定後，您傳送語音或發票照片，系統將自動以您的名義精準入帳，姐姐月底結算零失誤：", size='xs', color='#555555', wrap=True),
                    SeparatorComponent(margin='xs'),
                    *buttons,
                    SeparatorComponent(margin='sm'),
                    TextComponent(text="💡 若點選錯誤，日後隨時輸入「綁定身分」即可更換！", size='xxs', color='#888888', wrap=True)
                ])
            ])
        )
    )

# ==================== 建立列排版小工具 ====================
def create_row(label, value, color='#333333', weight='regular'):
    return BoxComponent(
        layout='horizontal',
        margin='sm',
        contents=[
            TextComponent(text=label, size='sm', color='#888888', flex=3),
            TextComponent(text=str(value), size='sm', color=color, weight=weight, flex=5, align='end')
        ]
    )

# ==================== 1. 記一筆支出卡片 ====================
def create_expense_menu_flex():
    return FlexSendMessage(
        alt_text="【爸爸照顧】記一筆支出",
        contents=BubbleContainer(
            body=BoxComponent(layout='vertical', padding_all='none', contents=[
                BoxComponent(layout='vertical', padding_all='lg', background_color='#e63946', contents=[
                    TextComponent(text="💰 爸爸照顧專戶 ｜ 記一筆支出", weight='bold', size='md', color='#ffffff'),
                    TextComponent(text="實報實銷、公開透明，大家辛苦了！", size='xs', color='#ffe3e3', margin='xs'),
                ]),
                BoxComponent(layout='vertical', padding_all='lg', spacing='sm', contents=[
                    TextComponent(text="請選擇記帳方式或確認身分：", weight='bold', size='xs', color='#555555'),
                    SeparatorComponent(margin='xs'),
                    # 按鍵 1：拍照上傳發票 (使用 line://nv/camera 快速調起手機相機拍照)
                    ButtonComponent(
                        action=URIAction(label="📸 拍照上傳發票", uri="line://nv/camera"),
                        style='primary',
                        color='#e63946',
                        height='sm',
                        margin='sm'
                    ),
                    # 按鍵 2：語音輸入花費金額
                    ButtonComponent(
                        action=MessageAction(label="🎙️ 語音輸入花費金額", text="語音記帳說明"),
                        style='primary',
                        color='#1d3557',
                        height='sm',
                        margin='sm'
                    ),
                    # 按鍵 3：綁定身分
                    ButtonComponent(
                        action=MessageAction(label="👤 綁定身分", text="綁定身分"),
                        style='secondary',
                        height='sm',
                        margin='sm'
                    ),
                ])
            ])
        )
    )

# ==================== 2. 專戶進項管理卡片 ====================
def create_income_menu_flex():
    return FlexSendMessage(
        alt_text="【爸爸照顧】專戶進項管理",
        contents=BubbleContainer(
            body=BoxComponent(layout='vertical', padding_all='none', contents=[
                BoxComponent(layout='vertical', padding_all='lg', background_color='#2a9d8f', contents=[
                    TextComponent(text="🏠 爸爸照顧專戶 ｜ 進項管理", weight='bold', size='md', color='#ffffff'),
                    TextComponent(text="房租實收匯入與被動收益入帳核算", size='xs', color='#d8f3dc', margin='xs'),
                ]),
                BoxComponent(layout='vertical', padding_all='lg', spacing='sm', contents=[
                    TextComponent(text="💼 點擊按鈕或直接輸入金額：", weight='bold', size='sm', color='#333333'),
                    SeparatorComponent(margin='xs'),
                    ButtonComponent(action=MessageAction(label="🏠 蕙芬 登記房租實收入帳", text="登記房租入帳"), style='primary', color='#2a9d8f', height='sm'),
                    ButtonComponent(action=MessageAction(label="🏛️ 芊甄 登記老人年金", text="登記老人年金"), style='secondary', height='sm', margin='xs'),
                    ButtonComponent(action=MessageAction(label="📈 芊甄 登記債券收益", text="登記債券收益"), style='secondary', height='sm', margin='xs'),
                    ButtonComponent(action=MessageAction(label="💰 芊甄 登記利息收入", text="登記利息收入"), style='secondary', height='sm', margin='xs'),
                    SeparatorComponent(margin='md'),
                    TextComponent(text="💡 小提示：直接輸入「房租 18500」或「老人年金 4164」即可直接入帳！", size='xxs', color='#666666', wrap=True),
                ])
            ])
        )
    )

# ==================== 3. 本月收支總表卡片 ====================
def create_summary_flex(month_str, total_income, total_expense, net_balance, category_breakdown, base_balance=635008, current_balance=635008):
    net_color = '#2a9d8f' if net_balance >= 0 else '#e63946'
    net_sign = "+" if net_balance > 0 else ""
    cat_boxes = []
    for cat, amt in category_breakdown.items():
        if amt > 0:
            cat_boxes.append(create_row(f"• {cat}", f"{amt:,} 元"))

    return FlexSendMessage(
        alt_text=f"【收支總表】爸爸照顧專戶 {month_str} 財務月報",
        contents=BubbleContainer(
            body=BoxComponent(layout='vertical', padding_all='none', contents=[
                BoxComponent(layout='vertical', padding_all='lg', background_color='#1d3557', contents=[
                    TextComponent(text=f"📊 照顧專戶 ｜ {month_str} 財務總表", weight='bold', size='md', color='#ffffff'),
                    TextComponent(text="自動扣抵月收支，解放雙手、公開透明", size='xs', color='#a8dadc', margin='xs'),
                ]),
                BoxComponent(layout='vertical', padding_all='lg', spacing='sm', contents=[
                    BoxComponent(
                        layout='vertical',
                        background_color='#f1faee',
                        padding_all='md',
                        corner_radius='md',
                        contents=[
                            TextComponent(text="💰 爸爸照顧專戶目前總餘額", size='xs', color='#457b9d', weight='bold'),
                            TextComponent(text=f"${current_balance:,} 元", size='xl', color='#1d3557', weight='bold', margin='xs'),
                            TextComponent(text=f"基準結餘：${base_balance:,} 元 (姐姐10/1結算)", size='xxs', color='#888888', margin='xs')
                        ]
                    ),
                    SeparatorComponent(margin='md'),
                    create_row("🟢 本月進項總額", f"+{total_income:,} 元", color='#2a9d8f', weight='bold'),
                    create_row("🔴 本月支出總額", f"-{total_expense:,} 元", color='#e63946', weight='bold'),
                    create_row("💵 本月收支淨差額", f"{net_sign}{net_balance:,} 元", color=net_color, weight='bold'),
                    SeparatorComponent(margin='md'),
                    TextComponent(text="📑 本月主要支出項目：", weight='bold', size='xs', color='#555555', margin='sm'),
                    *(cat_boxes if cat_boxes else [TextComponent(text="目前尚無支出記錄", size='xs', color='#999999')]),
                    SeparatorComponent(margin='md'),
                    ButtonComponent(action=MessageAction(label="👥 姐姐結帳專用：本月各人代墊彙整", text="各人代墊結算"), style='primary', color='#e63946', height='sm'),
                    ButtonComponent(action=MessageAction(label="📋 查看本月支出流水帳", text="查詢最近支出"), style='secondary', height='sm', margin='xs'),
                    ButtonComponent(action=MessageAction(label="✏️ 月底對帳：修正專戶總結餘", text="修正總金額"), style='secondary', height='sm', margin='xs'),
                ])
            ])
        )
    )

# ==================== 3b. 月底姐姐結帳專用卡片 (各人代墊彙整) ====================
def create_settlement_flex(month_str, member_expenses, total_expense):
    member_boxes = []
    # 5 位家人名單
    family_members = ["黃志嘉", "黃志龍", "黃芊甄", "黃蕙芬", "塗雅芳"]
    roles = {
        "黃志嘉": "哥哥",
        "黃志龍": "弟弟",
        "黃芊甄": "姊姊",
        "黃蕙芬": "妹妹",
        "塗雅芳": "弟妹"
    }

    for name in family_members:
        amt = member_expenses.get(name, 0)
        role = roles.get(name, "")
        color = '#e63946' if amt > 0 else '#888888'
        weight = 'bold' if amt > 0 else 'regular'
        member_boxes.append(create_row(f"👤 {role} {name}", f"{amt:,} 元", color=color, weight=weight))

    # 檢查是否有其他代墊人
    for name, amt in member_expenses.items():
        if name not in family_members and amt > 0:
            member_boxes.append(create_row(f"👤 {name}", f"{amt:,} 元", color='#e63946', weight='bold'))

    return FlexSendMessage(
        alt_text=f"【月底結算】爸爸照顧專戶 {month_str} 各人代墊款總表",
        contents=BubbleContainer(
            body=BoxComponent(layout='vertical', padding_all='none', contents=[
                BoxComponent(layout='vertical', padding_all='lg', background_color='#e63946', contents=[
                    TextComponent(text=f"🧾 月底結帳 ｜ {month_str} 各人代墊彙整", weight='bold', size='md', color='#ffffff'),
                    TextComponent(text="方便黃芊甄(姊姊)月底統一撥款核銷", size='xs', color='#ffe3e3', margin='xs'),
                ]),
                BoxComponent(layout='vertical', padding_all='lg', spacing='sm', contents=[
                    TextComponent(text="💡 本月全家人代墊金額結算：", weight='bold', size='sm', color='#333333'),
                    SeparatorComponent(margin='xs'),
                    *member_boxes,
                    SeparatorComponent(margin='md'),
                    create_row("💵 本月應撥款總額", f"{total_expense:,} 元", color='#e63946', weight='bold'),
                    SeparatorComponent(margin='md'),
                    TextComponent(text="📌 說明：姊姊匯款後，該月代墊款即已由照顧專戶支應平帳。", size='xxs', color='#888888', wrap=True),
                    ButtonComponent(action=MessageAction(label="📊 返回本月收支總表", text="本月收支總表"), style='secondary', height='sm', margin='sm'),
                    ButtonComponent(action=MessageAction(label="✏️ 校正專戶總結餘", text="修正總金額"), style='secondary', height='sm', margin='xs'),
                ])
            ])
        )
    )

# ==================== 3c. 專戶總結餘修正引導卡片 ====================
def create_adjust_balance_guide_flex(current_balance):
    return FlexSendMessage(
        alt_text="【月底校正】爸爸照顧專戶總結餘修正",
        contents=BubbleContainer(
            body=BoxComponent(layout='vertical', padding_all='none', contents=[
                BoxComponent(layout='vertical', padding_all='lg', background_color='#457b9d', contents=[
                    TextComponent(text="✏️ 照顧專戶 ｜ 月底總結餘修正", weight='bold', size='md', color='#ffffff'),
                    TextComponent(text="核對網銀/存摺真實餘額，即時校正結算", size='xs', color='#e8f4f8', margin='xs'),
                ]),
                BoxComponent(layout='vertical', padding_all='lg', spacing='sm', contents=[
                    BoxComponent(
                        layout='vertical',
                        background_color='#f1faee',
                        padding_all='md',
                        corner_radius='md',
                        contents=[
                            TextComponent(text="💰 系統目前計算總結餘", size='xs', color='#457b9d', weight='bold'),
                            TextComponent(text=f"${current_balance:,} 元", size='xl', color='#1d3557', weight='bold', margin='xs'),
                        ]
                    ),
                    SeparatorComponent(margin='md'),
                    TextComponent(text="💡 使用情境說明：", weight='bold', size='xs', color='#333333'),
                    TextComponent(text="若平時有零星進項或支出忘記登記，月底負責人核對存摺真實金額後，可直接修正專戶總額平帳！", size='xs', color='#666666', wrap=True),
                    SeparatorComponent(margin='md'),
                    TextComponent(text="📋 兩種便利修正方式：", weight='bold', size='xs', color='#333333'),
                    TextComponent(text="👉 方式一：直接在對話框輸入\n「修正總額 635008」\n（可加備註，如：修正總額 635008 10月底對帳）\n\n👉 方式二：點擊下方按鈕開啟線上表單直接填寫！", size='xs', color='#555555', wrap=True),
                    SeparatorComponent(margin='md'),
                    ButtonComponent(action=URIAction(label="🌐 開啟線上快速校正表單", uri=f"{BASE_URL}/adjust_balance"), style='primary', color='#457b9d', height='sm'),
                    ButtonComponent(action=MessageAction(label="📊 返回本月收支總表", text="本月收支總表"), style='secondary', height='sm', margin='xs'),
                ])
            ])
        )
    )

# ==================== 4. 醫院回診與領藥卡片 (高醫專屬) ====================
def create_clinic_flex(clinic_rows):
    items = []
    if clinic_rows:
        for r in clinic_rows[:3]:
            # r: [看診日期, 時段, 醫院, 科別, 主治醫師, 掛號號碼, 備註, 狀態]
            date_str = r[0] if len(r) > 0 else ""
            dept     = r[3] if len(r) > 3 else "神經內科"
            doc      = r[4] if len(r) > 4 else ""
            no       = r[5] if len(r) > 5 else ""
            items.append(create_row(f"📅 {date_str}", f"高醫 {dept} {doc} ({no}號)"))
    else:
        items.append(TextComponent(text="📍 醫院固定為【高醫】\n目前暫無近期預約，點擊下方即可預約！", size='xs', color='#555555', wrap=True))

    return FlexSendMessage(
        alt_text="【爸爸照顧】高醫回診與慢箋提醒",
        contents=BubbleContainer(
            body=BoxComponent(layout='vertical', padding_all='none', contents=[
                BoxComponent(layout='vertical', padding_all='lg', background_color='#457b9d', contents=[
                    TextComponent(text="🏥 爸爸健康 ｜ 高醫回診與慢箋", weight='bold', size='md', color='#ffffff'),
                    TextComponent(text="神經內科(失智) ＆ 新陳代謝科(血糖/血壓)", size='xs', color='#f1faee', margin='xs'),
                ]),
                BoxComponent(layout='vertical', padding_all='lg', spacing='sm', contents=[
                    TextComponent(text="🗓️ 近期回診排程：", weight='bold', size='sm', color='#333333'),
                    SeparatorComponent(margin='xs'),
                    *items,
                    SeparatorComponent(margin='md'),
                    ButtonComponent(action=URIAction(label="➕ 預約登記高醫下次回診", uri=f"{BASE_URL}/clinic_form"), style='primary', color='#457b9d', height='sm'),
                    ButtonComponent(action=MessageAction(label="💊 查看慢性處方箋領藥", text="查看慢箋領藥"), style='secondary', height='sm', margin='xs'),
                ])
            ])
        )
    )

# ==================== 5. 目前用藥手冊卡片 (Baseline) ====================
def create_medication_flex(med_rows):
    med_boxes = []
    if med_rows:
        for r in med_rows[:6]:
            # r: [藥品名稱, 作用/用途, 用法用量, 服用時段, 飯前/飯後, 注意事項, 照片]
            name   = r[0] if len(r) > 0 else ""
            timing = r[3] if len(r) > 3 else ""
            meal   = r[4] if len(r) > 4 else ""
            med_boxes.append(create_row(f"💊 {name}", f"{timing} ({meal})"))
    else:
        med_boxes.append(create_row("💊 憶思能 (Exelon)", "早上隨餐 (失智認知)"))
        med_boxes.append(create_row("💊 庫魯化錠 (Glucophage)", "早晚飯後 (血糖控制)"))
        med_boxes.append(create_row("💊 脈優錠 (Norvasc)", "早上飯後 (血壓控制)"))
        med_boxes.append(create_row("💊 悠樂丁 (Eurodin)", "睡前半顆 (夜間舒眠)"))

    return FlexSendMessage(
        alt_text="【爸爸照顧】目前用藥清單手冊",
        contents=BubbleContainer(
            body=BoxComponent(layout='vertical', padding_all='none', contents=[
                BoxComponent(layout='vertical', padding_all='lg', background_color='#6a4c93', contents=[
                    TextComponent(text="💊 爸爸用藥 ｜ 現行用藥手冊", weight='bold', size='md', color='#ffffff'),
                    TextComponent(text="高醫神經內科與新陳代謝科現行用藥", size='xs', color='#f3e8ee', margin='xs'),
                ]),
                BoxComponent(layout='vertical', padding_all='lg', spacing='sm', contents=[
                    TextComponent(text="📋 每日固定服用藥品清單：", weight='bold', size='sm', color='#333333'),
                    SeparatorComponent(margin='xs'),
                    *med_boxes,
                    SeparatorComponent(margin='md'),
                    TextComponent(text="💡 新增方式：\n1. 拍新藥袋直接傳送至此對話框！\n2. 點擊下方線上表單填寫登記！", size='xs', color='#666666', wrap=True),
                    ButtonComponent(action=URIAction(label="✏️ 線上表單新增/維護用藥", uri=f"{BASE_URL}/med_form"), style='secondary', height='sm', margin='xs'),
                ])
            ])
        )
    )

# ==================== 6. 生活照護日誌卡片 ====================
def create_care_log_flex(log_rows):
    recent_logs = []
    if log_rows:
        for r in log_rows[:3]:
            # r: [時間, 記錄人, 精神/心情, 飲食/食慾, 量測數據, 今日活動, 照片]
            date_str = r[0][:10] if len(r) > 0 else ""
            author   = r[1] if len(r) > 1 else ""
            mood     = r[2] if len(r) > 2 else ""
            vitals   = r[4] if len(r) > 4 else ""
            recent_logs.append(create_row(f"{date_str} ({author})", f"{mood} {vitals}"))
    else:
        recent_logs.append(TextComponent(text="目前尚無日誌，歡迎隨手記錄爸爸日常！", size='xs', color='#888888'))

    return FlexSendMessage(
        alt_text="【爸爸照顧】生活照護日誌",
        contents=BubbleContainer(
            body=BoxComponent(layout='vertical', padding_all='none', contents=[
                BoxComponent(layout='vertical', padding_all='lg', background_color='#f4a261', contents=[
                    TextComponent(text="📋 爸爸照顧 ｜ 生活日誌", weight='bold', size='md', color='#ffffff'),
                    TextComponent(text="紀錄生活點滴，全家人隨時掌握狀況", size='xs', color='#fff1e6', margin='xs'),
                ]),
                BoxComponent(layout='vertical', padding_all='lg', spacing='sm', contents=[
                    TextComponent(text="📝 近期生活狀況紀錄：", weight='bold', size='sm', color='#333333'),
                    SeparatorComponent(margin='xs'),
                    *recent_logs,
                    SeparatorComponent(margin='md'),
                    ButtonComponent(action=URIAction(label="✏️ 填寫今日生活照護日誌", uri=f"{BASE_URL}/log_form"), style='primary', color='#f4a261', height='sm'),
                ])
            ])
        )
    )

# ==================== 語音記帳強大自然語言解析器 ====================
def parse_voice_expense(text, sender_name):
    """
    解析語音輸入轉文字：
    '黃志龍買水果600元'
    '黃志龍 買水果 600'
    '志龍買尿布850'
    '塗雅芳買便當240元'
    '黃志嘉買午餐160'
    '買水果600元'
    '午餐160'
    """
    raw = text.strip()
    
    # 抽取金額
    m_num = re.search(r'(\d+)\s*(元|塊)?', raw)
    amount = 0
    num_str_matched = ""
    if m_num:
        amount = int(m_num.group(1))
        num_str_matched = m_num.group(0)
    else:
        cn_map = {'一': 1, '二': 2, '兩': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
        m_cn = re.search(r'([一二兩三四五六七八九])\s*百\s*([一二兩三四五六七八九])?\s*(十)?\s*(元|塊)?', raw)
        if m_cn:
            hundred = cn_map.get(m_cn.group(1), 0) * 100
            ten = (cn_map.get(m_cn.group(2), 0) * 10) if m_cn.group(2) else 0
            amount = hundred + ten
            num_str_matched = m_cn.group(0)

    if amount <= 0:
        return None

    # 判斷支出人（精準對齊 5 位家人）
    spender = clean_user_name(sender_name)
    rem_text = raw
    if any(k in raw for k in ["黃志嘉", "志嘉", "哥哥", "大哥"]):
        spender = "黃志嘉"
        rem_text = re.sub(r'黃志嘉|志嘉|哥哥|大哥', '', rem_text)
    elif any(k in raw for k in ["黃志龍", "志龍", "弟弟", "阿龍"]):
        spender = "黃志龍"
        rem_text = re.sub(r'黃志龍|志龍|弟弟|阿龍', '', rem_text)
    elif any(k in raw for k in ["黃芊甄", "芊甄", "姊姊", "姐姐", "大姊", "大姐"]):
        spender = "黃芊甄"
        rem_text = re.sub(r'黃芊甄|芊甄|姊姊|姐姐|大姊|大姐', '', rem_text)
    elif any(k in raw for k in ["黃蕙芬", "蕙芬", "Sandy", "sandy", "先迪", "妹妹", "小妹"]):
        spender = "黃蕙芬"
        rem_text = re.sub(r'黃蕙芬|蕙芬|Sandy|sandy|先迪|妹妹|小妹', '', rem_text)
    elif any(k in raw for k in ["塗雅芳", "雅芳", "弟妹"]):
        spender = "塗雅芳"
        rem_text = re.sub(r'塗雅芳|雅芳|弟妹', '', rem_text)

    # 抽取品項
    if num_str_matched:
        rem_text = rem_text.replace(num_str_matched, "")
    rem_text = re.sub(r'幫爸爸|給爸爸|買了|買|付了|付|花了|花|吃|費用|支出', '', rem_text).strip()
    rem_text = re.sub(r'^[，,。！!、\s]+|[，,。！!、\s]+$', '', rem_text)
    item = rem_text if rem_text else "日常採買"

    # 智慧分類
    if any(k in item for k in ["水果", "菜", "肉", "飯", "麵", "餐", "便當", "早餐", "午餐", "晚餐", "吃", "牛奶", "點心", "飲料", "米", "食物"]):
        category = "伙食餐飲"
    elif any(k in item for k in ["藥", "醫", "診", "掛號", "健保", "針", "紗布", "棉棒", "抽血", "複診"]):
        category = "醫療藥費"
    elif any(k in item for k in ["尿布", "紙巾", "衛生紙", "沐浴", "肥皂", "洗髮", "乳液", "牙刷", "毛巾", "看護墊", "濕紙巾", "耗品", "生活用品"]):
        category = "生活耗品"
    elif any(k in item for k in ["車", "計程車", "油", "加油", "高鐵", "捷運", "公車", "停車"]):
        category = "交通接送"
    else:
        category = "日常雜支"

    return {
        "spender": spender,
        "item": item,
        "amount": amount,
        "category": category
    }

# ==================== LINE 訊息處理核心 ====================
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    text = event.message.text.strip()
    user_id = event.source.user_id

    try:
        profile = line_bot_api.get_profile(user_id)
        display_name = profile.display_name
    except Exception:
        display_name = "家人"

    # 優先從綁定名單取得身分
    user_name = get_user_name_by_id(user_id, display_name)

    # ── 0. 身分綁定指令（手動觸發或點選卡片）──
    if text in ["綁定身分", "身分綁定", "重新綁定", "我是誰"]:
        line_bot_api.reply_message(event.reply_token, create_binding_flex())
        return

    if text.startswith("綁定身分 "):
        target = text.replace("綁定身分", "").strip()
        if target in ["黃志嘉", "黃志龍", "黃芊甄", "黃蕙芬", "塗雅芳"]:
            bind_user(user_id, target)
            roles = {"黃志嘉": "哥哥", "黃志龍": "弟弟", "黃芊甄": "姊姊", "黃蕙芬": "妹妹", "塗雅芳": "弟妹"}
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                text=f"🎉 身分綁定成功！\n\n已成功將您的 LINE 帳號綁定為：\n👉 {roles.get(target, '')}【{target}】\n\n今後您傳送語音或發票收據，系統將 100% 自動以您的名義入帳，姐姐月底結算精準零失誤！"
            ))
            return
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                text="⚠️ 請選擇正確家人姓名：黃志嘉、黃志龍、黃芊甄、黃蕙芬、塗雅芳。"
            ))
            return

    # ── 0b. 快速修改上一筆金額指令（例如「850」、「850元」、「金額 800」或「改為 800」）──
    is_pure_num = bool(re.match(r'^\d+\s*(元|塊)?$', text))
    is_modify_cmd = bool(re.match(r'^(金額|金額改為|修改金額|改為)\s*\d+', text) or (text.startswith("金額") and any(c.isdigit() for c in text)))

    if is_modify_cmd or (is_pure_num and len(text) <= 7):
        m_amt = re.search(r'\d+', text)
        if m_amt:
            new_amt = int(m_amt.group())
            ws_out = get_worksheet("支出明細")
            if ws_out:
                rows = ws_out.get_all_values()
                if len(rows) > 1:
                    last_idx = len(rows)
                    last_row = rows[-1]
                    old_amt = last_row[5] if len(last_row) > 5 else "0"
                    old_item = last_row[4] if len(last_row) > 4 else "品項"
                    old_spender = last_row[2] if len(last_row) > 2 else user_name
                    # 更新第 6 欄 (F欄 金額)
                    ws_out.update_cell(last_idx, 6, new_amt)
                    ws_out.update_cell(last_idx, 8, f"已更正金額 (原{old_amt}元)")
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(
                        text=f"✏️ 【已成功更正金額！】\n• 代墊人：{old_spender}\n• 品項：{old_item}\n• 原金額：{old_amt} 元\n• 修正後金額：{new_amt:,} 元\n\n已同步更新至 Google 試算表！"
                    ))
                    return
                else:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="目前尚無支出記錄可供修改！"))
                    return
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 目前試算表連線中，請稍候重試！"))
                return

    # ── 0c. 刪除上一筆指令 ──
    if text in ["刪除上一筆", "刪除剛才那一筆", "取消上一筆"]:
        ws_out = get_worksheet("支出明細")
        if ws_out:
            rows = ws_out.get_all_values()
            if len(rows) > 1:
                last_idx = len(rows)
                last_row = rows[-1]
                del_item = last_row[4] if len(last_row) > 4 else "品項"
                del_amt = last_row[5] if len(last_row) > 5 else "0"
                ws_out.delete_rows(last_idx)
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text=f"🗑️ 已為您刪除上一筆支出記錄：\n• 【{del_item} {del_amt}元】已自試算表中移除！"
                ))
                return
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="目前無支出記錄可刪除！"))
                return

    # ── 1. 六宮格按鈕 1：記一筆支出 ──
    if text in ["記一筆支出", "記帳", "支出", "1", "按鈕1"]:
        line_bot_api.reply_message(event.reply_token, create_expense_menu_flex())
        return

    # ── 1b. 語音記帳說明 ──
    if text in ["語音記帳說明", "語音輸入花費金額", "語音輸入"]:
        reply_msg = f"🎙️ 【語音記帳超簡單】\n\n"
        reply_msg += f"您只要按住 LINE 麥克風說話（或直接打字）：\n"
        reply_msg += f"👉 「買尿布 850」\n"
        reply_msg += f"👉 「買水果 600」\n"
        reply_msg += f"👉 「計程車 220」\n\n"
        reply_msg += f"系統將自動依您的綁定身分【{user_name}】直接寫入 Google 試算表！\n"
        reply_msg += f"（若幫其他家人代記，也可以說：「黃志龍買尿布850」）"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_msg))
        return

    # ── 2. 六宮格按鈕 2：專戶進項管理 ──
    if text in ["專戶進項管理", "進項管理", "收入", "進項", "2", "按鈕2", "房租照顧進項"]:
        line_bot_api.reply_message(event.reply_token, create_income_menu_flex())
        return

    # ── 3. 六宮格按鈕 3：本月收支總表 ──
    if text in ["本月收支總表", "收支總表", "看總表", "財務報表", "3", "按鈕3", "總表"]:
        now = datetime.now()
        month_str = f"{now.year}年{now.month}月"
        ws_in  = get_worksheet("進項明細")
        ws_out = get_worksheet("支出明細")
        
        in_rows  = ws_in.get_all_values() if ws_in else []
        out_rows = ws_out.get_all_values() if ws_out else []

        total_income = 0
        cur_prefix = f"{now.year}/{now.month:02d}"
        alt_prefix = f"{now.year}-{now.month:02d}"

        if in_rows and len(in_rows) > 1:
            for r in in_rows[1:]:
                d = r[1] if len(r) > 1 else ""
                amt = clean_num(r[5]) if len(r) > 5 else 0
                if d.startswith(cur_prefix) or d.startswith(alt_prefix) or f"{now.month}月" in d:
                    total_income += amt

        total_expense = 0
        categories = {"伙食餐飲": 0, "生活耗品": 0, "醫療藥費": 0, "交通接送": 0, "日常雜支": 0}
        if out_rows and len(out_rows) > 1:
            for r in out_rows[1:]:
                d = r[1] if len(r) > 1 else ""
                cat = r[3] if len(r) > 3 else "日常雜支"
                amt = clean_num(r[5]) if len(r) > 5 else 0
                if d.startswith(cur_prefix) or d.startswith(alt_prefix) or f"{now.month}月" in d:
                    total_expense += amt
                    found = False
                    for k in categories:
                        if k in cat:
                            categories[k] += amt
                            found = True
                            break
                    if not found:
                        categories["日常雜支"] += amt

        # 照顧專戶起始結算基準 (2026/10/1 姐姐結算基準: 635,008 元)
        BASE_STARTING_BALANCE = 635008
        base_balance = BASE_STARTING_BALANCE
        ws_sum = get_worksheet("收支總表")
        if ws_sum:
            try:
                sum_rows = ws_sum.get_all_values()
                if len(sum_rows) > 1:
                    # 優先檢查是否有手動透過指令設定的最新基準總額
                    for sr in reversed(sum_rows[1:]):
                        note = sr[5] if len(sr) > 5 else ""
                        if "設定/結算專戶基準總額" in note or "期初基準結餘" in note:
                            b_val = clean_num(sr[4]) if len(sr) > 4 else 0
                            if b_val > 0:
                                base_balance = b_val
                                break
            except Exception as se:
                print(f"讀取收支總表基準失敗: {se}")

        net_bal = total_income - total_expense
        current_balance = base_balance + net_bal

        # 自動回寫/同步更新「收支總表」工作表當前月份資料
        if ws_sum:
            try:
                # 若已有當月，更新之；若無則新增
                sum_rows = ws_sum.get_all_values()
                found_idx = -1
                for idx, sr in enumerate(sum_rows[1:], start=2):
                    if len(sr) > 0 and (month_str in sr[0] or f"{now.year}/{now.month:02d}" in sr[0]):
                        found_idx = idx
                        break
                if found_idx > 0:
                    ws_sum.update(f"A{found_idx}:F{found_idx}", [[
                        month_str, total_income, total_expense, net_bal, current_balance,
                        f"自動結算 (基準{base_balance:,} + 進項{total_income} - 支出{total_expense})"
                    ]])
                else:
                    ws_sum.append_row([
                        month_str, total_income, total_expense, net_bal, current_balance,
                        f"自動結算 (基準{base_balance:,} + 進項{total_income} - 支出{total_expense})"
                    ])
            except Exception as e:
                print(f"同步收支總表失敗: {e}")

        line_bot_api.reply_message(event.reply_token, create_summary_flex(
            month_str, total_income, total_expense, net_bal, categories,
            base_balance=base_balance, current_balance=current_balance
        ))
        return

    # ── 3b. 姐姐月底結帳專用：各人代墊款彙整 ──
    if text in ["各人代墊結算", "代墊結算", "結算代墊", "代墊款", "代墊", "各人代墊"]:
        now = datetime.now()
        month_str = f"{now.year}年{now.month}月"
        ws_out = get_worksheet("支出明細")
        out_rows = ws_out.get_all_values() if ws_out else []

        cur_prefix = f"{now.year}/{now.month:02d}"
        alt_prefix = f"{now.year}-{now.month:02d}"

        member_expenses = {
            "黃志嘉": 0,
            "黃志龍": 0,
            "黃芊甄": 0,
            "黃蕙芬": 0,
            "塗雅芳": 0
        }
        total_expense = 0

        if out_rows and len(out_rows) > 1:
            for r in out_rows[1:]:
                d = r[1] if len(r) > 1 else ""
                who = clean_user_name(r[2]) if len(r) > 2 else "其他"
                amt = clean_num(r[5]) if len(r) > 5 else 0
                if d.startswith(cur_prefix) or d.startswith(alt_prefix) or f"{now.month}月" in d:
                    total_expense += amt
                    if who in member_expenses:
                        member_expenses[who] += amt
                    else:
                        member_expenses[who] = member_expenses.get(who, 0) + amt

        line_bot_api.reply_message(event.reply_token, create_settlement_flex(
            month_str, member_expenses, total_expense
        ))
        return

    # ── 4. 六宮格按鈕 4：醫院回診領藥 ──
    if text in ["醫院回診領藥", "回診", "領藥", "看診", "4", "按鈕4", "醫院回診"]:
        ws_c = get_worksheet("回診與領藥")
        c_rows = ws_c.get_all_values() if ws_c else []
        valid_rows = [r for r in c_rows[1:] if len(r) > 0 and r[0].strip()] if len(c_rows) > 1 else []
        line_bot_api.reply_message(event.reply_token, create_clinic_flex(valid_rows))
        return

    # ── 4b. 查看慢性處方箋領藥 ──
    if any(k in text for k in ["慢箋", "慢性處方箋", "領藥區間"]):
        msg = "💊 【高醫 慢性病連續處方箋領藥叮嚀】\n\n"
        msg += "• 就診醫院：高雄醫學大學附設醫院（高醫）\n"
        msg += "• 照護科別：神經內科 (失智認知) ＆ 新陳代謝科 (血糖/血壓)\n"
        msg += "• 領藥地點：住家鄰近健保藥局（如大樹藥局）或高醫領藥處\n"
        msg += "• 必備證件：爸爸健保卡正本 ＋ 慢箋紙本第二/三次聯\n"
        msg += "• 叮嚀事項：慢箋有效領藥期間為上次領藥後 28~30 天起，請留意藥袋標示日期！"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        return

    # ── 5. 六宮格按鈕 5：目前用藥手冊 ──
    if text in ["目前用藥手冊", "用藥清單", "用藥", "吃藥", "5", "按鈕5", "目前用藥與叮嚀"]:
        ws_m = get_worksheet("目前用藥")
        m_rows = ws_m.get_all_values() if ws_m else []
        valid_rows = [r for r in m_rows[1:] if len(r) > 0 and r[0].strip()] if len(m_rows) > 1 else []
        line_bot_api.reply_message(event.reply_token, create_medication_flex(valid_rows))
        return

    # ── 6. 六宮格按鈕 6：爸爸生活照護 ──
    if text in ["爸爸生活照護", "生活照護", "照護日誌", "日記", "6", "按鈕6", "生活日誌"]:
        ws_l = get_worksheet("照護日誌")
        l_rows = ws_l.get_all_values() if ws_l else []
        valid_rows = [r for r in l_rows[1:] if len(r) > 0 and r[0].strip()] if len(l_rows) > 1 else []
        valid_rows.reverse()
        line_bot_api.reply_message(event.reply_token, create_care_log_flex(valid_rows))
        return

    # ── 7. 查詢最近支出明細 ──
    if any(k in text for k in ["查詢最近支出", "最近支出", "支出明細", "查支出", "看支出"]):
        ws_out = get_worksheet("支出明細")
        rows = ws_out.get_all_values() if ws_out else []
        if len(rows) > 1:
            recent = rows[-5:]
            recent.reverse()
            msg = "📋 【最近支出明細記錄】\n\n"
            for r in recent:
                d = r[1] if len(r) > 1 else ""
                who = r[2] if len(r) > 2 else ""
                item = r[4] if len(r) > 4 else ""
                amt = r[5] if len(r) > 5 else "0"
                msg += f"• {d} [{who}] {item}：{amt} 元\n"
            msg += f"\n💡 點擊【本月收支總表】可看即時月結餘！"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg.strip()))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="目前尚無支出紀錄，歡迎用語音記帳（如「黃志龍買水果600元」）！"))
        return

    # ── 8. 專戶進項管理（房租實收、老人年金、債券、利息）──
    if text in ["登記房租入帳", "登記房租"]:
        msg = "🏠 【登記本月房屋租金實收淨額】\n\n"
        msg += "因每月可能有修繕支出或扣繳管理費，請直接輸入扣除後的實收金額，例如輸入：\n"
        msg += "👉 `房租 18500` 或 `房租 20000`\n\n"
        msg += "系統將以黃蕙芬名義自動記錄至進項明細！"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        return

    if "房租" in text:
        match = re.search(r'\d+', text)
        if match:
            amt = int(match.group())
            today_str = datetime.now().strftime("%Y/%m/%d")
            now_time = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
            ws = get_worksheet("進項明細")
            if ws:
                ws.append_row([now_time, today_str, "黃蕙芬", "房屋租金", f"{datetime.now().month}月份房屋租金(實收淨額)", amt, "實收淨額登記"])
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text=f"✅ 已成功登記本月房租淨額！\n• 登記人：黃蕙芬\n• 項目：房屋租金實收\n• 金額：{amt:,} 元\n• 入帳日期：{today_str}\n已自動歸入專戶總額！"
                ))
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 目前試算表連線中，請稍候重試！"))
            return
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                text="🏠 請輸入扣除修繕與管理費後的實收房租金額，例如輸入：`房租 18500`！"
            ))
            return

    if text in ["登記老人年金", "老人年金入帳"]:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            text="🏛️ 請回傳老人年金實收金額（例如直接輸入：`老人年金 4164`），系統將以黃芊甄(姊姊)名義自動入帳！"
        ))
        return

    if "老人年金" in text:
        match = re.search(r'\d+', text)
        if match:
            amt = int(match.group())
            today_str = datetime.now().strftime("%Y/%m/%d")
            now_time = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
            ws = get_worksheet("進項明細")
            if ws:
                ws.append_row([now_time, today_str, "黃芊甄", "政府津貼", f"{datetime.now().month}月份老人年金", amt, "網銀核對登記"])
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text=f"✅ 已成功登記老人年金入帳！\n• 登記人：黃芊甄\n• 項目：政府津貼(老人年金)\n• 金額：{amt:,} 元\n• 入帳日期：{today_str}\n已計入專戶本月總收入！"
                ))
            return
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🏛️ 請輸入本月老人年金金額，例如：`老人年金 4164`！"))
            return

    if text in ["登記債券收益", "登記債券"]:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            text="📈 請回傳本期債券配息金額（例如直接輸入：`債券 15000`），系統將以黃芊甄(姊姊)名義自動入帳！"
        ))
        return

    if "債券" in text:
        match = re.search(r'\d+', text)
        if match:
            amt = int(match.group())
            today_str = datetime.now().strftime("%Y/%m/%d")
            now_time = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
            ws = get_worksheet("進項明細")
            if ws:
                ws.append_row([now_time, today_str, "黃芊甄", "債券收益", "債券利息配息收益", amt, "網銀核對登記"])
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text=f"✅ 已成功登記債券收益入帳！\n• 登記人：黃芊甄\n• 項目：債券配息收益\n• 金額：{amt:,} 元\n• 入帳日期：{today_str}\n已計入專戶！"
                ))
            return
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="📈 請輸入本期債券配息金額，例如：`債券 15000`！"))
            return

    if text in ["登記利息收入", "登記利息"]:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            text="💰 請回傳銀行存款利息金額（例如直接輸入：`利息 250`），系統將以黃芊甄(姊姊)名義自動入帳！"
        ))
        return

    if "利息" in text:
        match = re.search(r'\d+', text)
        if match:
            amt = int(match.group())
            today_str = datetime.now().strftime("%Y/%m/%d")
            now_time = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
            ws = get_worksheet("進項明細")
            if ws:
                ws.append_row([now_time, today_str, "黃芊甄", "存款利息", "銀行存款利息", amt, "網銀核對登記"])
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text=f"✅ 已成功登記利息收入！\n• 登記人：黃芊甄\n• 項目：銀行利息\n• 金額：{amt:,} 元\n• 入帳日期：{today_str}\n已計入專戶！"
                ))
            return
    # ── 8b. 設定/修正專戶結算基準總額（例如：修正總金額 635008、設定總額 635008）──
    adjust_keywords = ["修正總金額", "修正總額", "修正餘額", "修正總結餘", "修改總額", "修改餘額", "校正總額", "校正餘額", "調整總額", "調整餘額", "設定總額", "更新總額", "結算總額", "修正金額"]
    if any(kw in text for kw in adjust_keywords):
        match = re.search(r'\d+', text)
        if match:
            new_base = int(match.group())
            # 取得自訂備註說明（去除關鍵字與數字）
            custom_note = text
            for kw in adjust_keywords:
                custom_note = custom_note.replace(kw, "")
            custom_note = custom_note.replace(str(new_base), "").replace("，", "").replace(",", "").strip()
            if not custom_note:
                custom_note = "月底核對存摺對帳校正"
            
            today_str = datetime.now().strftime("%Y/%m/%d")
            now = datetime.now()
            month_str = f"{now.year}年{now.month}月"
            ws_sum = get_worksheet("收支總表")
            if ws_sum:
                note_str = f"期初基準結餘 {new_base:,} 元 ({user_name}於 {today_str} {custom_note})"
                # 尋找當月列更新，或新增一列
                sum_rows = ws_sum.get_all_values()
                found_idx = -1
                for idx, sr in enumerate(sum_rows[1:], start=2):
                    if len(sr) > 0 and (month_str in sr[0] or f"{now.year}/{now.month:02d}" in sr[0]):
                        found_idx = idx
                        break
                if found_idx > 0:
                    ws_sum.update(f"A{found_idx}:F{found_idx}", [[
                        month_str, 0, 0, 0, new_base, note_str
                    ]])
                else:
                    ws_sum.append_row([
                        month_str, 0, 0, 0, new_base, note_str
                    ])

                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text=f"✅ 已成功修正照顧專戶總結餘！\n\n"
                         f"• 操作人：{user_name}\n"
                         f"• 最新專戶總結餘：${new_base:,} 元\n"
                         f"• 校正日期：{today_str}\n"
                         f"• 備註說明：{custom_note}\n\n"
                         f"系統已將專戶總結餘校正完畢，後續所有進項與支出都將以此基準持續累計！"
                ))
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 目前試算表連線中，請稍候重試！"))
            return
        else:
            # 使用者只輸入了「修正總金額」等指令（未帶金額），跳出引導卡片
            ws_sum = get_worksheet("收支總表")
            current_bal = 635008
            if ws_sum:
                try:
                    s_rows = ws_sum.get_all_values()
                    if len(s_rows) > 1:
                        last_row = s_rows[-1]
                        current_bal = clean_num(last_row[4]) if len(last_row) > 4 else 635008
                except Exception:
                    pass
            line_bot_api.reply_message(event.reply_token, create_adjust_balance_guide_flex(current_bal))
            return


    # ── 9. 強大語音輸入記帳辨識（例如：「黃志龍買水果600元」）──
    expense_data = parse_voice_expense(text, user_name)
    if expense_data:
        today_str = datetime.now().strftime("%Y/%m/%d")
        now_time = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
        ws = get_worksheet("支出明細")
        if ws:
            ws.append_row([
                now_time, today_str, expense_data["spender"], expense_data["category"],
                expense_data["item"], expense_data["amount"], "", "LINE語音快速記帳"
            ])
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                text=f"✅ 【語音記帳成功！】\n• 購買日期：{today_str}\n• 支出代墊：{expense_data['spender']}\n• 費用類別：{expense_data['category']}\n• 購買品項：{expense_data['item']}\n• 支出金額：{expense_data['amount']:,} 元\n\n已同步寫入 Google 試算表，感謝用心照顧！"
            ))
            return
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                text=f"⚠️ 辨識到記帳：{expense_data['spender']} 買 {expense_data['item']} {expense_data['amount']}元，但目前試算表連線中，請稍候重試！"
            ))
            return

    # ── 通用回應 ──
    line_bot_api.reply_message(event.reply_token, TextSendMessage(
        text=f"您好，{user_name}！爸爸照顧小秘書隨時為全家人服務。\n\n🎙️ 快速記帳只要說：\n「黃志龍買水果600元」或「黃志龍買尿布850」\n\n💡 或點選下方圖文選單查看收支總表與高醫回診！"
    ))

@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event):
    user_id = event.source.user_id
    try:
        profile = line_bot_api.get_profile(user_id)
        display_name = profile.display_name
    except:
        display_name = "家人"

    user_name = get_user_name_by_id(user_id, display_name)
    today_str = datetime.now().strftime("%Y/%m/%d")
    now_time = datetime.now().strftime("%Y/%m/%d %H:%M:%S")

    # 1. 下載 LINE 圖片二進制內容
    message_content = line_bot_api.get_message_content(event.message.id)
    image_bytes = b""
    for chunk in message_content.iter_content():
        image_bytes += chunk

    # 2. 調用 Gemini Flash 視覺模型自動分析發票/收據
    parsed = analyze_receipt_with_gemini(image_bytes)

    if parsed and parsed.get("amount", 0) > 0:
        amt = parsed["amount"]
        item = parsed.get("item", "日常採買")
        cat = parsed.get("category", "日常雜支")

        ws = get_worksheet("支出明細")
        if ws:
            ws.append_row([
                now_time, today_str, user_name, cat, item, amt, "", "AI發票拍照自動辨識"
            ])
            reply_text = f"📸 【AI 發票自動辨識記帳成功！】\n"
            reply_text += f"• 支出代墊：{user_name}\n"
            reply_text += f"• 辨識品項：{item}\n"
            reply_text += f"• 費用分類：{cat}\n"
            reply_text += f"• 發票金額：{amt:,} 元\n"
            reply_text += f"• 記帳日期：{today_str}\n\n"
            reply_text += f"✅ 已直接寫入 Google 試算表！\n"
            reply_text += f"💡 若金額有辨識偏差，可直接輸入「金額改為 800」即時修正！"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
            return

    # 若未辨識出金額（例如純日常照片、或收據模糊）
    reply_text = f"📸 收到【{user_name}】傳送的照片！\n\n"
    reply_text += "🧾 若為【發票/收據報銷】：\n"
    reply_text += f"因照片金額較為模糊，請直接回傳金額（例如輸入：`850` 或 `尿布 850`），系統將自動以【{user_name}】名義入帳！\n\n"
    reply_text += "💡 若要修改上一筆，隨時輸入「金額改為 XXX」即可！"
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text.strip()))

# ==================== Web 網頁表單 (記帳、進項、高醫回診、用藥、日誌) ====================

EXPENSE_FORM_HTML = """
<!DOCTYPE html>
<html lang="zh-TW">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>記一筆支出 ｜ 爸爸照顧專戶</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f8f9fa; margin: 0; padding: 15px; color: #333; }
    .card { background: #fff; border-radius: 12px; padding: 20px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); max-width: 480px; margin: 0 auto; }
    h2 { color: #e63946; margin-top: 0; font-size: 20px; border-bottom: 2px solid #f1faee; padding-bottom: 10px; }
    .form-group { margin-bottom: 15px; }
    label { display: block; font-size: 14px; font-weight: bold; margin-bottom: 5px; color: #495057; }
    input, select, textarea { width: 100%; padding: 12px; border: 1px solid #ced4da; border-radius: 8px; font-size: 15px; box-sizing: border-box; }
    button { width: 100%; background: #e63946; color: white; border: none; padding: 14px; border-radius: 8px; font-size: 16px; font-weight: bold; cursor: pointer; margin-top: 10px; }
    .success { background: #d4edda; color: #155724; padding: 15px; border-radius: 8px; text-align: center; font-weight: bold; }
  </style>
</head>
<body>
  <div class="card">
    {% if success %}
      <div class="success">
        🎉 支出登記成功！<br>已同步寫入 Google 試算表，謝謝！
      </div>
    {% else %}
      <h2>💰 登記一筆支出費用</h2>
      <form method="POST">
        <div class="form-group">
          <label>購買日期</label>
          <input type="date" name="date" value="{{ today }}" required>
        </div>
        <div class="form-group">
          <label>支出人 / 代墊人</label>
          <select name="spender" required>
            <option value="黃志龍">黃志龍 (弟弟)</option>
            <option value="塗雅芳">塗雅芳 (弟妹)</option>
            <option value="黃志嘉">黃志嘉 (哥哥)</option>
            <option value="黃芊甄">黃芊甄 (姊姊)</option>
            <option value="黃蕙芬">黃蕙芬 (Sandy)</option>
          </select>
        </div>
        <div class="form-group">
          <label>費用類別</label>
          <select name="category" required>
            <option value="伙食餐飲">伙食餐飲 (日常買菜/水果/外食)</option>
            <option value="生活耗品">生活耗品 (尿布/紙巾/沐浴/營養品)</option>
            <option value="醫療藥費">醫療藥費 (高醫門診/掛號/藥費)</option>
            <option value="交通接送">交通接送 (計程車/油資)</option>
            <option value="日常雜支">日常雜支 (生活雜項/維修)</option>
          </select>
        </div>
        <div class="form-group">
          <label>購買品項名稱</label>
          <input type="text" name="item" placeholder="例如：水果、成人尿布 2包" required>
        </div>
        <div class="form-group">
          <label>支出金額 (元)</label>
          <input type="number" name="amount" placeholder="例如：600" required>
        </div>
        <div class="form-group">
          <label>備註說明</label>
          <textarea name="note" rows="2" placeholder="選填"></textarea>
        </div>
        <button type="submit">確認登記並送出</button>
      </form>
    {% endif %}
  </div>
</body>
</html>
"""

@app.route("/expense_form", methods=['GET', 'POST'])
def expense_form():
    today = datetime.now().strftime("%Y-%m-%d")
    if request.method == 'POST':
        date_val = request.form.get("date", today).replace("-", "/")
        spender  = request.form.get("spender", "")
        category = request.form.get("category", "")
        item     = request.form.get("item", "")
        amount   = clean_num(request.form.get("amount", "0"))
        note     = request.form.get("note", "")
        now_time = datetime.now().strftime("%Y/%m/%d %H:%M:%S")

        ws = get_worksheet("支出明細")
        if ws:
            ws.append_row([now_time, date_val, spender, category, item, amount, "", note])

        return render_template_string(EXPENSE_FORM_HTML, success=True)
    return render_template_string(EXPENSE_FORM_HTML, success=False, today=today)

INCOME_FORM_HTML = """
<!DOCTYPE html>
<html lang="zh-TW">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>登記專戶進項 ｜ 爸爸照顧專戶</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f8f9fa; margin: 0; padding: 15px; color: #333; }
    .card { background: #fff; border-radius: 12px; padding: 20px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); max-width: 480px; margin: 0 auto; }
    h2 { color: #2a9d8f; margin-top: 0; font-size: 20px; border-bottom: 2px solid #e9ecef; padding-bottom: 10px; }
    .form-group { margin-bottom: 15px; }
    label { display: block; font-size: 14px; font-weight: bold; margin-bottom: 5px; color: #495057; }
    input, select, textarea { width: 100%; padding: 12px; border: 1px solid #ced4da; border-radius: 8px; font-size: 15px; box-sizing: border-box; }
    button { width: 100%; background: #2a9d8f; color: white; border: none; padding: 14px; border-radius: 8px; font-size: 16px; font-weight: bold; cursor: pointer; margin-top: 10px; }
    .success { background: #d4edda; color: #155724; padding: 15px; border-radius: 8px; text-align: center; font-weight: bold; }
  </style>
</head>
<body>
  <div class="card">
    {% if success %}
      <div class="success">
        🎉 進項收入登記成功！<br>已記錄至專戶明細，謝謝！
      </div>
    {% else %}
      <h2>🏠 登記專戶進項收入</h2>
      <form method="POST">
        <div class="form-group">
          <label>入帳日期</label>
          <input type="date" name="date" value="{{ today }}" required>
        </div>
        <div class="form-group">
          <label>登記人</label>
          <select name="recorder" required>
            <option value="黃蕙芬">黃蕙芬 (Sandy)</option>
            <option value="黃芊甄">黃芊甄 (姊姊)</option>
            <option value="黃志嘉">黃志嘉 (哥哥)</option>
            <option value="黃志龍">黃志龍 (弟弟)</option>
            <option value="塗雅芳">塗雅芳 (弟妹)</option>
          </select>
        </div>
        <div class="form-group">
          <label>進項來源類別</label>
          <select name="category" required>
            <option value="房屋租金">房屋租金 (黃蕙芬 實收淨額匯入)</option>
            <option value="政府津貼">政府老人年金/津貼 (黃芊甄)</option>
            <option value="債券收益">債券利息/配息收益 (黃芊甄)</option>
            <option value="存款利息">銀行活存/定存利息 (黃芊甄)</option>
            <option value="其他收入">其他專戶入帳</option>
          </select>
        </div>
        <div class="form-group">
          <label>進項說明</label>
          <input type="text" name="desc" placeholder="例如：10月份房屋租金(扣除維修/管理費) 或 老人年金" required>
        </div>
        <div class="form-group">
          <label>入帳金額 (元)</label>
          <input type="number" name="amount" placeholder="例如：18500" required>
        </div>
        <button type="submit">確認入帳並送出</button>
      </form>
    {% endif %}
  </div>
</body>
</html>
"""

@app.route("/income_form", methods=['GET', 'POST'])
def income_form():
    today = datetime.now().strftime("%Y-%m-%d")
    if request.method == 'POST':
        date_val = request.form.get("date", today).replace("-", "/")
        recorder = request.form.get("recorder", "")
        category = request.form.get("category", "")
        desc     = request.form.get("desc", "")
        amount   = clean_num(request.form.get("amount", "0"))
        note     = request.form.get("note", "")
        now_time = datetime.now().strftime("%Y/%m/%d %H:%M:%S")

        ws = get_worksheet("進項明細")
        if ws:
            ws.append_row([now_time, date_val, recorder, category, desc, amount, note])

        return render_template_string(INCOME_FORM_HTML, success=True)
    return render_template_string(INCOME_FORM_HTML, success=False, today=today)

# ==================== 高醫專屬回診預約表單 ====================
CLINIC_FORM_HTML = """
<!DOCTYPE html>
<html lang="zh-TW">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>預約高醫回診 ｜ 爸爸照顧專戶</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f8f9fa; margin: 0; padding: 15px; color: #333; }
    .card { background: #fff; border-radius: 12px; padding: 20px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); max-width: 480px; margin: 0 auto; }
    h2 { color: #457b9d; margin-top: 0; font-size: 20px; border-bottom: 2px solid #e9ecef; padding-bottom: 10px; }
    .form-group { margin-bottom: 15px; }
    label { display: block; font-size: 14px; font-weight: bold; margin-bottom: 5px; color: #495057; }
    input, select, textarea { width: 100%; padding: 12px; border: 1px solid #ced4da; border-radius: 8px; font-size: 15px; box-sizing: border-box; }
    .fixed-box { background: #e9ecef; padding: 12px; border-radius: 8px; font-weight: bold; color: #1d3557; }
    button { width: 100%; background: #457b9d; color: white; border: none; padding: 14px; border-radius: 8px; font-size: 16px; font-weight: bold; cursor: pointer; margin-top: 10px; }
    .success { background: #d4edda; color: #155724; padding: 15px; border-radius: 8px; text-align: center; font-weight: bold; }
  </style>
</head>
<body>
  <div class="card">
    {% if success %}
      <div class="success">
        🎉 高醫回診預約成功！<br>已登記排程，看診前小秘書將推播提醒大家！
      </div>
    {% else %}
      <h2>🏥 高醫門診回診登記</h2>
      <form method="POST">
        <div class="form-group">
          <label>就診醫院</label>
          <div class="fixed-box">高雄醫學大學附設中和紀念醫院 (高醫)</div>
        </div>
        <div class="form-group">
          <label>看診科別</label>
          <select name="dept" required>
            <option value="神經內科 (失智認知)">神經內科 (失智認知退化專科)</option>
            <option value="新陳代謝科 (血糖血壓)">新陳代謝科 (血糖與血壓控制)</option>
            <option value="其他專科">其他專科門診</option>
          </select>
        </div>
        <div class="form-group">
          <label>看診日期</label>
          <input type="date" name="date" required>
        </div>
        <div class="form-group">
          <label>就診時段</label>
          <select name="period">
            <option value="上午診">上午診 (約 09:00 ~ 12:00)</option>
            <option value="下午診">下午診 (約 14:00 ~ 17:00)</option>
          </select>
        </div>
        <div class="form-group">
          <label>預約主治醫師</label>
          <input type="text" name="doctor" placeholder="例如：陳醫師" required>
        </div>
        <div class="form-group">
          <label>掛號號碼</label>
          <input type="text" name="queue_no" placeholder="例如：23 號">
        </div>
        <div class="form-group">
          <label>看診備註 / 慢箋提醒</label>
          <textarea name="note" rows="2" placeholder="選填，如：需空腹抽血、領第2次慢箋"></textarea>
        </div>
        <button type="submit">儲存高醫回診預約</button>
      </form>
    {% endif %}
  </div>
</body>
</html>
"""

@app.route("/clinic_form", methods=['GET', 'POST'])
def clinic_form():
    if request.method == 'POST':
        dept     = request.form.get("dept", "神經內科")
        date_val = request.form.get("date", "").replace("-", "/")
        period   = request.form.get("period", "上午診")
        doctor   = request.form.get("doctor", "")
        queue_no = request.form.get("queue_no", "")
        note     = request.form.get("note", "")

        ws = get_worksheet("回診與領藥")
        if ws:
            ws.append_row([date_val, period, "高醫", dept, doctor, queue_no, note, "待提醒"])

        return render_template_string(CLINIC_FORM_HTML, success=True)
    return render_template_string(CLINIC_FORM_HTML, success=False)

# ==================== 用藥維護表單 ====================
MED_FORM_HTML = """
<!DOCTYPE html>
<html lang="zh-TW">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>新增用藥 ｜ 爸爸照顧專戶</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f8f9fa; margin: 0; padding: 15px; color: #333; }
    .card { background: #fff; border-radius: 12px; padding: 20px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); max-width: 480px; margin: 0 auto; }
    h2 { color: #6a4c93; margin-top: 0; font-size: 20px; border-bottom: 2px solid #e9ecef; padding-bottom: 10px; }
    .form-group { margin-bottom: 15px; }
    label { display: block; font-size: 14px; font-weight: bold; margin-bottom: 5px; color: #495057; }
    input, select, textarea { width: 100%; padding: 12px; border: 1px solid #ced4da; border-radius: 8px; font-size: 15px; box-sizing: border-box; }
    button { width: 100%; background: #6a4c93; color: white; border: none; padding: 14px; border-radius: 8px; font-size: 16px; font-weight: bold; cursor: pointer; margin-top: 10px; }
    .success { background: #d4edda; color: #155724; padding: 15px; border-radius: 8px; text-align: center; font-weight: bold; }
  </style>
</head>
<body>
  <div class="card">
    {% if success %}
      <div class="success">
        🎉 藥品新增成功！<br>已同步更新至現行用藥手冊！
      </div>
    {% else %}
      <h2>💊 新增/維護現行用藥</h2>
      <form method="POST">
        <div class="form-group">
          <label>藥品名稱</label>
          <input type="text" name="name" placeholder="例如：憶思能 或 庫魯化錠" required>
        </div>
        <div class="form-group">
          <label>作用 / 用途</label>
          <input type="text" name="usage" placeholder="例如：神經內科/失智記憶保養、新陳代謝科/血糖與血壓控制" required>
        </div>
        <div class="form-group">
          <label>服用時段</label>
          <select name="timing" required>
            <option value="早上">早上</option>
            <option value="早上、晚上">早上、晚上</option>
            <option value="三餐">三餐飯後</option>
            <option value="睡前">睡前</option>
          </select>
        </div>
        <div class="form-group">
          <label>用法 (飯前/飯後)</label>
          <select name="meal">
            <option value="飯後">飯後</option>
            <option value="隨餐或飯後">隨餐或飯後</option>
            <option value="飯前">飯前</option>
            <option value="睡前30分鐘">睡前30分鐘</option>
          </select>
        </div>
        <div class="form-group">
          <label>注意事項 / 叮嚀</label>
          <textarea name="note" rows="2" placeholder="選填，如：不可咬碎、注意低血糖"></textarea>
        </div>
        <button type="submit">儲存至用藥手冊</button>
      </form>
    {% endif %}
  </div>
</body>
</html>
"""

@app.route("/med_form", methods=['GET', 'POST'])
def med_form():
    if request.method == 'POST':
        name   = request.form.get("name", "")
        usage  = request.form.get("usage", "")
        timing = request.form.get("timing", "早上")
        meal   = request.form.get("meal", "飯後")
        note   = request.form.get("note", "")

        ws = get_worksheet("目前用藥")
        if ws:
            ws.append_row([name, usage, "每日規則服用", timing, meal, note, ""])

        return render_template_string(MED_FORM_HTML, success=True)
    return render_template_string(MED_FORM_HTML, success=False)

# ==================== 照護日誌表單 ====================
LOG_FORM_HTML = """
<!DOCTYPE html>
<html lang="zh-TW">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>生活照護日誌 ｜ 爸爸照顧專戶</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f8f9fa; margin: 0; padding: 15px; color: #333; }
    .card { background: #fff; border-radius: 12px; padding: 20px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); max-width: 480px; margin: 0 auto; }
    h2 { color: #f4a261; margin-top: 0; font-size: 20px; border-bottom: 2px solid #e9ecef; padding-bottom: 10px; }
    .form-group { margin-bottom: 15px; }
    label { display: block; font-size: 14px; font-weight: bold; margin-bottom: 5px; color: #495057; }
    input, select, textarea { width: 100%; padding: 12px; border: 1px solid #ced4da; border-radius: 8px; font-size: 15px; box-sizing: border-box; }
    button { width: 100%; background: #f4a261; color: white; border: none; padding: 14px; border-radius: 8px; font-size: 16px; font-weight: bold; cursor: pointer; margin-top: 10px; }
    .success { background: #d4edda; color: #155724; padding: 15px; border-radius: 8px; text-align: center; font-weight: bold; }
  </style>
</head>
<body>
  <div class="card">
    {% if success %}
      <div class="success">
        🎉 照護日誌記錄成功！<br>兄弟姊妹隨時都看得到爸爸最新動態！
      </div>
    {% else %}
      <h2>📋 填寫今日生活照護日誌</h2>
      <form method="POST">
        <div class="form-group">
          <label>記錄人</label>
          <select name="author">
            <option value="黃志龍">黃志龍 (弟弟)</option>
            <option value="塗雅芳">塗雅芳 (弟妹)</option>
            <option value="黃志嘉">黃志嘉 (哥哥)</option>
            <option value="黃芊甄">黃芊甄 (姊姊)</option>
            <option value="黃蕙芬">黃蕙芬 (Sandy)</option>
          </select>
        </div>
        <div class="form-group">
          <label>精神與心情狀況</label>
          <input type="text" name="mood" placeholder="例如：精神很好、愛笑、黃昏時微焦躁" required>
        </div>
        <div class="form-group">
          <label>飲食與食慾</label>
          <input type="text" name="appetite" placeholder="例如：三餐正常、吃一整碗粥、胃口良好">
        </div>
        <div class="form-group">
          <label>量測數據 (選填)</label>
          <input type="text" name="vitals" placeholder="例如：血壓 125/80、血糖 110">
        </div>
        <div class="form-group">
          <label>今日活動 / 特殊狀況記錄</label>
          <textarea name="activity" rows="3" placeholder="例如：下午有到公園散步 30 分鐘，心情放鬆。"></textarea>
        </div>
        <button type="submit">送出今日日誌</button>
      </form>
    {% endif %}
  </div>
</body>
</html>
"""

@app.route("/log_form", methods=['GET', 'POST'])
def log_form():
    if request.method == 'POST':
        author   = request.form.get("author", "")
        mood     = request.form.get("mood", "")
        appetite = request.form.get("appetite", "")
        vitals   = request.form.get("vitals", "")
        activity = request.form.get("activity", "")
        now_time = datetime.now().strftime("%Y/%m/%d %H:%M:%S")

        ws = get_worksheet("照護日誌")
        if ws:
            ws.append_row([now_time, author, mood, appetite, vitals, activity, ""])

        return render_template_string(LOG_FORM_HTML, success=True)
    return render_template_string(LOG_FORM_HTML, success=False)

# ==================== 專戶總結餘修正表單 ====================
ADJUST_FORM_HTML = """
<!DOCTYPE html>
<html lang="zh-TW">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>修正專戶總結餘 ｜ 爸爸照顧專戶</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f0f2f5; margin: 0; padding: 15px; color: #333; }
    .card { background: #fff; border-radius: 14px; padding: 22px; box-shadow: 0 4px 16px rgba(0,0,0,0.08); max-width: 480px; margin: 0 auto; }
    .header { text-align: center; margin-bottom: 20px; }
    h2 { color: #1d3557; margin: 0 0 6px 0; font-size: 21px; }
    .subtitle { color: #6c757d; font-size: 13px; margin: 0; }
    .current-box { background: #f1faee; border: 1px solid #a8dadc; border-radius: 10px; padding: 14px; text-align: center; margin-bottom: 20px; }
    .current-label { font-size: 13px; color: #457b9d; font-weight: bold; margin-bottom: 4px; }
    .current-amount { font-size: 26px; color: #1d3557; font-weight: 800; }
    .form-group { margin-bottom: 16px; }
    label { display: block; font-size: 14px; font-weight: bold; margin-bottom: 6px; color: #343a40; }
    input, select, textarea { width: 100%; padding: 12px; border: 1px solid #ced4da; border-radius: 8px; font-size: 15px; box-sizing: border-box; }
    input:focus, select:focus, textarea:focus { border-color: #457b9d; outline: none; box-shadow: 0 0 0 3px rgba(69,123,157,0.2); }
    .tip { font-size: 12px; color: #6c757d; margin-top: 5px; line-height: 1.4; }
    button { width: 100%; background: #457b9d; color: white; border: none; padding: 14px; border-radius: 8px; font-size: 16px; font-weight: bold; cursor: pointer; margin-top: 10px; transition: background 0.2s; }
    button:active { background: #1d3557; }
    .success { background: #d4edda; border: 1px solid #c3e6cb; color: #155724; padding: 18px; border-radius: 10px; text-align: center; font-weight: bold; line-height: 1.6; }
    .back-btn { display: inline-block; margin-top: 15px; padding: 10px 20px; background: #155724; color: white; border-radius: 6px; text-decoration: none; font-size: 14px; }
  </style>
</head>
<body>
  <div class="card">
    {% if success %}
      <div class="success">
        🎉 專戶總結餘修正成功！<br>
        最新專戶結算總額：<strong>${{ new_amount }} 元</strong><br>
        <span style="font-size: 13px; color: #28a745;">已同步更新至 Google 試算表《收支總表》</span><br>
        <a href="https://line.me/R/" class="back-btn">返回 LINE 查看</a>
      </div>
    {% else %}
      <div class="header">
        <h2>✏️ 爸爸照顧專戶總結餘修正</h2>
        <p class="subtitle">月底核對存摺/網銀真實餘額時，一鍵平帳校正</p>
      </div>

      <div class="current-box">
        <div class="current-label">💰 系統目前計算總結餘</div>
        <div class="current-amount">${{ current_amount }} 元</div>
      </div>

      <form method="POST">
        <div class="form-group">
          <label>校正後真實專戶總金額 (元) *</label>
          <input type="number" name="amount" placeholder="例如：635008 或 635000" value="{{ current_raw }}" required>
          <div class="tip">💡 請以實際銀行存摺或網銀結餘金額為準填寫。</div>
        </div>

        <div class="form-group">
          <label>操作負責人 *</label>
          <select name="author" required>
            <option value="黃芊甄">黃芊甄 (姊姊 - 財務負責)</option>
            <option value="黃蕙芬" selected>黃蕙芬 (Sandy)</option>
            <option value="黃志嘉">黃志嘉 (哥哥)</option>
            <option value="黃志龍">黃志龍 (弟弟)</option>
            <option value="塗雅芳">塗雅芳 (弟妹)</option>
          </select>
        </div>

        <div class="form-group">
          <label>校正原因 / 備註說明 (選填)</label>
          <input type="text" name="note" placeholder="例如：月底網銀存摺對帳、補登未列帳雜支">
          <div class="tip">💡 此備註將永久留存於雲端《收支總表》供家人查閱。</div>
        </div>

        <button type="submit">確認修正並更新專戶</button>
      </form>
    {% endif %}
  </div>
</body>
</html>
"""

@app.route("/adjust_balance", methods=['GET', 'POST'])
def adjust_balance():
    ws_sum = get_worksheet("收支總表")
    now = datetime.now()
    month_str = f"{now.year}年{now.month}月"
    today_str = now.strftime("%Y/%m/%d")

    # 取得當前總結餘
    current_bal = 635008
    if ws_sum:
        try:
            s_rows = ws_sum.get_all_values()
            if len(s_rows) > 1:
                last_row = s_rows[-1]
                current_bal = clean_num(last_row[4]) if len(last_row) > 4 else 635008
        except Exception:
            pass

    if request.method == 'POST':
        amt_str = request.form.get("amount", "").strip()
        author  = request.form.get("author", "黃蕙芬").strip()
        note    = request.form.get("note", "").strip()
        if not note:
            note = "月底核對存摺對帳校正"

        try:
            new_amount = int(re.sub(r'[^\d]', '', amt_str))
        except Exception:
            new_amount = current_bal

        if ws_sum:
            try:
                note_str = f"期初基準結餘 {new_amount:,} 元 ({author}於 {today_str} {note})"
                sum_rows = ws_sum.get_all_values()
                found_idx = -1
                for idx, sr in enumerate(sum_rows[1:], start=2):
                    if len(sr) > 0 and (month_str in sr[0] or f"{now.year}/{now.month:02d}" in sr[0]):
                        found_idx = idx
                        break
                if found_idx > 0:
                    ws_sum.update(f"A{found_idx}:F{found_idx}", [[
                        month_str, 0, 0, 0, new_amount, note_str
                    ]])
                else:
                    ws_sum.append_row([
                        month_str, 0, 0, 0, new_amount, note_str
                    ])
            except Exception as e:
                print(f"網頁端更新收支總表失敗: {e}")

        return render_template_string(
            ADJUST_FORM_HTML,
            success=True,
            new_amount=f"{new_amount:,}",
            current_amount=f"{new_amount:,}",
            current_raw=new_amount
        )

    return render_template_string(
        ADJUST_FORM_HTML,
        success=False,
        current_amount=f"{current_bal:,}",
        current_raw=current_bal
    )

@app.route("/health")
def health():
    gc = get_gc()
    sheet_ok = False
    if gc:
        try:
            sh = gc.open(SPREADSHEET_NAME)
            sheet_ok = bool(sh)
        except Exception:
            sheet_ok = False
    return jsonify({
        "status": "ok",
        "app": "dad_care_bot",
        "google_sheets_connected": sheet_ok,
        "gemini_key_configured": bool(os.getenv("GEMINI_API_KEY")),
        "time": datetime.now().isoformat()
    })

app.app = app

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
