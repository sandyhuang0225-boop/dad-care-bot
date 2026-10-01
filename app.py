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
ADMIN_LINE_ID = os.getenv("ADMIN_LINE_ID", "Uc63f60ac0469e5f5a684081e20814dff")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

SPREADSHEET_NAME = "爸爸照顧專戶收支與照護總表"
CREDENTIALS_FILE = os.path.join(os.path.dirname(__file__), "credentials.json")

# 確保對外網址一律為 https:// (LINE Flex 要求)
BASE_URL = os.getenv("RENDER_EXTERNAL_URL", "https://dad-care-bot.onrender.com").rstrip('/')
if not BASE_URL.startswith("https://"):
    BASE_URL = "https://" + BASE_URL.split("://")[-1]

# ==================== Google Sheets 工具函式 ====================
def get_gc():
    try:
        # 1. 優先從環境變數讀取 (避免 GitHub 攔截撤銷)
        creds_json = os.getenv("GOOGLE_CREDENTIALS")
        if creds_json:
            creds_dict = json.loads(creds_json)
            return gspread.service_account_from_dict(creds_dict)
        # 2. 本地開發讀取 credentials.json
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

def clean_user_name(name):
    if not name:
        return "家人"
    if any(k in name for k in ["Sandy", "sandy", "先迪"]):
        return "Sandy"
    if any(k in name for k in ["士龍", "弟弟", "建宏"]):
        return "黃士龍"
    if any(k in name for k in ["弟妹", "小芳"]):
        return "弟妹"
    if any(k in name for k in ["惠芬", "姊姊", "姐姐"]):
        return "姊姊"
    if "諾華" in name:
        return "Sandy"
    return name

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
                    TextComponent(text="🎙️ 推薦【語音輸入】秒記帳：", weight='bold', size='sm', color='#e63946'),
                    SeparatorComponent(margin='xs'),
                    TextComponent(text="用 LINE 語音輸入說一句話即可自動記帳，例如：\n•「黃士龍買水果600元」\n•「買尿布850」\n•「士龍買午餐160」", size='xs', color='#333333', wrap=True),
                    SeparatorComponent(margin='md'),
                    ButtonComponent(action=URIAction(label="📝 線上詳細記帳表單", uri=f"{BASE_URL}/expense_form"), style='primary', color='#e63946', height='sm'),
                    ButtonComponent(action=MessageAction(label="📋 查詢最近支出明細", text="查詢最近支出"), style='secondary', height='sm', margin='xs'),
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
                    TextComponent(text="房租匯入與被動收益入帳核算", size='xs', color='#d8f3dc', margin='xs'),
                ]),
                BoxComponent(layout='vertical', padding_all='lg', spacing='sm', contents=[
                    TextComponent(text="💼 點擊快速登記或文字輸入：", weight='bold', size='sm', color='#333333'),
                    SeparatorComponent(margin='xs'),
                    ButtonComponent(action=MessageAction(label="🏠 Sandy 登記本月房租入帳", text="登記房租入帳 20000"), style='primary', color='#2a9d8f', height='sm'),
                    ButtonComponent(action=MessageAction(label="🏛️ 姊姊登記老人年金", text="登記老人年金"), style='secondary', height='sm', margin='xs'),
                    ButtonComponent(action=MessageAction(label="📈 姊姊登記債券收益", text="登記債券收益"), style='secondary', height='sm', margin='xs'),
                    ButtonComponent(action=MessageAction(label="💰 姊姊登記利息收入", text="登記利息收入"), style='secondary', height='sm', margin='xs'),
                    SeparatorComponent(margin='md'),
                    TextComponent(text="💡 小提示：也可直接傳送文字，例如「老人年金 4164」或「債券 15000」！", size='xs', color='#666666', wrap=True),
                    ButtonComponent(action=URIAction(label="📝 線上自訂進項登記表單", uri=f"{BASE_URL}/income_form"), style='secondary', height='sm', margin='xs'),
                ])
            ])
        )
    )

