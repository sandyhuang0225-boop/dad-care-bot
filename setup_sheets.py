import gspread
from datetime import datetime

SPREADSHEET_NAME = "爸爸照顧專戶收支與照護總表"
CREDENTIALS_FILE = "credentials.json"

def init_google_sheets():
    gc = gspread.service_account(CREDENTIALS_FILE)
    try:
        sh = gc.open(SPREADSHEET_NAME)
        print(f"成功開啟試算表：{SPREADSHEET_NAME}")
    except Exception as e:
        print(f"無法開啟試算表，請確認已建立並共用給 service account: {e}")
        return False

    # 1. 支出明細表
    try:
        ws_expense = sh.worksheet("支出明細")
    except:
        ws_expense = sh.add_worksheet(title="支出明細", rows=500, cols=8)
    ws_expense.update(range_name='A1:H1', values=[[
        "記錄時間", "購買日期", "支出人/代墊人", "分類", "品項名稱", "金額", "憑證照片網址", "備註"
    ]])
    print("✅ 支出明細表完成")

    # 2. 進項明細表
    try:
        ws_income = sh.worksheet("進項明細")
    except:
        ws_income = sh.add_worksheet(title="進項明細", rows=500, cols=7)
    ws_income.update(range_name='A1:G1', values=[[
        "記錄時間", "入帳日期", "登記人", "來源類別", "進項說明", "金額", "備註"
    ]])
    print("✅ 進項明細表完成")

    # 3. 回診與領藥表
    try:
        ws_clinic = sh.worksheet("回診與領藥")
    except:
        ws_clinic = sh.add_worksheet(title="回診與領藥", rows=200, cols=8)
    ws_clinic.update(range_name='A1:H1', values=[[
        "看診/領藥日期", "時段", "醫院/藥局", "科別", "主治醫師", "掛號號碼", "備註事項/領藥區間", "提醒狀態"
    ]])
    print("✅ 回診與領藥表完成")

    # 4. 目前用藥清單
    try:
        ws_med = sh.worksheet("目前用藥")
    except:
        ws_med = sh.add_worksheet(title="目前用藥", rows=50, cols=7)
    ws_med.update(range_name='A1:G1', values=[[
        "藥品名稱", "作用/用途", "用法用量", "服用時段(早/中/晚/睡前)", "飯前/飯後", "注意事項", "外觀照片"
    ]])
    print("✅ 目前用藥清單完成")

    # 5. 生活照護日誌
    try:
        ws_log = sh.worksheet("照護日誌")
    except:
        ws_log = sh.add_worksheet(title="照護日誌", rows=500, cols=7)
    ws_log.update(range_name='A1:G1', values=[[
        "記錄時間", "記錄人", "精神/心情狀況", "飲食/食慾狀況", "量測數據(血壓/血糖)", "今日活動/特殊狀況", "備註照片"
    ]])
    print("✅ 照護日誌完成")

    # 6. 收支總表（自動摘要月度收支）
    try:
        ws_summary = sh.worksheet("收支總表")
    except:
        ws_summary = sh.add_worksheet(title="收支總表", rows=100, cols=6)
    ws_summary.update(range_name='A1:F1', values=[[
        "月份", "本月進項總額", "本月支出總額", "本月收支淨結餘", "累計專戶總結餘", "備註"
    ]])
    print("✅ 收支總表完成")

    # 刪除預設的 Sheet1 (工作表1) 如果存在
    try:
        ws_default = sh.worksheet("工作表1")
        sh.del_worksheet(ws_default)
    except:
        try:
            ws_default = sh.worksheet("Sheet1")
            sh.del_worksheet(ws_default)
        except:
            pass

    print("🎉 爸爸照顧專戶所有工作表初始化完成！")
    return True

if __name__ == "__main__":
    init_google_sheets()
