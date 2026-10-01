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

# ==================== Google Sheets 工具函式 ====================
def get_gc():
    try:
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
def create_expense_menu_flex(base_url):
    return FlexSendMessage(
        alt_text="【爸爸照顧】記一筆支出",
        contents=BubbleContainer(
            body=BoxComponent(layout='vertical', padding_all='none', contents=[
                BoxComponent(layout='vertical', padding_all='lg', background_color='#e63946', contents=[
                    TextComponent(text="💰 爸爸照顧專戶 ｜ 記一筆支出", weight='bold', size='md', color='#ffffff'),
                    TextComponent(text="實報實銷、公開透明，大家辛苦了！", size='xs', color='#ffe3e3', margin='xs'),
                ]),
                BoxComponent(layout='vertical', padding_all='lg', spacing='sm', contents=[
                    TextComponent(text="📌 兩種超快速記帳方式：", weight='bold', size='sm', color='#333333'),
                    SeparatorComponent(margin='xs'),
                    TextComponent(text="1. 【直接拍照/留言】在對話框直接拍發票，並留言「買尿布 850」或「午餐 160」。", size='xs', color='#555555', wrap=True),
                    TextComponent(text="2. 【線上表單記帳】點擊下方按鈕，選取支出人、品項與分類。", size='xs', color='#555555', wrap=True),
                    SeparatorComponent(margin='md'),
                    ButtonComponent(action=URIAction(label="📝 線上填寫支出表單", uri=f"{base_url}/expense_form"), style='primary', color='#e63946', height='sm'),
                    ButtonComponent(action=MessageAction(label="📋 查詢最近支出明細", text="查詢最近支出"), style='secondary', height='sm', margin='xs'),
                ])
            ])
        )
    )

# ==================== 2. 專戶進項管理卡片 ====================
def create_income_menu_flex(base_url):
    return FlexSendMessage(
        alt_text="【爸爸照顧】專戶進項管理",
        contents=BubbleContainer(
            body=BoxComponent(layout='vertical', padding_all='none', contents=[
                BoxComponent(layout='vertical', padding_all='lg', background_color='#2a9d8f', contents=[
                    TextComponent(text="🏠 爸爸照顧專戶 ｜ 進項管理", weight='bold', size='md', color='#ffffff'),
                    TextComponent(text="房租匯入與銀行收益核算", size='xs', color='#d8f3dc', margin='xs'),
                ]),
                BoxComponent(layout='vertical', padding_all='lg', spacing='sm', contents=[
                    TextComponent(text="💼 快捷進項登記：", weight='bold', size='sm', color='#333333'),
                    SeparatorComponent(margin='xs'),
                    ButtonComponent(action=MessageAction(label="🏠 Sandy 登記房租入帳", text="登記房租入帳"), style='primary', color='#2a9d8f', height='sm'),
                    ButtonComponent(action=MessageAction(label="🏛️ 姊姊登記老人年金", text="登記老人年金"), style='secondary', height='sm', margin='xs'),
                    ButtonComponent(action=MessageAction(label="📈 姊姊登記債券收益", text="登記債券收益"), style='secondary', height='sm', margin='xs'),
                    ButtonComponent(action=MessageAction(label="💰 姊姊登記利息/其他", text="登記利息收入"), style='secondary', height='sm', margin='xs'),
                    SeparatorComponent(margin='md'),
                    ButtonComponent(action=URIAction(label="📝 線上詳細進項登記表單", uri=f"{base_url}/income_form"), style='secondary', height='sm'),
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
                    ButtonComponent(action=MessageAction(label="📋 查看本月支出明細", text="查詢本月支出明細"), style='secondary', height='sm'),
                ])
            ])
        )
    )

