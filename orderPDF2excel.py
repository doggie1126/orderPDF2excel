import streamlit as st
import pandas as pd
import numpy as np
from pdf2image import convert_from_bytes
from paddleocr import PaddleOCR
import re

# 頁面標題
st.set_page_config(page_title="採購單PDF轉Excel")
st.title("採購單自動處理系統")
st.write("請上傳 PDF 採購單，系統將自動提取工單號碼與料號。")

# 初始化 OCR
@st.cache_resource
def load_ocr():
    return PaddleOCR(use_angle_cls=True, lang='ch')

ocr = load_ocr()

# 上傳檔案
uploaded = st.file_uploader("選擇 PDF 檔案", type=["pdf"])



def process_page(img_np, header_y, anchors, procurement_no, factory_site):
    data_blocks = []
    data_start_y = header_y + 85

    result = ocr.ocr(img_np)
    if not result or not result[0]: return []

    # 欄位映射與容錯字典
    column_mapping = {
        "料號": "料號", "料号": "料號",
        "單價": "單價", "罩價": "單價",
        "總金額": "總金額", "總金额": "總金額",
        "交貨日期": "交貨日期",
        "工單": "工單", "工早": "工單", "工單號碼": "工單"
    }

    # 提取文字區塊
    for res in result[0]:
        box = res[0]
        text = res[1][0]
        center_x = (box[0][0] + box[1][0]) / 2
        center_y = (box[0][1] + box[2][1]) / 2

        # 只在「靠近表頭」的區域內做 Mapping，不要整張紙都掃
        if center_y < (header_y + 50):
            for typo, col in column_mapping.items():
                if typo == text.strip():
                    # 【核心修正】：只有當該欄位目前還沒有被定位，或者新抓到的位置更精準時才更新
                    # 我們這裡限制：如果已經有 anchors 了，就不再隨意覆蓋，避免抓到雜訊
                    if col not in anchors:
                        anchors[col] = center_x
                        print(f"✅ 成功定位 [{col}] 於 X={center_x:.1f}")

        if center_y > data_start_y:
            data_blocks.append({"text": text, "cx": center_x, "cy": center_y, "column": ""})


    # 【保險推論機制】：如果料號沒抓到，用其他已抓到的錨點推算
    ideal_template_x = {"品名": 400.0, "單價": 1430.0, "總金額": 1718.0, "交貨日期": 1914.0, "工單": 1876.0}

    for col in ideal_template_x:
        if col not in anchors:
            # 找距離最近且已存在的錨點
            found_anchors = [a for a in anchors if a in ideal_template_x]
            if found_anchors:
                # 找最靠近目標缺失欄位的那個鄰居
                neighbor = min(found_anchors, key=lambda a: abs(ideal_template_x[a] - ideal_template_x[col]))
                # 推算位置：鄰居實際 X + (目標理想 X - 鄰居理想 X)
                anchors[col] = anchors[neighbor] + (ideal_template_x[col] - ideal_template_x[neighbor])
                print(f"⚠️ 啟動安全機制：從 {neighbor} 推論出 {col} 位置為 {anchors[col]:.1f}")

    # 分配欄位
    for block in data_blocks:
        closest_col = min(anchors.keys(), key=lambda col: abs(anchors[col] - block['cx']))
        if abs(anchors[closest_col] - block['cx']) <= 180:
            block['column'] = closest_col

    # 分組行 (Y軸容忍度 20)
    data_blocks.sort(key=lambda x: x['cy'])
    lines = []
    current_line = []
    current_line_y = None
    for block in data_blocks:
        if current_line_y is None or abs(block['cy'] - current_line_y) < 20:
            current_line.append(block)
            current_line_y = block['cy']
        else:
            lines.append(current_line)
            current_line = [block]
            current_line_y = block['cy']
    lines.append(current_line)

    # 萃取資料
    final_data = []
    current_item = {"年": "", "月": "", "日": "", "工單": "", "料號": ""}
    for line in lines:
        line_text = " ".join([b['text'] for b in line])
        has_date = re.search(r'(\d{4})/(\d{1,2})/(\d{1,2})', line_text)
        has_seq = any(b['column'] == '料號' and re.match(r'^\d{4,5}$', b['text'].strip()) for b in line)

        if has_date or has_seq:
            if current_item["年"] or current_item["料號"]:
                final_data.append(current_item.copy())
                current_item = {"年": "", "月": "", "日": "", "工單": "", "料號": ""}
        if has_date:
            current_item["年"], current_item["月"], current_item["日"] = has_date.groups()
        wo_match = re.search(r'\d{8,}', line_text)
        if wo_match:
            current_item["工單"] = wo_match.group(0)
        for b in line:
            if b['column'] == '品名':
                text_clean = b['text'].strip()
                if not re.match(r'^\d{4,5}$', text_clean) and "備" not in text_clean:
                    if not current_item["料號"]: current_item["料號"] = text_clean
    if current_item["年"] or current_item["料號"] or current_item["工單"]:
        final_data.append(current_item)
    return final_data

# 3. 處理第一頁以取得全域資訊
pages = convert_from_path(filename, dpi=300)
first_img = np.array(pages[0])
first_result = ocr.ocr(first_img)[0]

# 定位錨點與採購單號
anchors = {}
header_y = 0
procurement_no = ""
factory_site = "未知"

for res in first_result:
    box = res[0]
    text = res[1][0]
    center_x = (box[0][0] + box[1][0]) / 2
    # 抓單號
    match = re.search(r'(\d{8,}-[CH])', text)
    if match and box[0][1] < 300: # Y < 300 的區域
        procurement_no = match.group(1)
        factory_site = "嘉義" if procurement_no.endswith('-C') else "新竹"
    # 抓表頭
    for typo, col in {"品名":"品名","單價":"單價","總金額":"總金額","交貨日期":"交貨日期","工單":"工單"}.items():
        if typo in text:
            anchors[col] = center_x
            header_y = max(header_y, box[0][1])



if uploaded is not None:
    if st.button("開始辨識"):
        with st.spinner('處理中，請稍候...'):
            # 讀取檔案
            bytes_data = uploaded.getvalue()
            pages = convert_from_bytes(bytes_data, dpi=300)
            
            # 這裡放入你原先的 process_page 邏輯
            # 4. 主迴圈：處理所有頁面
            total_all_data = []
            for i, page in enumerate(pages):
                print(f"正在處理第 {i+1} 頁...")
                page_data = process_page(np.array(page), header_y, anchors, procurement_no, factory_site)
                for row in page_data:
                    row.update({"廠區": factory_site, "採購單號": procurement_no})
                    total_all_data.append(row)

            # 5. 輸出
            df_final = pd.DataFrame(total_all_data, columns=["年", "月", "日", "廠區", "採購單號", "工單", "料號"])
            df_final.to_excel("Final_Complete_Report.xlsx", index=False)
            files.download("Final_Complete_Report.xlsx")
            print("✅ 處理完成！")
            
            # 執行處理...
            st.success("辨識完成！")
            
            # 提供 Excel 下載
            df_final = pd.DataFrame(total_all_data)
            csv = df_final.to_csv(index=False).encode('utf-8-sig')
            st.download_button("下載處理結果 (CSV)", csv, "report.csv", "text/csv")


