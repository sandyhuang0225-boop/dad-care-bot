import gspread

def seed_medications():
    gc = gspread.service_account("credentials.json")
    sh = gc.open("爸爸照顧專戶收支與照護總表")
    ws = sh.worksheet("目前用藥")
    data = [
        ["憶思能 (Exelon)", "神經內科 / 腦部記憶與認知退化保養", "4.5mg 每日早晨1顆", "早上", "隨餐或飯後", "隨餐服用，若有胃部不適可與早餐同服", ""],
        ["庫魯化錠 (Glucophage)", "新陳代謝科 / 血糖穩定控制", "500mg 每日早晚各1顆", "早上、晚上", "飯後", "規則服用，預防低血糖，隨身備小糖果", ""],
        ["脈優錠 (Norvasc)", "新陳代謝科 / 血壓平穩控制", "5mg 每日早晨1顆", "早上", "飯後", "每日早上量測血壓並記錄", ""],
        ["悠樂丁 (Eurodin)", "神經/身心科 / 改善夜間睡眠品質", "2mg 睡前半顆", "睡前", "睡前30分鐘", "服藥後請立即就寢，預防夜間下床跌倒", ""]
    ]
    ws.update(range_name='A2:G5', values=data)
    print("[SUCCESS] Medication baseline seeded successfully!")

if __name__ == "__main__":
    seed_medications()