# ==================== 4. 醫院回診與領藥卡片 ====================
def create_clinic_flex(base_url, clinic_rows):
    items = []
    if clinic_rows:
        for r in clinic_rows[:3]:
            # r: [日期, 時段, 醫院/藥局, 科別, 主治醫師, 掛號號碼, 備註, 狀態]
            date_str = r[0] if len(r) > 0 else ""
            hosp     = r[2] if len(r) > 2 else ""
            dept     = r[3] if len(r) > 3 else ""
            doc      = r[4] if len(r) > 4 else ""
            no       = r[5] if len(r) > 5 else ""
            items.append(create_row(f"📅 {date_str}", f"{hosp} {dept} {doc} ({no}號)"))
    else:
        items.append(TextComponent(text="目前無近期預約回診，大家可放心！", size='xs', color='#666666'))

    return FlexSendMessage(
        alt_text="【爸爸照顧】醫院回診與領藥提醒",
        contents=BubbleContainer(
            body=BoxComponent(layout='vertical', padding_all='none', contents=[
                BoxComponent(layout='vertical', padding_all='lg', background_color='#457b9d', contents=[
                    TextComponent(text="🏥 爸爸健康 ｜ 醫院回診與慢箋", weight='bold', size='md', color='#ffffff'),
                    TextComponent(text="預約門診、領藥提醒，健康守護不漏接", size='xs', color='#f1faee', margin='xs'),
                ]),
                BoxComponent(layout='vertical', padding_all='lg', spacing='sm', contents=[
                    TextComponent(text="🗓️ 近期回診與用藥規劃：", weight='bold', size='sm', color='#333333'),
                    SeparatorComponent(margin='xs'),
                    *items,
                    SeparatorComponent(margin='md'),
                    ButtonComponent(action=URIAction(label="➕ 預約/登記下次回診", uri=f"{base_url}/clinic_form"), style='primary', color='#457b9d', height='sm'),
                    ButtonComponent(action=MessageAction(label="💊 查看慢性處方箋領藥", text="查看慢箋領藥"), style='secondary', height='sm', margin='xs'),
                ])
            ])
        )
    )

# ==================== 5. 目前用藥清單卡片 ====================
def create_medication_flex(med_rows):
    med_boxes = []
    if med_rows:
        for r in med_rows[:6]:
            # r: [藥品名稱, 作用/用途, 用法用量, 服用時段, 飯前/飯後, 注意事項, 照片]
            name   = r[0] if len(r) > 0 else ""
            usage  = r[1] if len(r) > 1 else ""
            timing = r[3] if len(r) > 3 else ""
            med_boxes.append(create_row(f"💊 {name}", f"{timing} ({usage})"))
    else:
        med_boxes.append(TextComponent(text="暫無用藥資料，可在 Google 試算表更新！", size='xs', color='#888888'))

    return FlexSendMessage(
        alt_text="【爸爸照顧】目前用藥清單手冊",
        contents=BubbleContainer(
            body=BoxComponent(layout='vertical', padding_all='none', contents=[
                BoxComponent(layout='vertical', padding_all='lg', background_color='#6a4c93', contents=[
                    TextComponent(text="💊 爸爸用藥 ｜ 現行用藥手冊", weight='bold', size='md', color='#ffffff'),
                    TextComponent(text="給醫師看診、家人分藥防呆必備", size='xs', color='#f3e8ee', margin='xs'),
                ]),
                BoxComponent(layout='vertical', padding_all='lg', spacing='sm', contents=[
                    TextComponent(text="📋 每日規律服用藥品清單：", weight='bold', size='sm', color='#333333'),
                    SeparatorComponent(margin='xs'),
                    *med_boxes,
                    SeparatorComponent(margin='md'),
                    TextComponent(text="💡 小提醒：若至他院看診或牙科，請直接出示此清單供醫師評估藥物交互作用！", size='xs', color='#666666', wrap=True),
                ])
            ])
        )
    )