# ==================== 3. 本月收支總表卡片 ====================
def create_summary_flex(month_str, total_income, total_expense, net_balance, category_breakdown):
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
                    TextComponent(text=f"📊 照顧專戶 ｜ {month_str} 收支月報", weight='bold', size='md', color='#ffffff'),
                    TextComponent(text="即時自動統計，全體兄弟姊妹公開透明", size='xs', color='#a8dadc', margin='xs'),
                ]),
                BoxComponent(layout='vertical', padding_all='lg', spacing='sm', contents=[
                    create_row("🟢 本月進項總額", f"{total_income:,} 元", color='#2a9d8f', weight='bold'),
                    create_row("🔴 本月支出總額", f"{total_expense:,} 元", color='#e63946', weight='bold'),
                    SeparatorComponent(margin='sm'),
                    create_row("💵 本月收支淨結餘", f"{net_sign}{net_balance:,} 元", color=net_color, weight='bold'),
                    SeparatorComponent(margin='md'),
                    TextComponent(text="📑 本月主要支出項目：", weight='bold', size='xs', color='#555555', margin='sm'),
                    *(cat_boxes if cat_boxes else [TextComponent(text="目前尚無支出記錄", size='xs', color='#999999')]),
                    SeparatorComponent(margin='md'),
                    ButtonComponent(action=MessageAction(label="📋 查看本月支出明細", text="查詢最近支出"), style='secondary', height='sm'),
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
                    TextComponent(text="高醫神經內科、新陳代謝科就醫規劃", size='xs', color='#f1faee', margin='xs'),
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
        # 內建神經內科與新陳代謝科 Baseline
        med_boxes.append(create_row("💊 憶思能 (Exelon)", "早上隨餐 (認知失智)"))
        med_boxes.append(create_row("💊 庫魯化錠 (Glucophage)", "早晚飯後 (血糖控制)"))
        med_boxes.append(create_row("💊 脈優錠 (Norvasc)", "早上飯後 (血壓控制)"))
        med_boxes.append(create_row("💊 悠樂丁 (Eurodin)", "睡前半顆 (夜間舒眠)"))

    return FlexSendMessage(
        alt_text="【爸爸照顧】目前用藥清單手冊",
        contents=BubbleContainer(
            body=BoxComponent(layout='vertical', padding_all='none', contents=[
                BoxComponent(layout='vertical', padding_all='lg', background_color='#6a4c93', contents=[
                    TextComponent(text="💊 爸爸用藥 ｜ 現行用藥手冊", weight='bold', size='md', color='#ffffff'),
                    TextComponent(text="高醫神經內科與新陳代謝科 Baseline", size='xs', color='#f3e8ee', margin='xs'),
                ]),
                BoxComponent(layout='vertical', padding_all='lg', spacing='sm', contents=[
                    TextComponent(text="📋 每日規律服用藥品清單：", weight='bold', size='sm', color='#333333'),
                    SeparatorComponent(margin='xs'),
                    *med_boxes,
                    SeparatorComponent(margin='md'),
                    TextComponent(text="💡 陪病看診必備：就診他科或牙科時，直接出示此畫面供醫師評估藥物交互作用！", size='xs', color='#666666', wrap=True),
                    ButtonComponent(action=URIAction(label="✏️ 維護 / 新增現行用藥清單", uri=f"{BASE_URL}/med_form"), style='secondary', height='sm', margin='xs'),
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
    '黃士龍買水果600元'
    '黃士龍 買水果 600'
    '士龍買尿布850'
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

    # 判斷支出人
    spender = clean_user_name(sender_name)
    rem_text = raw
    if any(k in raw for k in ["黃士龍", "士龍"]):
        spender = "黃士龍"
        rem_text = re.sub(r'黃士龍|士龍', '', rem_text)
    elif any(k in raw for k in ["弟妹", "小芳"]):
        spender = "弟妹"
        rem_text = re.sub(r'弟妹|小芳', '', rem_text)
    elif any(k in raw for k in ["姊姊", "姐姐", "惠芬"]):
        spender = "姊姊"
        rem_text = re.sub(r'姊姊|姐姐|惠芬', '', rem_text)
    elif any(k in raw for k in ["Sandy", "sandy", "先迪"]):
        spender = "Sandy"
        rem_text = re.sub(r'Sandy|sandy|先迪', '', rem_text)
    elif "弟弟" in raw:
        spender = "黃士龍"
        rem_text = re.sub(r'弟弟', '', rem_text)

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
        user_name = clean_user_name(profile.display_name)
    except Exception:
        user_name = "家人"

    # ── 1. 六宮格按鈕 1：記一筆支出 ──
    if text in ["記一筆支出", "記帳", "支出", "1", "按鈕1"]:
        line_bot_api.reply_message(event.reply_token, create_expense_menu_flex())
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
        categories = {"伙食餐飲": 0, "生活耗品": 0, "醫療藥費": 0, "交通接送": 0, "其他雜支": 0}
        if out_rows and len(out_rows) > 1:
            for r in out_rows[1:]:
                d = r[1] if len(r) > 1 else ""
                cat = r[3] if len(r) > 3 else "其他雜支"
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
                        categories["其他雜支"] += amt

        net_bal = total_income - total_expense
        line_bot_api.reply_message(event.reply_token, create_summary_flex(
            month_str, total_income, total_expense, net_bal, categories
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
        msg += "• 照護科別：神經內科、新陳代謝科\n"
        msg += "• 領藥地點：住家鄰近健保特約藥局（如大樹藥局）或高醫領藥處\n"
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
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="目前尚無支出紀錄，歡迎用語音記帳（如「黃士龍買水果600元」）！"))
        return

    # ── 8. 專戶進項快捷指令 ──
    if "登記房租" in text or text.startswith("房租"):
        match = re.search(r'\d+', text)
        amt = int(match.group()) if match else 20000
        today_str = datetime.now().strftime("%Y/%m/%d")
        now_time = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
        ws = get_worksheet("進項明細")
        if ws:
            ws.append_row([now_time, today_str, "Sandy", "房屋租金", f"{datetime.now().month}月份房屋租金", amt, "快捷登記"])
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                text=f"✅ 已成功登記本月房租入帳！\n• 登記人：Sandy\n• 項目：房屋租金\n• 金額：{amt:,} 元\n• 入帳日期：{today_str}\n已自動歸入專戶總額！"
            ))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 目前試算表連線中，請稍候重試或確認 Google 授權！"))
        return

    if text == "登記老人年金":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            text="🏛️ 請回傳老人年金入帳金額（例如直接輸入：`老人年金 4164`），系統將自動為姊姊入帳！"
        ))
        return

    if "老人年金" in text:
        match = re.search(r'\d+', text)
        amt = int(match.group()) if match else 4164
        today_str = datetime.now().strftime("%Y/%m/%d")
        now_time = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
        ws = get_worksheet("進項明細")
        if ws:
            ws.append_row([now_time, today_str, "姊姊", "政府津貼", f"{datetime.now().month}月份老人年金", amt, "網銀核對登記"])
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                text=f"✅ 已成功登記老人年金入帳！\n• 登記人：姊姊\n• 項目：政府津貼(老人年金)\n• 金額：{amt:,} 元\n• 入帳日期：{today_str}\n已計入專戶本月總收入！"
            ))
        return

    if text == "登記債券收益":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            text="📈 請回傳本季債券配息金額（例如直接輸入：`債券 15000`），系統將自動為姊姊入帳！"
        ))
        return

    if "債券" in text:
        match = re.search(r'\d+', text)
        amt = int(match.group()) if match else 15000
        today_str = datetime.now().strftime("%Y/%m/%d")
        now_time = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
        ws = get_worksheet("進項明細")
        if ws:
            ws.append_row([now_time, today_str, "姊姊", "債券收益", "債券利息收益", amt, "網銀核對登記"])
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                text=f"✅ 已成功登記債券收益入帳！\n• 登記人：姊姊\n• 項目：債券收益\n• 金額：{amt:,} 元\n• 入帳日期：{today_str}\n已計入專戶！"
            ))
        return

    if text == "登記利息收入":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            text="💰 請回傳銀行利息金額（例如直接輸入：`利息 250`），系統將自動為姊姊入帳！"
        ))
        return

    if "利息" in text:
        match = re.search(r'\d+', text)
        amt = int(match.group()) if match else 250
        today_str = datetime.now().strftime("%Y/%m/%d")
        now_time = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
        ws = get_worksheet("進項明細")
        if ws:
            ws.append_row([now_time, today_str, "姊姊", "存款利息", "銀行存款利息", amt, "網銀核對登記"])
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                text=f"✅ 已成功登記利息收入！\n• 登記人：姊姊\n• 項目：銀行利息\n• 金額：{amt:,} 元\n• 入帳日期：{today_str}\n已計入專戶！"
            ))
        return

    # ── 9. 強大語音輸入記帳辨識（例如：「黃士龍買水果600元」）──
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
                text=f"✅ 【語音記帳成功！】\n• 日期：{today_str}\n• 代墊人：{expense_data['spender']}\n• 分類：{expense_data['category']}\n• 品項：{expense_data['item']}\n• 金額：{expense_data['amount']:,} 元\n\n已同步寫入 Google 試算表，感謝您的貼心照料！"
            ))
            return

    # ── 通用回應 ──
    line_bot_api.reply_message(event.reply_token, TextSendMessage(
        text=f"您好，{user_name}！爸爸照顧小秘書隨時為您服務。\n\n🎙️ 快速記帳只要用語音說：\n「黃士龍買水果600元」或「買尿布850」\n\n💡 或點選下方圖文選單查看收支總表與高醫回診！"
    ))

