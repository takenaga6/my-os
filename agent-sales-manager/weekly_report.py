"""
weekly_report.py
Well Body 週次営業レポート自動生成スクリプト

動作の流れ：
1. Notion APIで「営業議事録」「2026年度営業戦略全体像」を取得
2. Google SheetsからKPIの実数値を取得
3. 先週のSlackスレッドへの返信をフィードバックとして取得
4. localhost:8006（Well Body商談分析ツール）から商談記録を取得
5. Claude APIに渡してMarkdownレポートを生成
6. reports/ フォルダに日付付きファイルとして保存
7. Slackの #営業 チャンネルに @尺長孝紀 メンション付きで投稿

必要な環境変数（.env ファイルに記載、または直接システム環境変数として設定）：
  ANTHROPIC_API_KEY           : Claude APIキー
  NOTION_API_KEY              : Notion インテグレーションキー
  SLACK_BOT_TOKEN             : Slack Bot Token（xoxb-で始まるもの）
                                必要なスコープ：chat:write, channels:history
  GOOGLE_SERVICE_ACCOUNT_JSON : Google サービスアカウントキーJSON
                                （JSON文字列 or JSONファイルパス）
  HUBSPOT_TOKEN               : HubSpot プライベートアプリトークン（省略可）

実行方法：
  python weekly_report.py
"""

import os
import sys
import json
import requests
from datetime import datetime
from anthropic import Anthropic

# .env ファイルを自動読み込み（python-dotenv）
# .env がなくても動作する（システム環境変数にフォールバック）
try:
    from dotenv import load_dotenv
    load_dotenv(
        dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
        override=True,
    )
except ImportError:
    pass  # python-dotenv 未インストール時はスキップ（環境変数から読む）


# ============================================================
# 定数
# ============================================================
MINUTES_PAGE_ID  = "2a32e3257cf4809a8b8fcdf14b29bd19"  # 営業議事録（2025/11〜）
STRATEGY_PAGE_ID = "3372e3257cf481519639c151ad320de0"  # 2026年度営業戦略全体像
SPREADSHEET_ID          = "1RufS0hXUXHMwTWv178l0fJniZybSkK0rWBM_XyEw55k"  # 2026年営業分析シート
SPREADSHEET_ID_2025     = "1r3RKBwPU41EaFdkxf_Zbw-sm8t3KXete4Mx7LuJ7xew"  # 2025年営業分析シート（前年比較用）
SPREADSHEET_ID_ACTIVITY = "1z5LyAiDyQIvqbYceDHBUPnJayRTRiZrkRYIUlhsQ5sw"  # 架電・商談シート（足元の日次動向）
SPREADSHEET_ID_INTERN   = "1n1RC7-U2Kpti-pMOgfYHWKD0JT7B0n4gsvOLoPde0uc"  # インターン業績シート（ROI分析用）
SPREADSHEET_ID_MEMBER   = "1NuURUrnmR_klr0ok1GmB1oUwRpvQrwFYWp2Y_MYOg68"  # ビジネスメンバー別成果（Looker Studio連携元）
SPREADSHEET_ID_FEEDBACK = "17t7zWa2NnX8ozsZNyZ1IB-gI7EBhZk_P7upscgQ2nYE"  # 商談フィードバックDB

SLACK_CHANNEL_ID  = "C04FK14K2QJ"   # #営業
SLACK_MENTION_UID = "U0635DZNKSQ"   # 尺長孝紀

# 先週の投稿情報を保存するファイル（Slackフィードバック取得に使用）
SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
LAST_POST_FILE = os.path.join(SCRIPT_DIR, "reports", "last_post_meta.json")


# ============================================================
# Notion API：ページ内容の取得
# ============================================================

def _extract_rich_text(rich_text_list: list) -> str:
    return "".join([rt.get("plain_text", "") for rt in rich_text_list])


def _blocks_to_markdown(blocks: list, notion_headers: dict, depth: int = 0) -> str:
    lines = []
    indent = "  " * depth

    for block in blocks:
        block_type = block.get("type", "")
        block_data = block.get(block_type, {})
        text = _extract_rich_text(block_data.get("rich_text", []))

        if block_type == "heading_1":
            lines.append(f"# {text}")
        elif block_type == "heading_2":
            lines.append(f"## {text}")
        elif block_type == "heading_3":
            lines.append(f"### {text}")
        elif block_type == "paragraph":
            lines.append(f"{indent}{text}" if text else "")
        elif block_type == "bulleted_list_item":
            lines.append(f"{indent}- {text}")
        elif block_type == "numbered_list_item":
            lines.append(f"{indent}1. {text}")
        elif block_type == "toggle":
            lines.append(f"{indent}**{text}**")
        elif block_type == "table_row":
            cells = block_data.get("cells", [])
            cell_texts = [_extract_rich_text(cell) for cell in cells]
            lines.append("| " + " | ".join(cell_texts) + " |")
        elif block_type == "callout":
            lines.append(f"> {text}")
        elif block_type == "divider":
            lines.append("---")
        elif block_type == "code":
            lang = block_data.get("language", "")
            lines.append(f"```{lang}\n{text}\n```")

        if block.get("has_children"):
            child_blocks = fetch_block_children(block["id"], notion_headers)
            child_text = _blocks_to_markdown(child_blocks, notion_headers, depth + 1)
            if child_text:
                lines.append(child_text)

    return "\n".join(lines)


def fetch_block_children(block_id: str, headers: dict) -> list:
    results = []
    url = f"https://api.notion.com/v1/blocks/{block_id}/children"
    params = {"page_size": 100}

    while True:
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        data = response.json()
        results.extend(data.get("results", []))
        if data.get("has_more"):
            params["start_cursor"] = data["next_cursor"]
        else:
            break

    return results