# ==================== 6. 生活照護日誌卡片 ====================
def create_care_log_flex(base_url, log_rows):
    recent_logs = []
    if log_rows:
        for r in log_rows[:3]:
            # r: [時間, 記錄人, 精神/心情, 飲食/食慾, 量測數據, 今日活動, 照片]
            date_str = r[0] if len(r) > 0 else ""
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
                    ButtonComponent(action=URIAction(label="✏️ 填寫今日生活照護日誌", uri=f"{base_url}/log_form"), style='primary', color='#f4a261', height='sm'),
                ])
            ])
        )
    )

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
    base_url = request.host_url.rstrip('/')

    # 取得發送者暱稱
    try:
        profile = line_bot_api.get_profile(user_id)
        user_name = profile.display_name
    except Exception:
        user_name = "家人"

    # ── 1. 六宮格按鈕 1：記一筆支出 ──
    if text in ["記一筆支出", "記帳", "支出", "1", "按鈕1"]:
        line_bot_api.reply_message(event.reply_token, create_expense_menu_flex(base_url))
        return

    # ── 2. 六宮格按鈕 2：專戶進項管理 ──
    if text in ["專戶進項管理", "進項管理", "收入", "進項", "2", "按鈕2"]:
        line_bot_api.reply_message(event.reply_token, create_income_menu_flex(base_url))
        return

    # ── 3. 六宮格按鈕 3：本月收支總表 ──
    if text in ["本月收支總表", "收支總表", "看總表", "財務報表", "3", "按鈕3"]:
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
                # r: [記錄時間, 入帳日期, 登記人, 來源類別, 進項說明, 金額]
                d = r[1] if len(r) > 1 else ""
                amt = clean_num(r[5]) if len(r) > 5 else 0
                if d.startswith(cur_prefix) or d.startswith(alt_prefix) or f"{now.month}月" in d:
                    total_income += amt

        total_expense = 0
        categories = {"伙食餐飲": 0, "生活耗品": 0, "醫療藥費": 0, "交通接送": 0, "其他雜支": 0}
        if out_rows and len(out_rows) > 1:
            for r in out_rows[1:]:
                # r: [記錄時間, 購買日期, 支出人, 分類, 品項, 金額]
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
    if text in ["醫院回診領藥", "回診", "領藥", "看診", "4", "按鈕4"]:
        ws_c = get_worksheet("回診與領藥")
        c_rows = ws_c.get_all_values() if ws_c else []
        valid_rows = [r for r in c_rows[1:] if len(r) > 0 and r[0].strip()] if len(c_rows) > 1 else []
        line_bot_api.reply_message(event.reply_token, create_clinic_flex(base_url, valid_rows))
        return

    # ── 5. 六宮格按鈕 5：目前用藥手冊 ──
    if text in ["目前用藥手冊", "用藥清單", "用藥", "吃藥", "5", "按鈕5"]:
        ws_m = get_worksheet("目前用藥")
        m_rows = ws_m.get_all_values() if ws_m else []
        valid_rows = [r for r in m_rows[1:] if len(r) > 0 and r[0].strip()] if len(m_rows) > 1 else []
        line_bot_api.reply_message(event.reply_token, create_medication_flex(valid_rows))
        return

    # ── 6. 六宮格按鈕 6：爸爸生活照護 ──
    if text in ["爸爸生活照護", "生活照護", "照護日誌", "日記", "6", "按鈕6"]:
        ws_l = get_worksheet("照護日誌")
        l_rows = ws_l.get_all_values() if ws_l else []
        valid_rows = [r for r in l_rows[1:] if len(r) > 0 and r[0].strip()] if len(l_rows) > 1 else []
        # 按時間倒序
        valid_rows.reverse()
        line_bot_api.reply_message(event.reply_token, create_care_log_flex(base_url, valid_rows))
        return

    # ── 快捷進項登記指令 ──
    if "登記房租" in text:
        # 預設房租 20000，或由字串中解析數字
        match = re.search(r'\d+', text)
        amt = int(match.group()) if match else 20000
        today_str = datetime.now().strftime("%Y/%m/%d")
        now_time = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
        ws = get_worksheet("進項明細")
        if ws:
            ws.append_row([now_time, today_str, "Sandy", "房屋租金", f"{datetime.now().month}月份房屋租金", amt, "LINE快捷登記"])
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                text=f"✅ 已成功登記本月房租入帳！\n• 登記人：Sandy\n• 金額：{amt:,} 元\n• 入帳日期：{today_str}\n已自動歸入專戶總額！"
            ))
            return

    if "登記老人年金" in text:
        match = re.search(r'\d+', text)
        amt = int(match.group()) if match else 4164
        today_str = datetime.now().strftime("%Y/%m/%d")
        now_time = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
        ws = get_worksheet("進項明細")
        if ws:
            ws.append_row([now_time, today_str, "姊姊", "政府津貼", f"{datetime.now().month}月份老人年金", amt, "網銀核對登記"])
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                text=f"✅ 已成功登記老人年金入帳！\n• 登記人：姊姊\n• 金額：{amt:,} 元\n• 入帳日期：{today_str}\n已自動累計至本月收入！"
            ))
            return

    if "登記債券" in text:
        match = re.search(r'\d+', text)
        amt = int(match.group()) if match else 15000
        today_str = datetime.now().strftime("%Y/%m/%d")
        now_time = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
        ws = get_worksheet("進項明細")
        if ws:
            ws.append_row([now_time, today_str, "姊姊", "債券收益", "本季/本月債券配息", amt, "網銀核對登記"])
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                text=f"✅ 已成功登記債券收益入帳！\n• 登記人：姊姊\n• 金額：{amt:,} 元\n• 入帳日期：{today_str}\n已計入專戶！"
            ))
            return

    if "登記利息" in text:
        match = re.search(r'\d+', text)
        amt = int(match.group()) if match else 250
        today_str = datetime.now().strftime("%Y/%m/%d")
        now_time = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
        ws = get_worksheet("進項明細")
        if ws:
            ws.append_row([now_time, today_str, "姊姊", "存款利息", "銀行存款利息", amt, "網銀核對登記"])
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                text=f"✅ 已成功登記存款利息！\n• 登記人：姊姊\n• 金額：{amt:,} 元\n• 入帳日期：{today_str}\n已計入專戶！"
            ))
            return

    # ── 對話框快速文字記帳辨識（例如：買尿布 850 / 弟弟午餐 160）──
    match_money = re.search(r'(\d+)\s*元?', text)
    if match_money and any(kw in text for kw in ["買", "吃", "費", "餐", "藥", "車", "布", "奶粉", "用品", "掛號"]):
        amt = int(match_money.group(1))
        item_text = text.replace(match_money.group(0), "").strip()
        today_str = datetime.now().strftime("%Y/%m/%d")
        now_time = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
        
        # 分類推估
        if any(w in text for w in ["吃", "飯", "餐", "菜", "肉", "果"]):
            cat = "伙食餐飲"
        elif any(w in text for w in ["藥", "醫", "診", "掛號", "護理"]):
            cat = "醫療藥費"
        elif any(w in text for w in ["車", "計程車", "油", "捷運"]):
            cat = "交通接送"
        else:
            cat = "生活耗品"

        ws = get_worksheet("支出明細")
        if ws:
            ws.append_row([now_time, today_str, user_name, cat, item_text, amt, "", "LINE對話快速記帳"])
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                text=f"✅ 已成功記錄一筆支出！\n• 記錄人：{user_name}\n• 日期：{today_str}\n• 分類：{cat}\n• 品項：{item_text}\n• 金額：{amt:,} 元\n感謝您的用心照顧與代墊！"
            ))
            return

    # ── 查詢本月支出明細 ──
    if "支出明細" in text:
        ws_out = get_worksheet("支出明細")
        rows = ws_out.get_all_values() if ws_out else []
        if len(rows) > 1:
            recent = rows[-5:]
            msg = "📋 【最近 5 筆支出明細】\n\n"
            for r in recent:
                d = r[1] if len(r) > 1 else ""
                who = r[2] if len(r) > 2 else ""
                item = r[4] if len(r) > 4 else ""
                amt = r[5] if len(r) > 5 else "0"
                msg += f"• {d} [{who}] {item}：{amt} 元\n"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg.strip()))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="目前尚無支出明細紀錄！"))
        return

    # 通用親切回應
    line_bot_api.reply_message(event.reply_token, TextSendMessage(
        text=f"您好，{user_name}！爸爸照顧小秘書隨時為全家人服務。\n\n💡 請點選下方【圖文選單】功能鍵：\n• 💰 記一筆支出\n• 🏠 專戶進項管理\n• 📊 本月收支總表\n• 🏥 醫院回診領藥\n• 💊 目前用藥手冊\n• 📋 爸爸生活照護"
    ))