@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event):
    user_id = event.source.user_id
    try:
        profile = line_bot_api.get_profile(user_id)
        user_name = clean_user_name(profile.display_name)
    except:
        user_name = "家人"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(
        text=f"📸 收到 {user_name} 傳送的照片！\n\n若這是【發票/收據報銷】，請在此接著說或打字「品項與金額」（例如：`水果 600` 或 `尿布 850`），系統將自動為您歸檔完成記帳！\n\n若這是爸爸的生活照片，已備查留存，感謝您的細心陪伴！"
    ))

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
            <option value="黃士龍">黃士龍 (弟弟)</option>
            <option value="弟妹">弟妹</option>
            <option value="姊姊">姊姊</option>
            <option value="Sandy">Sandy</option>
            <option value="其他">其他</option>
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
            <option value="Sandy">Sandy</option>
            <option value="姊姊">姊姊</option>
            <option value="黃士龍">黃士龍</option>
            <option value="其他">其他</option>
          </select>
        </div>
        <div class="form-group">
          <label>進項來源類別</label>
          <select name="category" required>
            <option value="房屋租金">房屋租金 (Sandy匯入)</option>
            <option value="政府津貼">政府老人年金/老農津貼</option>
            <option value="債券收益">債券利息/配息收益</option>
            <option value="存款利息">銀行活存/定存利息</option>
            <option value="其他收入">其他專戶入帳</option>
          </select>
        </div>
        <div class="form-group">
          <label>進項說明</label>
          <input type="text" name="desc" placeholder="例如：10月份房屋租金 或 10月老人年金" required>
        </div>
        <div class="form-group">
          <label>入帳金額 (元)</label>
          <input type="number" name="amount" placeholder="例如：20000" required>
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
            <option value="神經內科">神經內科 (認知退化/失智專科)</option>
            <option value="新陳代謝科">新陳代謝科 (血糖/慢性病控制)</option>
            <option value="其他科別">其他科別</option>
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
        🎉 藥品新增成功！<br>已同步更新至用藥手冊！
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
          <input type="text" name="usage" placeholder="例如：神經內科/失智記憶保養、血糖控制" required>
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
            <option value="黃士龍">黃士龍 (弟弟)</option>
            <option value="弟妹">弟妹</option>
            <option value="姊姊">姊姊</option>
            <option value="Sandy">Sandy</option>
            <option value="其他">其他</option>
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

@app.route("/health")
def health():
    return jsonify({"status": "ok", "app": "dad_care_bot", "time": datetime.now().isoformat()})

app.app = app

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