def fetch_notion_page_as_text(page_id: str, notion_api_key: str) -> str:
    headers = {
        "Authorization": f"Bearer {notion_api_key}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }

    page_response = requests.get(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers=headers,
    )
    page_response.raise_for_status()
    page_data = page_response.json()

    title = ""
    for prop in page_data.get("properties", {}).values():
        if prop.get("type") == "title":
            title = _extract_rich_text(prop.get("title", []))
            break

    blocks = fetch_block_children(page_id, headers)
    content_md = _blocks_to_markdown(blocks, headers)

    return f"# {title}\n\n{content_md}"


# ============================================================
# Google Sheets API：KPI実数値の取得
# ============================================================

def _get_gspread_client_sa(service_account_json: str):
    """
    サービスアカウント認証でgspreadクライアントを返す共通関数。
    GOOGLE_SERVICE_ACCOUNT_JSON 環境変数の値（JSON文字列またはファイルパス）を受け取る。
    ブラウザ認証不要でサーバー上でも動作する。
    """
    import gspread
    from google.oauth2.service_account import Credentials

    SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

    # JSON文字列かファイルパスかを自動判定
    stripped = service_account_json.strip()
    if stripped.startswith("{"):
        # JSON文字列として直接パース
        sa_info = json.loads(stripped)
    else:
        # ファイルパスとして読み込む
        with open(stripped, encoding="utf-8") as f:
            sa_info = json.load(f)

    creds = Credentials.from_service_account_info(sa_info, scopes=SCOPES)
    return gspread.authorize(creds)


def _fetch_sheet_rows(spreadsheet, sheet_name: str, max_rows: int = 30) -> str:
    """指定シート名のデータを取得してタブ区切り文字列で返す。存在しなければ空文字。"""
    try:
        ws = spreadsheet.worksheet(sheet_name)
        rows = ws.get_all_values()
        non_empty = [r for r in rows if any(c.strip() for c in r)][:max_rows]
        return "\n".join(["\t".join(r) for r in non_empty])
    except Exception:
        return ""


def fetch_spreadsheet_kpi(service_account_json: str) -> str:
    """
    4つのGoogle SheetsからKPI・活動データを取得してまとめて返す。
    サービスアカウント認証を使用（GOOGLE_SERVICE_ACCOUNT_JSON）。

    取得対象：
    ① 2026年営業分析シート：今期のKPI全体（年間推移・月次推移・商談記入）
    ② 2025年営業分析シート：前年同期比較用
    ③ 架電・商談シート：足元の日次活動（架電数・アポ数・商談結果）
    ④ インターン業績シート：インターン別成果・ROI分析
    """
    try:
        import gspread
        gc = _get_gspread_client_sa(service_account_json)
        result_parts = []

        # ===== ① 2026年営業分析シート =====
        try:
            ss = gc.open_by_key(SPREADSHEET_ID)
            parts = []

            # 年間推移
            rows = _fetch_sheet_rows(ss, "年間推移", max_rows=25)
            if rows:
                parts.append("#### 年間推移\n" + rows)

            # 今月の推移シート（例：4月推移）
            now = datetime.now()
            for ws in ss.worksheets():
                if f"{now.month}月" in ws.title and "推移" in ws.title:
                    rows = _fetch_sheet_rows(ss, ws.title, max_rows=30)
                    if rows:
                        parts.append(f"#### {ws.title}\n" + rows)
                    break

            # 商談記入（直近50件）
            try:
                ws = ss.worksheet("商談記入")
                all_rows = ws.get_all_values()
                if all_rows:
                    header = all_rows[0]
                    recent = all_rows[-50:] if len(all_rows) > 50 else all_rows[1:]
                    parts.append(
                        "#### 商談記入（直近50件）\n"
                        + "\t".join(header) + "\n"
                        + "\n".join(["\t".join(r) for r in recent])
                    )
            except Exception:
                pass

            if parts:
                result_parts.append("### 【今期】2026年営業分析シート\n" + "\n\n".join(parts))

        except Exception as e:
            result_parts.append(f"### 【今期】2026年営業分析シート\n取得失敗：{e}")

        # ===== ② 2025年営業分析シート（前年比較用） =====
        try:
            ss2025 = gc.open_by_key(SPREADSHEET_ID_2025)
            parts = []

            rows = _fetch_sheet_rows(ss2025, "年間推移", max_rows=25)
            if rows:
                parts.append("#### 年間推移（2025年）\n" + rows)

            # 商談記入があれば取得
            try:
                ws = ss2025.worksheet("商談記入")
                all_rows = ws.get_all_values()
                if all_rows:
                    header = all_rows[0]
                    recent = all_rows[-30:] if len(all_rows) > 30 else all_rows[1:]
                    parts.append(
                        "#### 商談記入2025（直近30件）\n"
                        + "\t".join(header) + "\n"
                        + "\n".join(["\t".join(r) for r in recent])
                    )
            except Exception:
                pass

            if parts:
                result_parts.append(
                    "### 【前年比較】2025年営業分析シート（今年との比較に使用）\n"
                    + "\n\n".join(parts)
                )

        except Exception as e:
            result_parts.append(f"### 【前年比較】2025年営業分析シート\n取得失敗：{e}")

        # ===== ③ 架電・商談シート（足元の日次動向） =====
        try:
            ss_activity = gc.open_by_key(SPREADSHEET_ID_ACTIVITY)
            parts = []

            # 全シートを取得して直近データを抽出
            for ws in ss_activity.worksheets():
                all_rows = ws.get_all_values()
                if not all_rows:
                    continue
                # 直近40行（ヘッダー含む）を取得
                header = all_rows[0]
                recent = all_rows[-40:] if len(all_rows) > 40 else all_rows[1:]
                parts.append(
                    f"#### {ws.title}\n"
                    + "\t".join(header) + "\n"
                    + "\n".join(["\t".join(r) for r in recent])
                )

            if parts:
                result_parts.append(
                    "### 【足元動向】架電・商談シート（日次活動データ）\n"
                    + "\n\n".join(parts)
                )

        except Exception as e:
            result_parts.append(f"### 【足元動向】架電・商談シート\n取得失敗：{e}")

        # ===== ④ インターン業績シート（ROI分析用） =====
        try:
            ss_intern = gc.open_by_key(SPREADSHEET_ID_INTERN)
            parts = []

            for ws in ss_intern.worksheets():
                all_rows = ws.get_all_values()
                if not all_rows:
                    continue
                non_empty = [r for r in all_rows if any(c.strip() for c in r)][:40]
                parts.append(
                    f"#### {ws.title}\n"
                    + "\n".join(["\t".join(r) for r in non_empty])
                )

            if parts:
                result_parts.append(
                    "### 【インターンROI】インターン業績シート\n"
                    + "\n\n".join(parts)
                )

        except Exception as e:
            result_parts.append(f"### 【インターンROI】インターン業績シート\n取得失敗：{e}")

        # ===== ⑤ ビジネスメンバー別成果（Looker Studio連携元） =====
        try:
            ss_member = gc.open_by_key(SPREADSHEET_ID_MEMBER)
            parts = []

            for ws in ss_member.worksheets():
                all_rows = ws.get_all_values()
                if not all_rows:
                    continue

                header = all_rows[0]

                # データあり行のみ抽出（空行・#DIV/0!のみ行はスキップ）
                data_rows = []
                for row in all_rows[1:]:
                    # 名前列（index 1）が空なら除外
                    if not row[1].strip():
                        continue
                    # 業務時間・架電件数など数値列が全部空なら除外
                    numeric_cols = [row[i].strip() for i in range(2, min(8, len(row)))]
                    if not any(c and c != '#DIV/0!' for c in numeric_cols):
                        continue
                    # #DIV/0! を "-" に置換して見やすく
                    cleaned = [c if c != '#DIV/0!' else '-' for c in row]
                    data_rows.append(cleaned)

                if data_rows:
                    parts.append(
                        f"#### {ws.title}\n"
                        + "\t".join(header) + "\n"
                        + "\n".join(["\t".join(r) for r in data_rows])
                    )

            if parts:
                result_parts.append(
                    "### 【メンバー別成果】ビジネスメンバー（セールス）\n"
                    "※Looker Studioで可視化しているメンバー別の架電数・アポ率・接続率データ\n"
                    + "\n\n".join(parts)
                )

        except Exception as e:
            result_parts.append(f"### 【メンバー別成果】ビジネスメンバーシート\n取得失敗：{e}")

        return "\n\n".join(result_parts)

    except ImportError:
        return "スキップ：Google Sheets連携ライブラリ未インストール（pip install gspread google-auth google-auth-oauthlib）"
    except Exception as e:
        return f"スプレッドシート取得エラー：{e}"


# ============================================================
# Google Sheets：商談フィードバックDB（独立取得）
# ============================================================

def fetch_feedback_sheet(service_account_json: str) -> str:
    """
    商談フィードバックDBシート（ID: SPREADSHEET_ID_FEEDBACK）から
    商談スコア・失注理由・hit/miss/objection数を取得する。

    ① 失敗しても他のデータ取得には影響しない独立設計
    ② 失敗時は「（取得失敗）」文字列を返す
    """
    try:
        import gspread
        gc = _get_gspread_client_sa(service_account_json)
        ss = gc.open_by_key(SPREADSHEET_ID_FEEDBACK)

        parts = []

        # 全シートを走査（シート名に関係なく全データを読む）
        for ws in ss.worksheets():
            try:
                all_rows = ws.get_all_values()
                if not all_rows or len(all_rows) < 2:
                    continue

                header = all_rows[0]
                data_rows = [r for r in all_rows[1:] if any(c.strip() for c in r)]

                if not data_rows:
                    continue

                # 直近50件に絞る
                recent = data_rows[-50:] if len(data_rows) > 50 else data_rows

                parts.append(
                    f"#### {ws.title}（{len(data_rows)}件中直近{len(recent)}件）\n"
                    + "\t".join(header) + "\n"
                    + "\n".join(["\t".join(r) for r in recent])
                )
            except Exception:
                continue

        if not parts:
            return "（商談フィードバックDB：データなし）"

        return "### 【商談フィードバックDB】商談スコア・失注理由・hit/miss/objection\n" + "\n\n".join(parts)

    except ImportError:
        return "（取得失敗：gspread未インストール）"
    except Exception as e:
        return f"（取得失敗：{e}）"


# ============================================================
# HubSpot：今月のアポ・架電結果集計（独立取得）
# ============================================================

def fetch_hubspot_data(hubspot_token: str) -> str:
    """
    HubSpotから今月のアポ獲得数・架電結果の集計を取得する。

    取得内容：
    - 今月作成された架電エンゲージメント（Call）と結果別件数
    - 今月作成されたディール（Deal）の件数とステージ分布
    - 今月更新されたカンパニーの体験状況（taiken_status）集計

    失敗しても他のデータ取得には影響しない独立設計。
    """
    if not hubspot_token:
        return "（取得失敗：HUBSPOT_TOKEN未設定）"

    hs_headers = {
        "Authorization": f"Bearer {hubspot_token}",
        "Content-Type": "application/json",
    }
    base_url = "https://api.hubapi.com"

    # 今月の開始タイムスタンプ（ミリ秒）
    now = datetime.now()
    month_start = datetime(now.year, now.month, 1)
    month_start_ms = int(month_start.timestamp() * 1000)

    parts = []

    # ===== ① 架電エンゲージメント（Call）の集計 =====
    try:
        call_resp = requests.post(
            f"{base_url}/crm/v3/objects/calls/search",
            headers=hs_headers,
            json={
                "filterGroups": [{
                    "filters": [{
                        "propertyName": "hs_timestamp",
                        "operator": "GTE",
                        "value": str(month_start_ms),
                    }]
                }],
                "properties": ["hs_call_status", "hs_call_disposition", "hs_timestamp"],
                "limit": 100,
            },
            timeout=10,
        )
        if call_resp.ok:
            call_data = call_resp.json()
            call_records = call_data.get("results", [])
            total_calls = call_data.get("total", len(call_records))

            # 結果別に集計
            disposition_counts: dict[str, int] = {}
            for rec in call_records:
                props = rec.get("properties", {})
                disp = props.get("hs_call_disposition", "") or props.get("hs_call_status", "") or "不明"
                disposition_counts[disp] = disposition_counts.get(disp, 0) + 1

            call_lines = [f"#### 今月の架電（Call）集計（総数：{total_calls}件）"]
            for disp, cnt in sorted(disposition_counts.items(), key=lambda x: -x[1]):
                call_lines.append(f"- {disp}: {cnt}件")
            parts.append("\n".join(call_lines))
        else:
            parts.append(f"#### 架電集計：取得失敗（{call_resp.status_code}）")
    except Exception as e:
        parts.append(f"#### 架電集計：取得失敗（{e}）")

    # ===== ② 今月作成されたDeal（アポ）の集計 =====
    try:
        deal_resp = requests.post(
            f"{base_url}/crm/v3/objects/deals/search",
            headers=hs_headers,
            json={
                "filterGroups": [{
                    "filters": [{
                        "propertyName": "createdate",
                        "operator": "GTE",
                        "value": str(month_start_ms),
                    }]
                }],
                "properties": ["dealname", "dealstage", "createdate", "amount"],
                "limit": 100,
            },
            timeout=10,
        )
        if deal_resp.ok:
            deal_data = deal_resp.json()
            deal_records = deal_data.get("results", [])
            total_deals = deal_data.get("total", len(deal_records))

            # ステージ別に集計
            stage_counts: dict[str, int] = {}
            for rec in deal_records:
                stage = rec.get("properties", {}).get("dealstage", "不明") or "不明"
                stage_counts[stage] = stage_counts.get(stage, 0) + 1

            deal_lines = [f"#### 今月のDeal（アポ）集計（総数：{total_deals}件）"]
            for stage, cnt in sorted(stage_counts.items(), key=lambda x: -x[1]):
                deal_lines.append(f"- {stage}: {cnt}件")
            parts.append("\n".join(deal_lines))
        else:
            parts.append(f"#### Deal集計：取得失敗（{deal_resp.status_code}）")
    except Exception as e:
        parts.append(f"#### Deal集計：取得失敗（{e}）")

    # ===== ③ 今月更新されたカンパニーの体験状況（taiken_status）集計 =====
    try:
        company_resp = requests.post(
            f"{base_url}/crm/v3/objects/companies/search",
            headers=hs_headers,
            json={
                "filterGroups": [{
                    "filters": [{
                        "propertyName": "hs_lastmodifieddate",
                        "operator": "GTE",
                        "value": str(month_start_ms),
                    }]
                }],
                "properties": ["name", "taiken_status", "shodan_result", "shodan_score", "a_12"],
                "limit": 100,
            },
            timeout=10,
        )
        if company_resp.ok:
            co_data = company_resp.json()
            co_records = co_data.get("results", [])
            total_cos = co_data.get("total", len(co_records))

            # taiken_status別に集計
            taiken_counts: dict[str, int] = {}
            result_counts: dict[str, int] = {}
            rank_counts: dict[str, int] = {}
            for rec in co_records:
                props = rec.get("properties", {})
                taiken = props.get("taiken_status", "") or ""
                result = props.get("shodan_result", "") or ""
                rank   = props.get("a_12", "") or ""
                if taiken:
                    taiken_counts[taiken] = taiken_counts.get(taiken, 0) + 1
                if result:
                    result_counts[result] = result_counts.get(result, 0) + 1
                if rank:
                    rank_counts[rank] = rank_counts.get(rank, 0) + 1

            co_lines = [f"#### 今月更新カンパニー（{total_cos}件）"]
            if taiken_counts:
                co_lines.append("体験状況別：" + "、".join(f"{k}:{v}件" for k, v in sorted(taiken_counts.items(), key=lambda x: -x[1])))
            if result_counts:
                co_lines.append("商談結果別：" + "、".join(f"{k}:{v}件" for k, v in sorted(result_counts.items(), key=lambda x: -x[1])))
            if rank_counts:
                co_lines.append("リストランク別：" + "、".join(f"{k}:{v}件" for k, v in sorted(rank_counts.items())))
            parts.append("\n".join(co_lines))
        else:
            parts.append(f"#### カンパニー集計：取得失敗（{company_resp.status_code}）")
    except Exception as e:
        parts.append(f"#### カンパニー集計：取得失敗（{e}）")

    if not parts:
        return "（HubSpotデータなし）"

    return (
        f"### 【HubSpot】今月（{now.month}月）の営業活動集計\n"
        + "\n\n".join(parts)
    )


# ============================================================
# HubSpot：リストシグナル別アポ獲得率集計（独立取得）
# ============================================================

def get_hubspot_signal_stats(hubspot_token: str) -> str:
    """
    HubSpotからリストシグナル別アポ獲得率を集計する（直近30日）。

    取得プロパティ：
    - a_12（リストランク）、apurochinaiyou（架電結果）、apurochibi（アプローチ日）
    - houteigaifukurikouseinokisaiari（S3）、kenkoukeieihenochuuryoku（S4）
    - hantoshiinaihprinyuaru（S5）、jishabiruhoyuu（S6）、industry（業種）

    失敗しても他のデータ取得には影響しない独立設計。
    """
    if not hubspot_token:
        return "（HubSpot取得失敗: HUBSPOT_TOKEN未設定）"

    try:
        from datetime import timedelta

        hs_headers = {
            "Authorization": f"Bearer {hubspot_token}",
            "Content-Type": "application/json",
        }
        base_url = "https://api.hubapi.com"

        # 直近30日のタイムスタンプ（ミリ秒）
        now = datetime.now()
        thirty_days_ago_ms = int((now - timedelta(days=30)).timestamp() * 1000)

        # ページネーションしながら全件取得
        all_records: list = []
        after: str | None = None
        while True:
            payload: dict = {
                "filterGroups": [{
                    "filters": [{
                        "propertyName": "apurochibi",
                        "operator": "GTE",
                        "value": str(thirty_days_ago_ms),
                    }]
                }],
                "properties": [
                    "a_12",
                    "apurochinaiyou",
                    "apurochibi",
                    "houteigaifukurikouseinokisaiari",
                    "kenkoukeieihenochuuryoku",
                    "hantoshiinaihprinyuaru",
                    "jishabiruhoyuu",
                    "industry",
                ],
                "limit": 100,
            }
            if after:
                payload["after"] = after

            resp = requests.post(
                f"{base_url}/crm/v3/objects/companies/search",
                headers=hs_headers,
                json=payload,
                timeout=15,
            )
            if not resp.ok:
                return f"（HubSpot取得失敗: {resp.status_code}）"

            data = resp.json()
            all_records.extend(data.get("results", []))

            # ページネーション：paging.next.after が存在すれば次ページあり
            after = data.get("paging", {}).get("next", {}).get("after")
            if not after:
                break

        if not all_records:
            return "（直近30日のデータなし）"

        # ── 集計 ───────────────────────────────────────────────────
        # ランク別
        rank_stats: dict[str, dict] = {}
        # シグナル別
        _SIGNAL_META = {
            "S3": {"label": "法定外福利厚生あり", "prop": "houteigaifukurikouseinokisaiari"},
            "S4": {"label": "健康経営注力あり",   "prop": "kenkoukeieihenochuuryoku"},
            "S5": {"label": "HPリニューアルあり", "prop": "hantoshiinaihprinyuaru"},
            "S6": {"label": "自社ビルあり",       "prop": "jishabiruhoyuu"},
        }
        signal_stats: dict[str, dict] = {
            k: {"label": v["label"], "total": 0, "shacho_apo": 0}
            for k, v in _SIGNAL_META.items()
        }
        # 業種別
        industry_stats: dict[str, dict] = {}

        for rec in all_records:
            props = rec.get("properties", {})
            rank     = props.get("a_12", "") or ""
            approach = props.get("apurochinaiyou", "") or ""

            is_shacho_apo = (approach == "社長アポ")
            is_tanto_apo  = (approach == "担当アポ")

            # ランク別集計
            if rank:
                if rank not in rank_stats:
                    rank_stats[rank] = {"total": 0, "shacho_apo": 0, "tanto_apo": 0}
                rank_stats[rank]["total"] += 1
                if is_shacho_apo:
                    rank_stats[rank]["shacho_apo"] += 1
                if is_tanto_apo:
                    rank_stats[rank]["tanto_apo"] += 1

            # シグナル別集計（プロパティ値が "true" の場合のみカウント）
            for sig_key, meta in _SIGNAL_META.items():
                if props.get(meta["prop"], "") == "true":
                    signal_stats[sig_key]["total"] += 1
                    if is_shacho_apo:
                        signal_stats[sig_key]["shacho_apo"] += 1

            # 業種別集計
            industry = props.get("industry", "") or ""
            if industry:
                if industry not in industry_stats:
                    industry_stats[industry] = {"total": 0, "shacho_apo": 0}
                industry_stats[industry]["total"] += 1
                if is_shacho_apo:
                    industry_stats[industry]["shacho_apo"] += 1

        # ── テキスト組み立て ────────────────────────────────────────
        lines = [
            "## 🎯 リストシグナル別アポ獲得率（直近30日）",
            f"集計件数: {len(all_records)}件",
            "",
            "### ランク別",
            "| ランク | 件数 | 社長アポ | 担当アポ | 社長アポ率 | 総アポ率 |",
            "|---|---|---|---|---|---|",
        ]

        for rank_label, rank_key in [("Aランク", "A"), ("Bランク", "B"), ("Cランク", "C")]:
            s = rank_stats.get(rank_key, {"total": 0, "shacho_apo": 0, "tanto_apo": 0})
            total  = s["total"]
            shacho = s["shacho_apo"]
            tanto  = s["tanto_apo"]
            total_apo = shacho + tanto
            shacho_rate = f"{shacho / total * 100:.1f}%" if total > 0 else "-"
            total_rate  = f"{total_apo / total * 100:.1f}%" if total > 0 else "-"
            lines.append(
                f"| {rank_label} | {total}件 | {shacho}件 | {tanto}件 | {shacho_rate} | {total_rate} |"
            )

        lines += [
            "",
            "### シグナル別（S3〜S6）",
            "| シグナル | 件数 | 社長アポ | 社長アポ率 |",
            "|---|---|---|---|",
        ]
        for sig_key in ["S3", "S4", "S5", "S6"]:
            s = signal_stats[sig_key]
            total  = s["total"]
            shacho = s["shacho_apo"]
            rate   = f"{shacho / total * 100:.1f}%" if total > 0 else "-"
            lines.append(f"| {s['label']} | {total}件 | {shacho}件 | {rate} |")

        # 業種別（5件以上のみ・件数降順）
        industry_filtered = {
            k: v for k, v in industry_stats.items() if v["total"] >= 5
        }
        if industry_filtered:
            lines += [
                "",
                "### 業種別（5件以上）",
                "| 業種 | 件数 | 社長アポ | 社長アポ率 |",
                "|---|---|---|---|",
            ]
            for ind, s in sorted(industry_filtered.items(), key=lambda x: -x[1]["total"]):
                total  = s["total"]
                shacho = s["shacho_apo"]
                rate   = f"{shacho / total * 100:.1f}%" if total > 0 else "-"
                lines.append(f"| {ind} | {total}件 | {shacho}件 | {rate} |")

        return "\n".join(lines)

    except Exception as e:
        return f"（HubSpot取得失敗: {e}）"


# ============================================================
# Slack：先週のフィードバック取得
# ============================================================

def load_last_post_meta() -> dict | None:
    """
    前回投稿したSlackメッセージのメタ情報（thread_ts等）を読み込む。
    ファイルがなければNoneを返す。
    """
    if os.path.exists(LAST_POST_FILE):
        with open(LAST_POST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def save_last_post_meta(thread_ts: str, channel_id: str) -> None:
    """
    今週投稿したSlackメッセージのメタ情報を保存する。
    次週のレポート生成時にフィードバック取得に使用する。
    """
    os.makedirs(os.path.dirname(LAST_POST_FILE), exist_ok=True)
    meta = {
        "thread_ts": thread_ts,
        "channel_id": channel_id,
        "posted_at": datetime.now().isoformat(),
    }
    with open(LAST_POST_FILE, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def fetch_slack_feedback(slack_token: str) -> str:
    """
    先週投稿したSlackスレッドへの返信を取得してフィードバックとして返す。

    使い方：
    - 毎週月曜にレポートが投稿される
    - チームメンバーがそのスレッドに返信でフィードバックを書く
    - 翌週のレポート生成時にその返信内容がプロンプトに含まれる
    - Claudeがフィードバックを反映したレポートを生成する

    必要なSlackスコープ：channels:history（chat:writeに追加で必要）
    """
    meta = load_last_post_meta()
    if not meta:
        return ""  # 初回実行時はフィードバックなし

    try:
        response = requests.get(
            "https://slack.com/api/conversations.replies",
            headers={"Authorization": f"Bearer {slack_token}"},
            params={
                "channel": meta["channel_id"],
                "ts": meta["thread_ts"],
                "limit": 50,
            },
        )
        response.raise_for_status()
        data = response.json()

        if not data.get("ok"):
            return ""

        messages = data.get("messages", [])
        # 最初のメッセージ（レポート本文）を除いた返信だけ取得
        replies = messages[1:]

        if not replies:
            return ""

        feedback_lines = ["## 先週レポートへのフィードバック（Slackスレッド返信）"]
        for msg in replies:
            # Botのメッセージは除外（人間の返信だけ使う）
            if msg.get("bot_id"):
                continue
            text = msg.get("text", "").strip()
            if text:
                feedback_lines.append(f"- {text}")

        if len(feedback_lines) == 1:
            return ""  # 人間の返信がなければスキップ

        return "\n".join(feedback_lines)

    except Exception as e:
        return f"フィードバック取得エラー（スキップ）：{e}"


# ============================================================
# localhost:8006 商談分析ツール：商談記録の取得・集計
# ============================================================

SHODAN_TOOL_URL = "http://localhost:8006"


def fetch_shodan_analysis() -> str:
    """
    Well Body商談分析ツール（localhost:8006）から商談記録を取得し、
    週次レポート用に集計した文字列を返す。

    取得するデータ：
    - 全商談記録（result=win/hold/loss）
    - hit/miss/objectionの集計
    - 失注理由カテゴリの分布
    - スコア平均
    - 直近10件の商談サマリー

    商談分析ツールにデータが0件の場合は空文字を返す。
    ツールが起動していない場合はスキップ（エラーにしない）。
    """
    try:
        # 全商談記録を取得
        response = requests.get(
            f"{SHODAN_TOOL_URL}/knowledge",
            timeout=5,  # ローカルサーバーなので5秒でタイムアウト
        )
        response.raise_for_status()
        records: list = response.json()

        if not records:
            return ""  # データなし→スキップ

        total = len(records)

        # --- result（結果）別の件数集計 ---
        # index.html・server.py では win/hold/loss を使用（success/ngは旧仕様）
        result_counts: dict[str, int] = {"win": 0, "hold": 0, "loss": 0, "other": 0}
        for r in records:
            result = r.get("result", "").lower()
            if result in result_counts:
                result_counts[result] += 1
            else:
                result_counts["other"] += 1

        # 成約率 = win / (win + loss)  ※holdは保留中なので分母に含めない
        closed = result_counts["win"] + result_counts["loss"]
        win_rate = (result_counts["win"] / closed * 100) if closed > 0 else 0

        # --- hit/miss/objectionの集計 ---
        all_hits: list[str] = []
        all_misses: list[str] = []
        all_objections: list[str] = []

        for r in records:
            for h in r.get("hits", []):
                label = h.get("label") or h.get("text") or str(h)
                if label:
                    all_hits.append(label)
            for m in r.get("misses", []):
                label = m.get("label") or m.get("text") or str(m)
                if label:
                    all_misses.append(label)
            for o in r.get("objections", []):
                label = o.get("label") or o.get("text") or str(o)
                if label:
                    all_objections.append(label)

        def _top_items(items: list[str], n: int = 5) -> list[tuple[str, int]]:
            counts: dict[str, int] = {}
            for item in items:
                counts[item] = counts.get(item, 0) + 1
            return sorted(counts.items(), key=lambda x: -x[1])[:n]

        top_hits = _top_items(all_hits)
        top_misses = _top_items(all_misses)
        top_objections = _top_items(all_objections)

        # --- 失注理由カテゴリの集計 ---
        loss_counts: dict[str, int] = {}
        for r in records:
            cat = r.get("loss_category", "").strip()
            if cat:
                loss_counts[cat] = loss_counts.get(cat, 0) + 1

        # --- スコア平均 ---
        scores = [r.get("score", 0) for r in records if r.get("score")]
        avg_score = sum(scores) / len(scores) if scores else 0

        # --- 直近10件のサマリー ---
        recent = sorted(
            records,
            key=lambda r: r.get("meeting_date", ""),
            reverse=True,
        )[:10]

        # --- テキスト組み立て ---
        lines = [
            "### 【商談分析ツール】Well Body商談記録サマリー",
            f"総商談件数: {total}件（成約: {result_counts['win']}件, 保留: {result_counts['hold']}件, 失注: {result_counts['loss']}件）",
            f"成約率（クローズド商談ベース）: {win_rate:.1f}%",
        ]

        if avg_score:
            lines.append(f"商談スコア平均: {avg_score:.1f}点")

        if top_objections:
            lines.append("\n#### よく出る反論（Objection Top5）")
            for obj, cnt in top_objections:
                lines.append(f"- {obj}（{cnt}件）")

        if top_hits:
            lines.append("\n#### ヒットトーク Top5")
            for h, cnt in top_hits:
                lines.append(f"- {h}（{cnt}件）")

        if top_misses:
            lines.append("\n#### ミストーク Top5")
            for m, cnt in top_misses:
                lines.append(f"- {m}（{cnt}件）")

        if loss_counts:
            lines.append("\n#### 失注理由カテゴリ分布")
            for cat, cnt in sorted(loss_counts.items(), key=lambda x: -x[1]):
                lines.append(f"- {cat}: {cnt}件")

        if recent:
            lines.append("\n#### 直近10件の商談記録")
            for r in recent:
                date_str  = r.get("meeting_date", "-")
                company   = r.get("company_name", "-")
                industry  = r.get("industry", "-")
                result    = r.get("result", "-")
                score_val = r.get("score", "-")
                next_act  = r.get("next_action", "")
                summary_parts = [f"{date_str} | {company} | {industry} | 結果:{result} | スコア:{score_val}"]
                if next_act:
                    summary_parts.append(f"次アクション:{next_act}")
                lines.append("- " + " / ".join(summary_parts))

        return "\n".join(lines)

    except requests.exceptions.ConnectionError:
        # サーバー起動していない場合は静かにスキップ
        return ""
    except Exception as e:
        return f"商談分析ツール取得エラー（スキップ）：{e}"


# ============================================================
# Claude API：レポート生成
# ============================================================

def get_week_label() -> str:
    now = datetime.now()
    week_of_month = (now.day - 1) // 7 + 1
    return f"{now.month}月{week_of_month}週目"


def generate_report(
    minutes_content: str,
    strategy_content: str,
    kpi_data: str,
    slack_feedback: str,
    client: Anthropic,
    shodan_data: str = "",
    feedback_sheet_data: str = "",
    hubspot_data: str = "",
) -> str:
    week_label = get_week_label()

    # フィードバックセクションの組み立て（あれば含める）
    feedback_section = ""
    if slack_feedback:
        feedback_section = f"""
---

## 先週レポートへのフィードバック
{slack_feedback}

※上記フィードバックを今週のレポートに反映すること。
"""

    # KPIセクションの組み立て（取得できていれば含める）
    kpi_section = ""
    if kpi_data and "スキップ" not in kpi_data and "エラー" not in kpi_data:
        kpi_section = f"""
---

## Google Sheetsの実数値データ（4シート分）
{kpi_data}

※読み取り方針：
- 【今期】2026年シート：KPI現状欄の数値に使用・「4月推移」の月累計実績列（成約数・アポ数・CPA・商談数・テレアポ数）を必ず確認
- 【前年比較】2025年シート：前年同月の成約数・アポ数・CPAを取得して成長・後退を分析
- 【足元動向】架電・商談シート：今週の具体的な活動量と成果を把握
- 【インターンROI/メンバー別成果】：インターン1人あたりのアポ数・コスト対効果を算出、媒体別アポ獲得率を分析
"""

    # 商談フィードバックDBセクション（取得できていれば含める）
    feedback_sheet_section = ""
    if feedback_sheet_data and "取得失敗" not in feedback_sheet_data and "データなし" not in feedback_sheet_data:
        feedback_sheet_section = f"""
---

## 商談フィードバックDB（スプレッドシート）
{feedback_sheet_data}

※活用方針：
- 商談スコアの分布と推移を確認し、平均スコアが低下していればトーク改善を指示すること
- 失注理由の頻出パターンからボトルネックを特定すること
- hit/miss/objection数の変化から業種別・担当者別の傾向を読み取ること
- 業種別商談成功パターンを「業種別商談成功パターン」セクションに反映すること
"""
    elif feedback_sheet_data:
        feedback_sheet_section = f"\n---\n\n## 商談フィードバックDB\n{feedback_sheet_data}\n"

    # HubSpotセクション（取得できていれば含める）
    hubspot_section = ""
    if hubspot_data and "取得失敗" not in hubspot_data and "未設定" not in hubspot_data:
        hubspot_section = f"""
---

## HubSpot 今月の営業活動集計
{hubspot_data}

※活用方針：
- 架電件数と結果別分布からインターンの架電活動を評価すること
- Deal件数をアポ獲得数として使用すること
- カンパニーの体験状況集計を「媒体別アポ獲得率」分析に活用すること
"""
    elif hubspot_data:
        hubspot_section = f"\n---\n\n## HubSpot集計\n{hubspot_data}\n"

    # 商談分析ツールセクション（データがあれば含める）
    shodan_section = ""
    if shodan_data and "エラー" not in shodan_data:
        shodan_section = f"""
---

## Well Body商談分析ツール（録音・メモ分析データ）
{shodan_data}

※活用方針：
- 反論Top5はトーク改善指示の根拠として使用すること
- 失注カテゴリ分布はボトルネック特定に使用すること
- 商談スコア平均が低下していればトーニング指示を出すこと
- 直近10件の結果・次アクションを「インターンへの指示」に反映すること
"""

    prompt = f"""
あなたはWell Body株式会社の営業部長の分身AIです。
以下のデータをもとに、週次営業レポートをMarkdown形式で生成してください。

## 会社・事業のコンテキスト
- 事業：Offi-Stretch®（企業向けフィジカルケアサービス）
- 体制：尺長（全体統括・クロージング）、河野（インサイドセールス統括）、インターン（テレアポ〜アポ獲得）
- 評価ランク：丁稚奉公 → 前座 → 二ツ目 → 真打 → 看板 → 大看板
- インセンティブ：社長アポ4,000円/件、担当アポ1,000円/件

---

## Notion議事録（2025/11〜）
{minutes_content}

---

## 2026年度営業戦略全体像
{strategy_content}
{kpi_section}
{feedback_sheet_section}
{hubspot_section}
{shodan_section}
{feedback_section}

---

## 出力形式

# Well Body 週次営業レポート（{week_label}）

## 今週の重点課題
（議事録の直近の意思決定・課題から3〜5点。具体的な数値や固有名詞を含めること）

## 先週の指示の効果検証
（前回レポートで出したインターンへの指示に対して、今週の数値がどう変化したかを検証。
 変化がプラスなら「✅ 効果あり」、変化なしなら「⚠ 変化なし」、悪化なら「❌ 悪化」で明示すること。
 前回レポートとの比較ができない場合は「（初回 or 比較データなし）」と明記すること）

## KPI現状
| 指標 | 目標 | 現状（月累計） | 乖離 |
|---|---|---|---|
| 成約数 | - | ← Sheetsの月累計実績値 | - |
| アポ数 | - | ← Sheetsの月累計実績値 | - |
| CPA | 20,000円 | ← Sheetsの月累計実績値 | - |
| 商談数 | - | ← Sheetsの月累計実績値 | - |
| テレアポ数 | - | ← Sheetsの月累計実績値 | - |
| 前年同月成約数 | - | ← 2025年シートの値 | - |
| 前年同月アポ数 | - | ← 2025年シートの値 | - |

※Google Sheetsの実数値がある場合は必ず入れること。不明な場合は「-」

## 媒体別アポ獲得率
（メンバー別成果シートやHubSpotデータから、媒体・リストソース別のアポ獲得率を集計。
 データが取得できない場合は「（データなし）」と明記すること）
| 媒体 | 架電数 | アポ数 | 獲得率 |
|---|---|---|---|

## インターン別パフォーマンス
（メンバー別成果シートから、インターン1人ずつの業務時間・架電数・アポ数・獲得率を記載。
 データが取得できない場合は「（データなし）」と明記すること）
| 名前 | 業務時間 | 架電数 | 社長アポ | 担当アポ | アポ率 |
|---|---|---|---|---|---|

## 業種別商談成功パターン
（商談フィードバックDBや商談分析ツールから、業種別のhit/miss/objection傾向を分析。
 データが取得できない場合は「（データなし）」と明記すること）

## インターンへの今週のアクション指示
（「誰が・何を・いつまでに・どのくらい」の形式で3〜5件）
1.
2.
3.

## 先週フィードバックの反映
（先週のSlack返信があれば、それに対する対応・回答を記載。なければこのセクションは省略）

## 来週注目すべきポイント
（次週に確認すべき数値・判断ポイントを2〜3点）

---

## 注意事項
- 最新の議事録の意思決定を必ず反映すること
- 数値の根拠がない場合は断定しないこと
- インターン指示は「頑張る」「積極的に」等の曖昧な表現を使わないこと
- フォーマット外の余分な文章は出力しないこと
- 「先週フィードバックの反映」はフィードバックがない場合は省略すること
- データ取得に失敗したセクションは「（取得失敗）」と明記し、残りのデータでレポートを完成させること
"""

    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=3500,
        messages=[{"role": "user", "content": prompt}],
    )

    return message.content[0].text


# ============================================================
# ファイル保存
# ============================================================

def save_report(report: str) -> str:
    output_dir = os.path.join(SCRIPT_DIR, "reports")
    os.makedirs(output_dir, exist_ok=True)

    filename = f"weekly_report_{datetime.now().strftime('%Y%m%d')}.md"
    filepath = os.path.join(output_dir, filename)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(report)

    return filepath


# ============================================================
# Slack投稿
# ============================================================

def markdown_to_slack(text: str) -> str:
    lines = text.split("\n")
    converted = []

    for line in lines:
        if line.startswith("### "):
            line = f"*{line[4:]}*"
        elif line.startswith("## "):
            line = f"*{line[3:]}*"
        elif line.startswith("# "):
            line = f"*{line[2:]}*"
        while "**" in line:
            line = line.replace("**", "*", 1).replace("**", "*", 1)
        if line.strip() == "---":
            line = ""
        converted.append(line)

    return "\n".join(converted)


def _split_into_chunks(text: str, max_chars: int = 3000) -> list[str]:
    sections = text.split("\n\n")
    chunks: list[str] = []
    current = ""

    for section in sections:
        candidate = (current + "\n\n" + section).strip()
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            if len(section) > max_chars:
                for line in section.split("\n"):
                    if len(current) + len(line) + 1 > max_chars:
                        chunks.append(current)
                        current = line
                    else:
                        current = (current + "\n" + line).strip()
            else:
                current = section

    if current:
        chunks.append(current)

    return chunks


def post_to_slack(report: str, slack_token: str) -> str:
    """
    Slackに投稿してthread_tsを返す。
    thread_tsは次週のフィードバック取得に使用する。
    """
    headers = {
        "Authorization": f"Bearer {slack_token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    api_url = "https://slack.com/api/chat.postMessage"

    slack_text = markdown_to_slack(report)
    chunks = _split_into_chunks(slack_text, max_chars=3000)

    week_label = get_week_label()
    first_text = (
        f":bar_chart: <@{SLACK_MENTION_UID}> "
        f"週次営業レポート（{week_label}）が生成されました！\n"
        f"_このスレッドに返信すると来週のレポートに反映されます_\n\n"
        f"{chunks[0]}"
    )

    payload = {"channel": SLACK_CHANNEL_ID, "text": first_text, "mrkdwn": True}
    response = requests.post(api_url, headers=headers, json=payload)
    response.raise_for_status()
    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(f"Slack投稿に失敗しました：{result.get('error')}")

    thread_ts = result["ts"]

    for chunk in chunks[1:]:
        payload = {
            "channel": SLACK_CHANNEL_ID,
            "text": chunk,
            "thread_ts": thread_ts,
            "mrkdwn": True,
        }
        response = requests.post(api_url, headers=headers, json=payload)
        response.raise_for_status()
        res = response.json()
        if not res.get("ok"):
            raise RuntimeError(f"Slackスレッド投稿に失敗しました：{res.get('error')}")

    return thread_ts


# ============================================================
# エントリーポイント
# ============================================================

def main():
    # --- 環境変数チェック ---
    anthropic_api_key       = os.environ.get("ANTHROPIC_API_KEY")
    notion_api_key          = os.environ.get("NOTION_API_KEY")
    slack_bot_token         = os.environ.get("SLACK_BOT_TOKEN")
    # Google Sheets: サービスアカウントJSON（文字列 or ファイルパス）
    google_sa_json          = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    # HubSpot
    hubspot_token           = os.environ.get("HUBSPOT_TOKEN", "")

    missing = []
    if not anthropic_api_key:
        missing.append("ANTHROPIC_API_KEY")
    if not notion_api_key:
        missing.append("NOTION_API_KEY")
    if not slack_bot_token:
        missing.append("SLACK_BOT_TOKEN")

    if missing:
        print(f"エラー：以下の環境変数が設定されていません：{', '.join(missing)}")
        sys.exit(1)

    # オプション環境変数の状態を表示
    if not google_sa_json:
        print("  ⚠ GOOGLE_SERVICE_ACCOUNT_JSON 未設定（Google Sheetsはスキップ）")
    if not hubspot_token:
        print("  ⚠ HUBSPOT_TOKEN 未設定（HubSpotはスキップ）")

    # --- Step 1: Notionからデータ取得 ---
    print("【Step 1/8】Notionから議事録を取得中...")
    try:
        minutes_content = fetch_notion_page_as_text(MINUTES_PAGE_ID, notion_api_key)
        print("  ✓ 営業議事録（2025/11〜）取得完了")
    except requests.HTTPError as e:
        print(f"  エラー：議事録の取得に失敗しました。({e})")
        sys.exit(1)

    print("【Step 1/8】Notionから営業戦略を取得中...")
    try:
        strategy_content = fetch_notion_page_as_text(STRATEGY_PAGE_ID, notion_api_key)
        print("  ✓ 2026年度営業戦略全体像取得完了")
    except requests.HTTPError as e:
        print(f"  エラー：営業戦略の取得に失敗しました。({e})")
        sys.exit(1)

    # --- Step 2: Google Sheets KPI取得（独立・失敗しても続行） ---
    print("【Step 2/8】Google Sheets KPI実数値を取得中（サービスアカウント認証）...")
    kpi_data = ""
    if google_sa_json:
        kpi_data = fetch_spreadsheet_kpi(google_sa_json)
        if "エラー" in kpi_data or "スキップ" in kpi_data:
            print(f"  ⚠ {kpi_data[:100]}")
            kpi_data = ""
        else:
            print("  ✓ KPIデータ取得完了")
    else:
        print("  ⚠ スキップ（GOOGLE_SERVICE_ACCOUNT_JSON未設定）")

    # --- Step 3: 商談フィードバックDB取得（独立・失敗しても続行） ---
    print("【Step 3/8】商談フィードバックDB（Sheets）を取得中...")
    feedback_sheet_data = ""
    if google_sa_json:
        feedback_sheet_data = fetch_feedback_sheet(google_sa_json)
        if "取得失敗" in feedback_sheet_data or "データなし" in feedback_sheet_data:
            print(f"  ⚠ {feedback_sheet_data[:100]}")
        else:
            print("  ✓ 商談フィードバックDB取得完了")
    else:
        feedback_sheet_data = "（取得失敗：GOOGLE_SERVICE_ACCOUNT_JSON未設定）"
        print("  ⚠ スキップ（GOOGLE_SERVICE_ACCOUNT_JSON未設定）")

    # --- Step 4: HubSpotデータ取得（独立・失敗しても続行） ---
    print("【Step 4/8】HubSpotから今月の営業活動データを取得中...")
    hubspot_data = ""
    if hubspot_token:
        hubspot_data = fetch_hubspot_data(hubspot_token)
        if "取得失敗" in hubspot_data or "未設定" in hubspot_data:
            print(f"  ⚠ {hubspot_data[:100]}")
        else:
            print("  ✓ HubSpotデータ取得完了")
    else:
        hubspot_data = "（取得失敗：HUBSPOT_TOKEN未設定）"
        print("  ⚠ スキップ（HUBSPOT_TOKEN未設定）")

    # --- Step 4b: HubSpotシグナル別集計（独立・失敗しても続行） ---
    print("【Step 4b】HubSpotからシグナル別アポ獲得率を集計中...")
    signal_stats_text = ""
    if hubspot_token:
        signal_stats_text = get_hubspot_signal_stats(hubspot_token)
        if "取得失敗" in signal_stats_text:
            print(f"  ⚠ {signal_stats_text[:100]}")
            signal_stats_text = ""
        else:
            print("  ✓ シグナル別集計完了")
    else:
        print("  ⚠ スキップ（HUBSPOT_TOKEN未設定）")

    # --- Step 5: Slackフィードバック取得 ---
    print("【Step 5/8】先週のSlackフィードバックを取得中...")
    slack_feedback = fetch_slack_feedback(slack_bot_token)
    if slack_feedback:
        print("  ✓ フィードバック取得完了")
    else:
        print("  ✓ フィードバックなし（初回 or 返信なし）")

    # --- Step 6: 商談分析ツール（localhost:8006）からデータ取得 ---
    print("【Step 6/8】Well Body商談分析ツール（localhost:8006）から商談記録を取得中...")
    shodan_data = fetch_shodan_analysis()
    if shodan_data and "エラー" not in shodan_data:
        line1 = shodan_data.split("\n")[1] if len(shodan_data.split("\n")) > 1 else ""
        print(f"  ✓ 商談記録取得完了（{line1}）")
    elif not shodan_data:
        print("  ✓ 商談記録なし（ツール未起動 or データ0件）スキップ")
    else:
        print(f"  ⚠ {shodan_data}")
        shodan_data = ""

    # --- Step 7: Claude APIでレポート生成 ---
    print("【Step 7/8】Claude APIでレポートを生成中...")
    client = Anthropic(api_key=anthropic_api_key)
    report = generate_report(
        minutes_content,
        strategy_content,
        kpi_data,
        slack_feedback,
        client,
        shodan_data=shodan_data,
        feedback_sheet_data=feedback_sheet_data,
        hubspot_data=hubspot_data,
    )
    print("  ✓ レポート生成完了")

    # シグナル別集計をレポート末尾に追記
    if signal_stats_text:
        report = report + "\n\n---\n\n" + signal_stats_text

    # --- Step 8: ファイル保存 ---
    print("【Step 8/8】レポートをファイルに保存中...")
    filepath = save_report(report)
    print(f"  ✓ 保存完了：{filepath}")

    # --- Slack投稿 ---
    print("Slackの #営業 チャンネルに投稿中...")
    try:
        thread_ts = post_to_slack(report, slack_bot_token)
        save_last_post_meta(thread_ts, SLACK_CHANNEL_ID)
        print(f"  ✓ Slack投稿完了・スレッドTSを保存（次週フィードバック取得に使用）")
    except (requests.HTTPError, RuntimeError) as e:
        print(f"  ⚠ Slack投稿に失敗しました（レポートファイルは保存済み）：{e}")

    print("\n" + "=" * 60)
    print(report)
    print("=" * 60)


if __name__ == "__main__":
    main()