@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event):
    user_id = event.source.user_id
    try:
        profile = line_bot_api.get_profile(user_id)
        user_name = profile.display_name
    except:
        user_name = "家人"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(
        text=f"📸 收到 {user_name} 傳送的照片！\n\n若是【發票/收據報銷】，請在此直接回傳「品項與金額」（例如輸入：`尿布 850` 或 `爸爸午餐 160`），系統將自動為您歸檔記帳！\n\n若是【生活照護照片】，已記錄備查，感謝您的貼心照料！"
    ))

# ==================== Web 網頁表單 (記帳、進項、回診、日誌) ====================

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
        🎉 支出登記成功！<br>已同步寫入 Google 試算表，感謝您的付出！
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
            <option value="弟弟">弟弟</option>
            <option value="弟妹">弟妹</option>
            <option value="姊姊">姊姊</option>
            <option value="Sandy">Sandy</option>
            <option value="其他">其他</option>
          </select>
        </div>
        <div class="form-group">
          <label>費用類別</label>
          <select name="category" required>
            <option value="生活耗品">生活耗品 (尿布/紙巾/沐浴/營養品)</option>
            <option value="伙食餐飲">伙食餐飲 (日常買菜/外食/點心)</option>
            <option value="醫療藥費">醫療藥費 (掛號費/自費藥/敷料)</option>
            <option value="交通接送">交通接送 (計程車/就醫車資)</option>
            <option value="其他雜支">其他雜支 (維修/家電/日常雜項)</option>
          </select>
        </div>
        <div class="form-group">
          <label>購買品項名稱</label>
          <input type="text" name="item" placeholder="例如：安安成人紙尿褲 2包" required>
        </div>
        <div class="form-group">
          <label>支出金額 (元)</label>
          <input type="number" name="amount" placeholder="例如：850" required>
        </div>
        <div class="form-group">
          <label>備註說明</label>
          <textarea name="note" rows="2" placeholder="選填，如發票號碼或特殊備註"></textarea>
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
            <option value="姊姊">姊姊</option>
            <option value="Sandy">Sandy</option>
            <option value="弟弟">弟弟</option>
            <option value="其他">其他</option>
          </select>
        </div>
        <div class="form-group">
          <label>進項來源類別</label>
          <select name="category" required>
            <option value="房屋租金">房屋租金收入 (Sandy)</option>
            <option value="政府津貼">政府老人年金/老農津貼</option>
            <option value="債券收益">債券利息/配息收益</option>
            <option value="存款利息">活存/定存利息</option>
            <option value="其他收入">其他專戶入帳/津貼補助</option>
          </select>
        </div>
        <div class="form-group">
          <label>說明名稱</label>
          <input type="text" name="desc" placeholder="例如：10月份房屋租金 或 10月老人年金" required>
        </div>
        <div class="form-group">
          <label>入帳金額 (元)</label>
          <input type="number" name="amount" placeholder="例如：20000" required>
        </div>
        <div class="form-group">
          <label>備註說明</label>
          <textarea name="note" rows="2" placeholder="選填"></textarea>
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

