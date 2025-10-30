@app.get("/debug/sheets/write")
def debug_sheets_write():
    import os, json, base64, gspread
    try:
        sa_json_b64 = os.getenv("SERVICE_ACCOUNT_JSON_B64")
        sheet_id    = os.getenv("SHEET_ID")
        ws_name     = os.getenv("SHEET_TAB", "bindings")  # 你的程式用哪個工作表就填哪個

        creds = json.loads(base64.b64decode(sa_json_b64))
        gc = gspread.service_account_from_dict(creds)
        sh = gc.open_by_key(sheet_id)

        try:
            ws = sh.worksheet(ws_name)
        except gspread.WorksheetNotFound:
            # 先試著建立
            ws = sh.add_worksheet(title=ws_name, rows=100, cols=10)
            ws.append_row(["timestamp","userId","name","status"])

        import datetime
        now = datetime.datetime.now().isoformat(timespec="seconds")
        ws.append_row([now, "TEST_USER", "測試寫入", "OK"])

        return {"ok": True, "sheet": sheet_id, "worksheet": ws_name}, 200
    except Exception as e:
        import traceback
        return {"ok": False, "error": str(e), "trace": traceback.format_exc()}, 500
