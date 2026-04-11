"""
ETL + AI Inference Pipeline
============================
Phase 1: Extract     - 구글 시트 → pandas DataFrame
Phase 2: Transform-D - groupby + 텍스트 병합 (결정론적)
Phase 3: Transform-S - LLM API 추론 (확률론적)
Phase 4: Load        - 결과를 구글 시트에 일괄 적재
"""
import json
import sys
import time

import gspread
import google.generativeai as genai
import pandas as pd

import config


# =============================================================
# 0. Initialization
# =============================================================
def init_gsheet():
    """구글 시트 클라이언트를 인증하고, 스프레드시트 객체를 반환한다."""
    gc = gspread.service_account(filename=config.GCP_SERVICE_ACCOUNT_FILE)
    sh = gc.open(config.SHEET_NAME)
    return sh


def init_llm():
    """Gemini API를 초기화한다."""
    genai.configure(api_key=config.GEMINI_API_KEY)


# =============================================================
# Phase 1: Extract
# =============================================================
def extract(sh) -> pd.DataFrame:
    """구글 시트의 입력 워크시트를 DataFrame으로 변환한다."""
    ws = sh.worksheet(config.INPUT_WORKSHEET)
    records = ws.get_all_records()

    if not records:
        print("[Extract] 시트에 데이터가 없습니다.")
        sys.exit(1)

    df = pd.DataFrame(records)
    print(f"[Extract] {len(df)}행 로드 완료. 컬럼: {list(df.columns)}")
    return df


# =============================================================
# Phase 2: Deterministic Transform
# =============================================================
def transform_deterministic(df: pd.DataFrame) -> pd.DataFrame:
    """GROUP_COLUMN 기준으로 TEXT_COLUMN을 ' --- '로 병합한다."""
    group_col = config.GROUP_COLUMN
    text_col = config.TEXT_COLUMN

    if group_col not in df.columns or text_col not in df.columns:
        print(f"[Transform-D] 필수 컬럼 누락: {group_col}, {text_col}")
        print(f"[Transform-D] 시트 컬럼 목록: {list(df.columns)}")
        sys.exit(1)

    # 결측치 제거 후 병합
    df[text_col] = df[text_col].astype(str).str.strip()
    df = df[df[text_col] != ""]

    df_grouped = df.groupby(group_col, as_index=False).agg(
        Merged_Text=(text_col, lambda x: " --- ".join(x)),
        Row_Count=(text_col, "count"),
    )
    print(f"[Transform-D] {len(df)}행 → {len(df_grouped)}그룹으로 병합 완료.")
    return df_grouped


# =============================================================
# Phase 3: Stochastic Transform (LLM Inference)
# =============================================================
PROMPT_TEMPLATE = """다음은 여러 데이터가 '---'로 구분되어 병합된 텍스트입니다.
이를 분석하여 적절한 카테고리로 분류하고, 전체를 요약하는 한 문장을 생성하세요.

데이터:
{merged_text}

반드시 아래 JSON 형식으로만 응답하세요. 다른 텍스트는 포함하지 마세요:
{{"category": "분류결과", "summary": "요약 문장"}}"""


def call_llm(merged_text: str) -> dict:
    """Gemini API를 호출하여 분류 + 요약을 수행한다."""
    model = genai.GenerativeModel(config.LLM_MODEL)
    prompt = PROMPT_TEMPLATE.format(merged_text=merged_text)

    response = model.generate_content(prompt)
    text = response.text.strip()

    # JSON 파싱 (```json ... ``` 래핑 제거)
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    return json.loads(text)


def transform_stochastic(df_grouped: pd.DataFrame) -> pd.DataFrame:
    """각 그룹에 대해 LLM 추론을 수행한다. 실패 시 해당 행만 스킵."""
    results = []
    total = len(df_grouped)

    print(f"[Transform-S] LLM 추론 시작 ({total}건)...")

    for idx, row in df_grouped.iterrows():
        group_id = row[config.GROUP_COLUMN]
        merged = row["Merged_Text"]

        try:
            llm_out = call_llm(merged)
            results.append({
                config.GROUP_COLUMN: group_id,
                "Merged_Text": merged,
                "Row_Count": row["Row_Count"],
                "Category": llm_out.get("category", ""),
                "Summary": llm_out.get("summary", ""),
                "Status": "OK",
            })
            print(f"  [{idx + 1}/{total}] Group '{group_id}' → {llm_out.get('category')}")

        except Exception as e:
            results.append({
                config.GROUP_COLUMN: group_id,
                "Merged_Text": merged,
                "Row_Count": row["Row_Count"],
                "Category": "",
                "Summary": "",
                "Status": f"ERROR: {e}",
            })
            print(f"  [{idx + 1}/{total}] Group '{group_id}' → ERROR: {e}")

        # Rate Limit 방어
        if idx < total - 1:
            time.sleep(config.API_DELAY_SECONDS)

    return pd.DataFrame(results)


# =============================================================
# Phase 4: Load
# =============================================================
def load(sh, df_final: pd.DataFrame):
    """결과 DataFrame을 구글 시트에 일괄 적재한다."""
    # 출력 워크시트 가져오기 (없으면 생성)
    try:
        ws_out = sh.worksheet(config.OUTPUT_WORKSHEET)
        ws_out.clear()
    except gspread.exceptions.WorksheetNotFound:
        ws_out = sh.add_worksheet(
            title=config.OUTPUT_WORKSHEET,
            rows=len(df_final) + 1,
            cols=len(df_final.columns),
        )

    # DataFrame → 2D 리스트 변환 (헤더 포함)
    header = df_final.columns.tolist()
    values = df_final.astype(str).values.tolist()
    payload = [header] + values

    # 단 1회의 API 호출로 일괄 업데이트
    ws_out.update(payload, "A1")
    print(f"[Load] '{config.OUTPUT_WORKSHEET}' 탭에 {len(df_final)}행 적재 완료.")


# =============================================================
# Main
# =============================================================
def main():
    print("=" * 60)
    print("ETL + AI Inference Pipeline")
    print("=" * 60)

    # Init
    sh = init_gsheet()
    init_llm()

    # Phase 1
    df = extract(sh)

    # Phase 2
    df_grouped = transform_deterministic(df)

    # Phase 3
    df_final = transform_stochastic(df_grouped)

    # Phase 4
    load(sh, df_final)

    # Summary
    ok_count = len(df_final[df_final["Status"] == "OK"])
    err_count = len(df_final[df_final["Status"] != "OK"])
    print("=" * 60)
    print(f"파이프라인 완료. 성공: {ok_count}, 실패: {err_count}")
    print("=" * 60)


if __name__ == "__main__":
    main()