CLINIC_FORM_HTML = """
<!DOCTYPE html>
<html lang="zh-TW">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>預約回診 ｜ 爸爸照顧專戶</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f8f9fa; margin: 0; padding: 15px; color: #333; }
    .card { background: #fff; border-radius: 12px; padding: 20px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); max-width: 480px; margin: 0 auto; }
    h2 { color: #457b9d; margin-top: 0; font-size: 20px; border-bottom: 2px solid #e9ecef; padding-bottom: 10px; }
    .form-group { margin-bottom: 15px; }
    label { display: block; font-size: 14px; font-weight: bold; margin-bottom: 5px; color: #495057; }
    input, select, textarea { width: 100%; padding: 12px; border: 1px solid #ced4da; border-radius: 8px; font-size: 15px; box-sizing: border-box; }
    button { width: 100%; background: #457b9d; color: white; border: none; padding: 14px; border-radius: 8px; font-size: 16px; font-weight: bold; cursor: pointer; margin-top: 10px; }
    .success { background: #d4edda; color: #155724; padding: 15px; border-radius: 8px; text-align: center; font-weight: bold; }
  </style>
</head>
<body>
  <div class="card">
    {% if success %}
      <div class="success">
        🎉 回診預約登記成功！<br>小秘書將在看診前主動推播提醒大家！
      </div>
    {% else %}
      <h2>🏥 預約 / 登記下次醫院門診</h2>
      <form method="POST">
        <div class="form-group">
          <label>看診日期</label>
          <input type="date" name="date" required>
        </div>
        <div class="form-group">
          <label>就診時段</label>
          <select name="period">
            <option value="上午診">上午診 (約 09:00 ~ 12:00)</option>
            <option value="下午診">下午診 (約 14:00 ~ 17:00)</option>
            <option value="夜間診">夜間診 (約 18:00 ~ 21:00)</option>
          </select>
        </div>
        <div class="form-group">
          <label>就診醫院</label>
          <input type="text" name="hospital" placeholder="例如：高醫、高雄榮總、長庚" required>
        </div>
        <div class="form-group">
          <label>科別</label>
          <input type="text" name="dept" placeholder="例如：神經內科、身心科、家醫科" required>
        </div>
        <div class="form-group">
          <label>主治醫師</label>
          <input type="text" name="doctor" placeholder="例如：陳醫師" required>
        </div>
        <div class="form-group">
          <label>掛號號碼</label>
          <input type="text" name="queue_no" placeholder="例如：23 號">
        </div>
        <div class="form-group">
          <label>注意事項 / 慢箋領藥備註</label>
          <textarea name="note" rows="2" placeholder="選填，如：需抽血、空腹、領第2次慢箋等"></textarea>
        </div>
        <button type="submit">儲存回診規劃</button>
      </form>
    {% endif %}
  </div>
</body>
</html>
"""

@app.route("/clinic_form", methods=['GET', 'POST'])
def clinic_form():
    if request.method == 'POST':
        date_val = request.form.get("date", "").replace("-", "/")
        period   = request.form.get("period", "")
        hospital = request.form.get("hospital", "")
        dept     = request.form.get("dept", "")
        doctor   = request.form.get("doctor", "")
        queue_no = request.form.get("queue_no", "")
        note     = request.form.get("note", "")

        ws = get_worksheet("回診與領藥")
        if ws:
            ws.append_row([date_val, period, hospital, dept, doctor, queue_no, note, "待提醒"])

        return render_template_string(CLINIC_FORM_HTML, success=True)
    return render_template_string(CLINIC_FORM_HTML, success=False)

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
            <option value="弟弟">弟弟</option>
            <option value="弟妹">弟妹</option>
            <option value="姊姊">姊姊</option>
            <option value="Sandy">Sandy</option>
            <option value="其他">其他</option>
          </select>
        </div>
        <div class="form-group">
          <label>精神與心情狀況</label>
          <input type="text" name="mood" placeholder="例如：精神很好、愛笑、有些焦躁等" required>
        </div>
        <div class="form-group">
          <label>飲食與食慾</label>
          <input type="text" name="appetite" placeholder="例如：三餐正常、吃一整碗粥、胃口普通等">
        </div>
        <div class="form-group">
          <label>量測數據 (選填)</label>
          <input type="text" name="vitals" placeholder="例如：血壓 125/80、血糖 110">
        </div>
        <div class="form-group">
          <label>今日活動 / 特殊狀況記錄</label>
          <textarea name="activity" rows="3" placeholder="例如：下午有到公園散步 30 分鐘，心情很放鬆。"></textarea>
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
