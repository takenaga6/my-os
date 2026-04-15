"""
Well Body 商談準備ツール — バックエンドサーバー
FastAPI で動作。フロントエンド（index.html）からのリクエストを受け取り、
スクレイピング → Web検索 → Claude API の順に処理して結果を返す。
Whisper による音声文字起こし・商談フィードバック生成機能も含む。
ナレッジDB（JSON）による商談記録の保存・閲覧機能も含む。
"""

import os
import json
import uuid
import logging
import datetime
import requests
from pathlib import Path
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
import anthropic
import openai
import io
from dotenv import load_dotenv

# PDF読み込み（pypdf）
try:
    from pypdf import PdfReader
    _HAS_PYPDF = True
except ImportError:
    _HAS_PYPDF = False

# Google Sheets連携（gspread）
try:
    import gspread
    from google.oauth2.service_account import Credentials
    _HAS_GSPREAD = True
except ImportError:
    _HAS_GSPREAD = False

# .envファイルを読み込む
load_dotenv()

# ── ログ設定 ──
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ── 環境変数 ──
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY     = os.environ.get("OPENAI_API_KEY", "")
GOOGLE_CSE_API_KEY = os.environ.get("GOOGLE_CSE_API_KEY", "")
GOOGLE_CSE_CX      = os.environ.get("GOOGLE_CSE_CX", "")
HUBSPOT_TOKEN      = os.environ.get("HUBSPOT_TOKEN", "")
NOTION_API_KEY     = os.environ.get("NOTION_API_KEY", "")
NOTION_DB_ID       = os.environ.get("NOTION_DB_ID", "3432e3257cf480a7bd97e8d6af5c4553")

# ── ナレッジDBのファイルパス ──
# 商談記録をこのJSONファイルに保存する（DB不要・シンプル）
KNOWLEDGE_DB_PATH = Path("G:/マイドライブ/well-body-shodan/knowledge_db.json")

# ── Google Sheets設定 ──
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")  # サービスアカウントJSONファイルのパス
GOOGLE_SHEETS_ID            = os.environ.get("GOOGLE_SHEETS_ID", "")             # スプレッドシートのID

# ── FastAPIアプリ初期化 ──
app = FastAPI(title="Well Body 商談準備ツール")

# CORSの許可（同じドメインからのアクセスを許可）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# index.html を / で配信する
app.mount("/static", StaticFiles(directory="."), name="static")

@app.get("/")
def root():
    return FileResponse("index.html")


# ════════════════════════════════════════
# 型定義
# ════════════════════════════════════════

class GenerateRequest(BaseModel):
    company_name:   str        # 企業名
    company_url:    str        # 企業URL
    president_name: str = ""   # 社長名（任意）
    apo_info:       str = ""   # アポ取得時の情報（任意）

class GenerateResponse(BaseModel):
    research: str   # ① 企業リサーチサマリー
    talk:     str   # ② キラートーク
    material: str   # ③ 商談資料カスタマイズ指示
    flyer:    str   # ④ チラシ骨子

class SaveKnowledgeRequest(BaseModel):
    company_name:    str = ""        # 企業名
    industry:        str = ""        # 業種
    employee_count:  str = ""        # 従業員数
    meeting_date:    str = ""        # 商談日（YYYY-MM-DD）
    result:          str = ""        # 結果：win / hold / loss
    apo_route:       str = ""        # アポ獲得経路
    contact_title:   str = ""        # 担当者役職
    memo:            str = ""        # 補足メモ
    transcript:      str = ""        # 商談文字起こし（Whisperの結果）
    feedback:        str = ""        # AIフィードバックテキスト
    # 音声分析から抽出される項目
    meeting_minutes: int = 0         # 商談時間（分）
    flow_stage:      int = 0         # フロー到達ステージ（1〜4）
    cases_used:      str = ""        # 使った事例名
    loss_category:   str = ""        # 失注理由カテゴリ
    score:           int = 0         # 商談スコア（0〜100）
    temperature:     str = ""        # 先方温度感（高/中/低）
    next_action:     str = ""        # 次回アクション内容
    total_utterances: int = 0        # 総発言数（Hit率計算用）
    hits:            list[dict] = [] # 刺さった発言リスト
    misses:          list[dict] = [] # 失注シグナルリスト
    objections:      list[dict] = [] # 反論リスト
    hit_categories:      list[str] = []
    loss_signals:        list[str] = []
    objection_categories: list[str] = []


class UpdateResultRequest(BaseModel):
    result: str  # "win" または "loss"


# ════════════════════════════════════════
# ① 企業URLのスクレイピング
# ════════════════════════════════════════

def scrape_company_site(url: str) -> str:
    """
    企業の公式サイトからテキストを取得する。
    取得できない場合は空文字を返す（エラーにはしない）。
    """
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; WellBodyBot/1.0)"}
        resp = requests.get(url, headers=headers, timeout=10)
        resp.encoding = resp.apparent_encoding
        soup = BeautifulSoup(resp.text, "html.parser")

        # スクリプト・スタイルタグは除去してテキストだけ取る
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()

        text = soup.get_text(separator="\n", strip=True)

        # 長すぎる場合は先頭3000文字だけ使う（Claudeへのトークン節約）
        return text[:3000]
    except Exception as e:
        logger.warning(f"スクレイピング失敗 ({url}): {e}")
        return ""


# ════════════════════════════════════════
# ② Google CSEでメディア記事を検索
# ════════════════════════════════════════

def search_company_news(company_name: str, president_name: str) -> list[dict]:
    """
    Google Custom Search APIで企業名・社長名に関する記事を検索する。
    結果は [{title, link, snippet}, ...] のリストで返す。
    """
    if not GOOGLE_CSE_API_KEY or not GOOGLE_CSE_CX:
        logger.warning("Google CSEキーが未設定のためWeb検索をスキップ")
        return []

    # 「企業名 社長名」または「企業名」で検索
    query = f"{company_name} {president_name}".strip() if president_name else company_name
    params = {
        "key": GOOGLE_CSE_API_KEY,
        "cx":  GOOGLE_CSE_CX,
        "q":   query,
        "num": 5,
        "lr":  "lang_ja",
    }

    try:
        resp = requests.get(
            "https://www.googleapis.com/customsearch/v1",
            params=params,
            timeout=10,
        )
        data = resp.json()
        items = data.get("items", [])
        return [
            {
                "title":   item.get("title", ""),
                "link":    item.get("link", ""),
                "snippet": item.get("snippet", ""),
            }
            for item in items
        ]
    except Exception as e:
        logger.warning(f"Google CSE検索失敗: {e}")
        return []


# ════════════════════════════════════════
# ③ Claude APIで商談準備資料を生成
# ════════════════════════════════════════

def build_prompt(
    company_name: str,
    company_url: str,
    site_text: str,
    news_articles: list[dict],
    apo_info: str,
) -> str:
    """
    Claudeに渡すプロンプトを組み立てる。
    収集した情報をまとめて4つの出力を依頼する。
    """
    news_text = "\n".join(
        f"・{a['title']}\n  {a['snippet']}\n  URL: {a['link']}"
        for a in news_articles
    ) if news_articles else "（取得なし）"

    site_section = site_text if site_text else "（取得できませんでした）"
    apo_section  = apo_info  if apo_info  else "（入力なし）"

    return f"""あなたはWell Bodyという企業向けストレッチサービスの営業担当者のアシスタントです。
以下の情報をもとに、商談準備資料を4つ生成してください。

=== 商談先企業 ===
企業名: {company_name}
URL: {company_url}

=== 企業サイトの内容 ===
{site_section}

=== メディア掲載・記事 ===
{news_text}

=== アポ取得時に得た情報 ===
{apo_section}

---

以下の4つのセクションを出力してください。

【重要】セクション見出しは必ず行頭に「① 」「② 」「③ 」「④ 」で始めてください。
セクション内では丸囲み数字（①②③④）を絶対に使わないでください。
代わりに(1)(2)(3)(4) や【A】【B】【C】などを使ってください。

① 企業リサーチサマリー
- 事業内容・従業員規模の推測
- 働き方の推測（デスクワーク率・現場作業・女性比率など）
- 健康経営・福利厚生の取り組み状況（サイトや記事から読み取れる範囲）
- 注目すべき記事・掲載情報（あれば）
- アポ情報と照合したポイント（先方が言っていた課題と企業情報の繋がり）

② キラートーク（業種別カスタマイズ）
商談フローの各ステップに合わせて以下を作成：
(1) 業種トーク（この企業の働き方に合わせた導入フレーズ）
(2) YES取りの文言（先方が「たしかに」と言いやすい問いかけ）
(3) 刺さりそうな導入事例（業種・規模・課題が近い事例を2〜3件想定で提案）
(4) 想定される反論と切り返し（2〜3パターン）

商談フロー参考：
STEP1. 不信の払拭：自己紹介＆取材
STEP2. 不信・不要の払拭：ストーリートーク
STEP3. 不適の払拭：ニーズ＆運用紹介
STEP4. クロージング：体験会のアクションを切る

③ 商談資料カスタマイズ指示（Well Body提案資料 全47スライド）
以下のスライド構成をもとに、この企業の業種・規模・課題に合わせた「見せ方」を具体的に指示してください。

【スライド一覧（参考）】
- P5:  フィジカルケア職種比較表（理学療法士が最高評価、他職種との差別化）
- P12: 整形外科学会エビデンス表（医学的根拠）
- P13: 10年後の身体グラフ（放置リスクの可視化）
- P15: 健康経営認定数の推移グラフ（市場トレンド）
- P16: 生産性研究エビデンス表（厚労省・Google・Harvard等）
- P17: WHO/厚労省警鐘スライド（放置すると1人75〜100万円/年の損失）
- P21: 導入実績ロゴ一覧（SBI・GMO・FamilyMart・Nike等 約60社）
- P22: KPI数値（満足度4.86/5.0、継続希望99.5%）
- P26: Offi-Stretch仕組み詳細（30分/枠フロー、オンライン対応）
- P30: 競合比較マトリクス（約100万円〜、高エンゲージメント）
- P36: 導入事例[ア] Social Interior（インテリア系）
- P37: 導入事例[イ] GMO（IT大手）
- P38: 導入事例[ウ] Renfro（製造・小売）
- P39: 導入事例[エ] LOTTE（食品・菓子）
- P40: 導入事例[オ] SBI・三井デザインテック（金融・不動産）
- P41: Well Bodyの強み3点
- P43: 導入フロー（トライアル→スモールスタート→プランご提案）
- P46: プラン案内・料金表

【出力内容】
(1) 商談フロー別おすすめスライド順（STEP1不信払拭→STEP2不要払拭→STEP3不適払拭→STEP4クロージング）
(2) この業種・規模に最も刺さる導入事例スライド（P36〜40から優先順位付けして理由も記載）
(3) 飛ばしてよいスライドとその理由
(4) 「このスライドを見せるときに添えるべき一言」を2〜3枚分、具体的なセリフで記載

④ 従業員向けチラシの骨子
- 業種・働き方に合わせたキャッチコピー案（3案）
- 強調すべきベネフィット（腰痛改善/生産性向上/メンタルケアなどから選択と理由）
- Canvaで編集する際の変更箇所の具体的指示

---
各セクションは実際の営業担当者がそのまま使えるレベルで具体的に書いてください。
「おそらく」「推測ですが」などの断り書きは最小限にして、
情報が不足している部分は「要確認」と記載してください。
"""


def generate_with_claude(prompt: str) -> str:
    """Claude APIを呼び出してテキストを生成する。"""
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY が未設定です")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


def split_sections(text: str) -> dict[str, str]:
    """Claudeの出力を①②③④のセクションに分割する。
    「行頭に現れるマーカー」だけをセクション境界とみなす。
    セクション内で①②③④が使われても誤分割しない。
    """
    import re
    labels  = ["research", "talk", "material", "flyer"]
    markers = ["①", "②", "③", "④"]
    result  = {label: "" for label in labels}

    # 行頭（先頭含む）にあるマーカーの位置を順番に取得
    positions = []  # (マーカー文字のテキスト内絶対位置)
    search_from = 0
    for marker in markers:
        # (?m) マルチラインモードで ^ = 各行の先頭
        m = re.search(r'(?m)^[^\S\r\n]*' + re.escape(marker), text[search_from:])
        if m is None:
            break
        # text全体での絶対位置
        abs_marker_pos = search_from + m.start() + len(m.group()) - 1  # マーカー文字の位置
        positions.append(abs_marker_pos)
        search_from = search_from + m.end()  # 次の検索はこのマーカーの後から

    for i, (label, marker_pos) in enumerate(zip(labels, positions)):
        # マーカー文字の次の文字から次セクション境界まで
        content_start = marker_pos + 1
        content_end   = positions[i + 1] if i + 1 < len(positions) else len(text)
        result[label] = text[content_start:content_end].strip()

    return result


# ════════════════════════════════════════
# A. Whisper 音声文字起こし + 商談フィードバック
# ════════════════════════════════════════

def transcribe_with_whisper(audio_bytes: bytes, filename: str) -> str:
    """
    OpenAI Whisper APIで音声を文字起こしする。
    audio_bytes: アップロードされた音声ファイルのバイトデータ
    filename:    元のファイル名（拡張子の判定に使う）
    """
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY が未設定です")

    client = openai.OpenAI(api_key=OPENAI_API_KEY)

    # Whisper APIはファイルオブジェクトを受け取る
    # (ファイル名, バイトデータ, MIMEタイプ) のタプル形式で渡す
    audio_file = (filename, audio_bytes, "audio/mpeg")

    response = client.audio.transcriptions.create(
        model="whisper-1",
        file=audio_file,
        language="ja",   # 日本語指定で精度向上
    )
    return response.text


def build_feedback_prompt(
    transcript: str,
    memo: str,
    result: str,
    manual_fields: dict | None = None,
) -> str:
    """
    商談フィードバック用のClaudeプロンプト。
    JSON形式で返却させる。
    manual_fields: UIで手入力された補助情報（industry, employee_count, etc.）
    """
    result_label = {
        "win":  "成約（体験会確定）",
        "hold": "保留・検討中",
        "loss": "失注",
    }.get(result, "不明")

    memo_section = memo if memo else "（なし）"

    # 手入力フィールドをコンテキストとして組み込む
    mf = manual_fields or {}
    manual_section = ""
    if any(mf.values()):
        rows = []
        if mf.get("industry"):
            rows.append(f"業種: {mf['industry']}")
        if mf.get("employee_count"):
            rows.append(f"従業員数: {mf['employee_count']}")
        if mf.get("apo_route"):
            rows.append(f"アポ獲得経路: {mf['apo_route']}")
        if mf.get("contact_title"):
            rows.append(f"担当者役職: {mf['contact_title']}")
        if mf.get("meeting_minutes"):
            rows.append(f"商談時間（手入力）: {mf['meeting_minutes']}分")
        if mf.get("flow_stage"):
            rows.append(f"フロー到達ステージ（手入力）: STEP{mf['flow_stage']}")
        if mf.get("cases_used"):
            rows.append(f"使った事例（手入力）: {mf['cases_used']}")
        manual_section = "\n=== 商談メタ情報（手入力） ===\n" + "\n".join(rows)

    return f"""あなたはWell Bodyという企業向けストレッチサービスの営業コーチです。
以下の商談の文字起こしと結果を分析し、JSON形式のみで返してください。
それ以外のテキスト（前置き・説明文）は一切出力しないでください。

=== 商談結果 ===
{result_label}
{manual_section}
=== 補足メモ ===
{memo_section}

=== 商談の文字起こし ===
{transcript}

---

以下のJSONを返してください（キーは必ず英語、値は日本語でOK）：

{{
  "feedback": "商談フロー評価・刺さった発言・改善点をまとめた総合フィードバック（400字以内）",
  "meeting_minutes": 商談時間を分単位で推測（整数。手入力値があればそれを優先、不明なら0）,
  "flow_stage": Well Bodyの商談フロー（1:不信払拭 2:不要払拭 3:不適払拭 4:クロージング）で到達した最大ステージ番号（整数1〜4。手入力値があればそれを優先）,
  "cases_used": "商談中に言及した導入事例の会社名をカンマ区切り（例：Renfro,SBI）。手入力値があればそれを優先。なければ空文字",
  "loss_category": "失注の場合のカテゴリ（予算/タイミング/必要性/競合/決裁者不在/その他）。失注でなければ空文字",
  "score": 商談全体の質を0〜100で採点（整数）,
  "temperature": "先方の購買温度感（高/中/低）",
  "next_action": "次回やるべき具体的アクション（1〜2文）",
  "hit_categories": ["該当するものをすべて選択: 理学療法士の専門性 / メンタルケア / アスリート実績 / 業種別事例 / 健康経営への共感 / 取材での信頼構築"],
  "loss_signals": ["該当するものをすべて選択: 持ち帰り/社内確認 / 決裁者に届いていない / 過去マッサージ失敗 / 来期/時期が合わない / ROI未提示 / ブリッジ不足 / クロージング未着手 / 雑談に流れた / ネクストアクション未着手"],
  "objection_categories": ["該当するものをすべて選択: 費用感 / 公平性 / 時間的負担 / 必要性 / 運用負担 / スペース"],
  "total_utterances": 文字起こし中の先方発言数（整数。目安：句点や改行で区切って数える）,
  "hits": [
    {{"quote": "先方の前向きな発言を引用", "reason": "なぜ刺さったか1文"}}
  ],
  "misses": [
    {{"quote": "失注シグナルとなった発言を引用", "reason": "なぜ問題か1文"}}
  ],
  "objections": [
    {{"quote": "反論・懸念の発言を引用", "reason": "反論の本質", "reply": "次回の切り返し案"}}
  ]
}}
"""


# ════════════════════════════════════════
# B-2. 商談メモ（PDF/テキスト）のテキスト分析
# ════════════════════════════════════════

def extract_text_from_file(file_bytes: bytes, filename: str) -> str:
    """
    PDFまたはテキストファイルからテキストを抽出する。
    """
    filename_lower = filename.lower()
    if filename_lower.endswith(".pdf"):
        if not _HAS_PYPDF:
            raise HTTPException(
                status_code=500,
                detail="PDF読み込みに pypdf が必要です。pip install pypdf を実行してください。"
            )
        reader = PdfReader(io.BytesIO(file_bytes))
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n".join(pages)[:20000]   # 最大2万文字
    else:
        # テキストファイル（UTF-8 / Shift-JIS を自動判定）
        for encoding in ("utf-8", "shift_jis", "cp932"):
            try:
                return file_bytes.decode(encoding)[:20000]
            except UnicodeDecodeError:
                continue
        raise HTTPException(status_code=400, detail="テキストのデコードに失敗しました")


def build_memo_analysis_prompt(
    text: str,
    company_name: str,
    result: str,
    manual_fields: dict | None = None,
) -> str:
    """
    商談メモ（文字起こしテキスト）を分析するプロンプト。
    音声分析と同じJSON構造で返すことで、保存・表示ロジックを統一する。
    manual_fields: UIで手入力された補助情報
    """
    result_label = {
        "win":  "成約",
        "hold": "保留",
        "loss": "失注",
    }.get(result, "不明")

    # 手入力フィールドをコンテキストとして組み込む
    mf = manual_fields or {}
    manual_section = ""
    if any(mf.values()):
        rows = []
        if mf.get("industry"):
            rows.append(f"業種: {mf['industry']}")
        if mf.get("employee_count"):
            rows.append(f"従業員数: {mf['employee_count']}")
        if mf.get("apo_route"):
            rows.append(f"アポ獲得経路: {mf['apo_route']}")
        if mf.get("contact_title"):
            rows.append(f"担当者役職: {mf['contact_title']}")
        if mf.get("meeting_minutes"):
            rows.append(f"商談時間（手入力）: {mf['meeting_minutes']}分")
        if mf.get("flow_stage"):
            rows.append(f"フロー到達ステージ（手入力）: STEP{mf['flow_stage']}")
        if mf.get("cases_used"):
            rows.append(f"使った事例（手入力）: {mf['cases_used']}")
        manual_section = "\n=== 商談メタ情報（手入力） ===\n" + "\n".join(rows)

    return f"""あなたはWell Bodyという企業向けストレッチサービスの営業コーチです。
以下の商談メモ・文字起こしを分析し、JSON形式のみで出力してください。
前置き・説明文は一切出力しないでください。

=== 商談先 ===
{company_name}

=== 商談結果 ===
{result_label}
{manual_section}
=== 商談メモ・文字起こし ===
{text}

---

以下の形式でJSONのみを出力してください（キーは英語、値は日本語でOK）：

{{
  "feedback": "商談フロー評価・刺さった発言・改善点をまとめた総合フィードバック（400字以内）",
  "meeting_minutes": 商談時間を分単位で推測（整数。手入力値があればそれを優先。不明なら0）,
  "flow_stage": Well Bodyの商談フロー（1:不信払拭 2:不要払拭 3:不適払拭 4:クロージング）で到達した最大ステージ（整数1〜4。手入力値があればそれを優先）,
  "cases_used": "メモ中に言及した導入事例の会社名をカンマ区切り。手入力値があればそれを優先。なければ空文字",
  "loss_category": "失注の場合のカテゴリ（予算/タイミング/必要性/競合/決裁者不在/その他）。失注でなければ空文字",
  "score": 商談全体の質を0〜100で採点（整数）,
  "temperature": "先方の購買温度感（高/中/低）",
  "next_action": "次回やるべき具体的アクション（1〜2文）",
  "hit_categories": ["該当するものをすべて選択: 理学療法士の専門性 / メンタルケア / アスリート実績 / 業種別事例 / 健康経営への共感 / 取材での信頼構築"],
  "loss_signals": ["該当するものをすべて選択: 持ち帰り/社内確認 / 決裁者に届いていない / 過去マッサージ失敗 / 来期/時期が合わない / ROI未提示 / ブリッジ不足 / クロージング未着手 / 雑談に流れた / ネクストアクション未着手"],
  "objection_categories": ["該当するものをすべて選択: 費用感 / 公平性 / 時間的負担 / 必要性 / 運用負担 / スペース"],
  "total_utterances": テキスト中の先方発言数の推定（整数。不明なら0）,
  "hits": [
    {{"quote": "顧客の前向きな発言を引用", "reason": "なぜ刺さったか1文"}}
  ],
  "misses": [
    {{"quote": "失注シグナルとなった発言を引用", "reason": "なぜ問題か1文"}}
  ],
  "objections": [
    {{"quote": "反論・懸念の発言を引用", "reason": "反論の本質", "reply": "次回の切り返し案"}}
  ]
}}

分類の定義：
- hits: 顧客が前向き・同意・期待・興味を示した発言
- misses: 顧客が懸念・消極的・回避的な発言
- objections: 顧客がコスト・タイミング・必要性などで具体的に反論した発言

引用は原文のまま。各カテゴリ最大5件まで。
"""


# ════════════════════════════════════════
# B. ナレッジDB（JSON）
# ════════════════════════════════════════

def load_knowledge_db() -> list[dict]:
    """
    knowledge_db.json を読み込んで商談記録のリストを返す。
    ファイルがなければ空リストを返す。
    """
    if not KNOWLEDGE_DB_PATH.exists():
        return []
    try:
        with open(KNOWLEDGE_DB_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"ナレッジDB読み込み失敗: {e}")
        return []


def save_knowledge_db(records: list[dict]) -> None:
    """商談記録のリストをknowledge_db.jsonに書き込む。"""
    with open(KNOWLEDGE_DB_PATH, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def append_to_sheets(record: dict) -> None:
    """
    Googleスプレッドシートの末尾に1行追記する。
    .envに GOOGLE_SERVICE_ACCOUNT_JSON と GOOGLE_SHEETS_ID が設定されていない場合はスキップ。
    """
    if not _HAS_GSPREAD:
        return
    if not GOOGLE_SERVICE_ACCOUNT_JSON or not GOOGLE_SHEETS_ID:
        return

    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds  = Credentials.from_service_account_file(GOOGLE_SERVICE_ACCOUNT_JSON, scopes=scopes)
        client = gspread.authorize(creds)
        sheet  = client.open_by_key(GOOGLE_SHEETS_ID).sheet1

        # ヘッダーがなければ追加
        if sheet.row_count == 0 or sheet.cell(1, 1).value != "企業名":
            sheet.append_row([
                "企業名", "業種", "従業員数", "商談日", "結果",
                "アポ獲得経路", "担当者役職",
                "商談時間(分)", "フロー到達ステージ", "使った事例",
                "失注理由カテゴリ", "商談スコア", "先方温度感",
                "次回アクション",
                "Hit数", "Miss数", "Objection数",
                "総発言数", "Hit率(%)", "フロー完遂率(%)",
                "フィードバック", "保存日時",
            ])

        result_label = {"win": "成約", "hold": "保留", "loss": "失注"}.get(record.get("result", ""), record.get("result", ""))
        hit_count    = len(record.get("hits", []))
        miss_count   = len(record.get("misses", []))
        obj_count    = len(record.get("objections", []))
        total_utt    = record.get("total_utterances", 0)
        flow_stage   = record.get("flow_stage", 0)

        # Hit率（総発言数が0の場合は空欄）
        hit_rate     = round(hit_count / total_utt * 100, 1) if total_utt > 0 else ""
        # フロー完遂率
        flow_rate    = round(flow_stage / 4 * 100, 1) if flow_stage > 0 else ""

        sheet.append_row([
            record.get("company_name", ""),
            record.get("industry", ""),
            record.get("employee_count", ""),
            record.get("meeting_date", ""),
            result_label,
            record.get("apo_route", ""),
            record.get("contact_title", ""),
            record.get("meeting_minutes", ""),
            record.get("flow_stage", ""),
            record.get("cases_used", ""),
            record.get("loss_category", ""),
            record.get("score", ""),
            record.get("temperature", ""),
            record.get("next_action", ""),
            hit_count,
            miss_count,
            obj_count,
            total_utt,
            hit_rate,
            flow_rate,
            record.get("feedback", "")[:500],
            record.get("created_at", ""),
        ])
        logger.info(f"Googleスプレッドシートに追記: {record.get('company_name')}")
    except Exception as e:
        # Sheets書き込み失敗してもナレッジDB保存は止めない
        logger.warning(f"Googleスプレッドシート追記失敗（無視して続行）: {e}")


def push_to_hubspot(record: dict) -> None:
    """
    商談記録をHubSpotのカンパニーに書き込む。

    企業名でカンパニーを検索し、見つかったカンパニーに以下のプロパティを更新する:
      - shodan_score          : 商談スコア（数値）
      - shodan_date           : 商談日（日付）
      - shodan_result         : 商談結果（win/hold/loss）
      - shisshu_reason        : 失注理由カテゴリ
      - shisshu_reason_detail : 失注理由詳細（次回アクションを補足として格納）
      - taiken_status         : 体験会ステータス（temperature を格納）
      - next_action           : 次回アクション

    HUBSPOT_TOKEN 未設定・企業名なし・企業未発見の場合は静かにスキップ。
    失敗してもナレッジDB/Sheets の保存は継続する（例外を上げない）。
    """
    token = HUBSPOT_TOKEN
    if not token:
        logger.debug("HUBSPOT_TOKEN 未設定 → HubSpot書き込みをスキップ")
        return

    company_name = record.get("company_name", "")
    if not company_name:
        logger.debug("企業名なし → HubSpot書き込みをスキップ")
        return

    hs_headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }
    base_url = "https://api.hubapi.com"

    try:
        # ① 企業名でカンパニーを検索（完全一致）
        search_resp = requests.post(
            f"{base_url}/crm/v3/objects/companies/search",
            json={
                "filterGroups": [{"filters": [{
                    "propertyName": "name",
                    "operator":     "EQ",
                    "value":        company_name,
                }]}],
                "limit": 1,
            },
            headers=hs_headers,
            timeout=10,
        )
        if not search_resp.ok:
            logger.warning(
                f"HubSpot企業検索失敗: {search_resp.status_code} - {search_resp.text[:200]}"
            )
            return

        results = search_resp.json().get("results", [])
        if not results:
            logger.info(f"HubSpot: 企業が見つかりません → {company_name}")
            return

        company_id = results[0]["id"]

        # ② 書き込むプロパティを構築（値がある項目のみ）
        props: dict = {}
        if record.get("score"):
            props["shodan_score"] = str(record["score"])
        if record.get("meeting_date"):
            props["shodan_date"] = record["meeting_date"]
        if record.get("result"):
            props["shodan_result"] = record["result"]
        if record.get("loss_category"):
            props["shisshu_reason"] = record["loss_category"]
        if record.get("next_action"):
            # shisshu_reason_detail にも次回アクションを補足格納
            props["shisshu_reason_detail"] = record["next_action"]
            props["next_action"]           = record["next_action"]
        if record.get("temperature"):
            props["taiken_status"] = record["temperature"]

        if not props:
            logger.debug(f"HubSpot: 書き込むプロパティがありません → {company_name}")
            return

        # ③ カンパニーを PATCH で更新
        update_resp = requests.patch(
            f"{base_url}/crm/v3/objects/companies/{company_id}",
            json={"properties": props},
            headers=hs_headers,
            timeout=10,
        )
        if update_resp.ok:
            logger.info(
                f"HubSpot書き込み成功: {company_name} "
                f"(ID:{company_id}) props={list(props.keys())}"
            )
        else:
            logger.warning(
                f"HubSpot更新失敗: {update_resp.status_code} - {update_resp.text[:200]}"
            )

    except Exception as e:
        # HubSpot書き込み失敗してもナレッジDB/Sheets保存は止めない
        logger.warning(f"HubSpot書き込み例外（無視して続行）: {e}")


def push_to_notion(record: dict) -> None:
    """
    商談記録をNotionの商談ナレッジDBにページとして追加する。
    プロパティ＋ページ本文（Hits/Misses/Objections詳細）を書き込む。
    失敗してもナレッジDB/Sheets保存は止めない。
    """
    if not NOTION_API_KEY or not NOTION_DB_ID:
        logger.debug("NOTION_API_KEY or NOTION_DB_ID 未設定 → Notion書き込みスキップ")
        return

    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }

    try:
        result_label = {"win": "成約", "hold": "保留", "loss": "失注"}.get(
            record.get("result", ""), record.get("result", "")
        )

        stage_map = {
            1: "STEP1:不信の払拭",
            2: "STEP2:不要の払拭",
            3: "STEP3:不適の払拭",
            4: "STEP4:クロージング",
        }
        flow_stage = record.get("flow_stage", 0)
        stage_label = stage_map.get(flow_stage, "")
        temp = record.get("temperature", "")

        properties = {
            "企業名": {"title": [{"text": {"content": record.get("company_name", "不明")}}]},
        }

        # 商談日（スラッシュ区切りをISO 8601に変換）
        meeting_date = record.get("meeting_date", "")
        if meeting_date:
            try:
                parts = meeting_date.replace("/", "-").split("-")
                meeting_date = f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
            except (ValueError, IndexError):
                pass
            properties["商談日"] = {"date": {"start": meeting_date}}

        if result_label:
            properties["結果"] = {"select": {"name": result_label}}

        score = record.get("score", 0)
        if score:
            properties["商談スコア"] = {"number": score}

        if temp:
            properties["先方温度感"] = {"select": {"name": temp}}

        if stage_label:
            properties["フロー到達ステージ"] = {"select": {"name": stage_label}}

        industry = record.get("industry", "")
        if industry:
            properties["業種"] = {"select": {"name": industry}}

        emp = record.get("employee_count", "")
        if emp:
            try:
                properties["従業員数"] = {"number": int(emp)}
            except (ValueError, TypeError):
                pass

        apo_route = record.get("apo_route", "")
        if apo_route:
            properties["アポ獲得経路"] = {"select": {"name": apo_route}}

        contact_title = record.get("contact_title", "")
        if contact_title:
            properties["担当者役職"] = {"rich_text": [{"text": {"content": contact_title}}]}

        minutes = record.get("meeting_minutes", 0)
        if minutes:
            properties["商談時間(分)"] = {"number": minutes}

        cases = record.get("cases_used", "")
        if cases:
            case_list = [c.strip() for c in cases.split(",") if c.strip()]
            properties["使った事例"] = {"multi_select": [{"name": c} for c in case_list]}

        loss_cat = record.get("loss_category", "")
        if loss_cat:
            properties["失注理由カテゴリ"] = {"select": {"name": loss_cat}}

        next_action = record.get("next_action", "")
        if next_action:
            properties["次回アクション"] = {"rich_text": [{"text": {"content": next_action[:2000]}}]}

        feedback = record.get("feedback", "")
        if feedback:
            properties["フィードバック"] = {"rich_text": [{"text": {"content": feedback[:2000]}}]}

        hits = record.get("hits", [])
        misses = record.get("misses", [])
        objections = record.get("objections", [])
        total_utt = record.get("total_utterances", 0)

        hit_count = len(hits)
        miss_count = len(misses)
        obj_count = len(objections)

        if hit_count:
            properties["Hit数"] = {"number": hit_count}
        if miss_count:
            properties["Miss数"] = {"number": miss_count}
        if obj_count:
            properties["Objection数"] = {"number": obj_count}
        if total_utt:
            properties["総発言数"] = {"number": total_utt}

        if total_utt > 0 and hit_count > 0:
            properties["Hit率(%)"] = {"number": round(hit_count / total_utt * 100, 1)}

        if flow_stage > 0:
            properties["フロー完遂率(%)"] = {"number": round(flow_stage / 4 * 100, 1)}

        created_at = record.get("created_at", "")
        if created_at:
            properties["保存日時"] = {"date": {"start": created_at[:10]}}

        # Hitカテゴリ
        hit_cats = record.get("hit_categories", [])
        if hit_cats:
            properties["Hitカテゴリ"] = {"multi_select": [{"name": c} for c in hit_cats]}

        # 失注シグナル
        loss_sigs = record.get("loss_signals", [])
        if loss_sigs:
            properties["失注シグナル"] = {"multi_select": [{"name": c} for c in loss_sigs]}

        # Objectionカテゴリ
        obj_cats = record.get("objection_categories", [])
        if obj_cats:
            properties["Objectionカテゴリ"] = {"multi_select": [{"name": c} for c in obj_cats]}

        children = []

        if feedback:
            children.append({
                "object": "block",
                "type": "heading_2",
                "heading_2": {"rich_text": [{"text": {"content": "フィードバック"}}]},
            })
            for i in range(0, len(feedback), 2000):
                children.append({
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{"text": {"content": feedback[i:i+2000]}}]},
                })

        if hits:
            children.append({
                "object": "block",
                "type": "heading_2",
                "heading_2": {"rich_text": [{"text": {"content": "Hits（刺さった発言）"}}]},
            })
            for h in hits:
                quote = h.get("quote", "")
                reason = h.get("reason", "")
                children.append({
                    "object": "block",
                    "type": "quote",
                    "quote": {"rich_text": [{"text": {"content": quote[:2000]}}]},
                })
                if reason:
                    children.append({
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {"rich_text": [{"text": {"content": f"→ {reason}"[:2000]}}]},
                    })

        if misses:
            children.append({
                "object": "block",
                "type": "heading_2",
                "heading_2": {"rich_text": [{"text": {"content": "Misses（失注シグナル）"}}]},
            })
            for m in misses:
                quote = m.get("quote", "")
                reason = m.get("reason", "")
                children.append({
                    "object": "block",
                    "type": "quote",
                    "quote": {"rich_text": [{"text": {"content": quote[:2000]}}]},
                })
                if reason:
                    children.append({
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {"rich_text": [{"text": {"content": f"→ {reason}"[:2000]}}]},
                    })

        if objections:
            children.append({
                "object": "block",
                "type": "heading_2",
                "heading_2": {"rich_text": [{"text": {"content": "Objections（反論・懸念）"}}]},
            })
            for o in objections:
                quote = o.get("quote", "")
                reason = o.get("reason", "")
                reply = o.get("reply", "")
                children.append({
                    "object": "block",
                    "type": "quote",
                    "quote": {"rich_text": [{"text": {"content": quote[:2000]}}]},
                })
                detail = ""
                if reason:
                    detail += f"本質: {reason}"
                if reply:
                    detail += f"\n切り返し案: {reply}"
                if detail:
                    children.append({
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {"rich_text": [{"text": {"content": detail[:2000]}}]},
                    })

        payload = {
            "parent": {"database_id": NOTION_DB_ID},
            "properties": properties,
        }
        if children:
            payload["children"] = children[:100]

        resp = requests.post(
            "https://api.notion.com/v1/pages",
            json=payload,
            headers=headers,
            timeout=30,
        )
        if resp.ok:
            page_id = resp.json().get("id", "")
            logger.info(f"Notion書き込み成功: {record.get('company_name')} (ID:{page_id})")
        else:
            logger.warning(f"Notion書き込み失敗: {resp.status_code} - {resp.text[:300]}")

    except Exception as e:
        logger.warning(f"Notion書き込み例外（無視して続行）: {e}")


def update_sheet_result(record: dict, new_result: str) -> None:
    """
    Googleスプレッドシートの該当行の「結果」列を上書き更新する。
    企業名 + 商談日でマッチングし、最初に見つかった行を更新する。
    行の追加は行わない。
    """
    if not _HAS_GSPREAD:
        return
    if not GOOGLE_SERVICE_ACCOUNT_JSON or not GOOGLE_SHEETS_ID:
        return

    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds  = Credentials.from_service_account_file(GOOGLE_SERVICE_ACCOUNT_JSON, scopes=scopes)
        client = gspread.authorize(creds)
        sheet  = client.open_by_key(GOOGLE_SHEETS_ID).sheet1

        result_label = {"win": "成約", "hold": "保留", "loss": "失注"}.get(new_result, new_result)
        company_name = record.get("company_name", "")
        meeting_date = record.get("meeting_date", "")

        all_rows = sheet.get_all_values()
        updated  = False
        for i, row in enumerate(all_rows):
            if i == 0:
                continue  # ヘッダー行スキップ
            # 企業名（列1）と商談日（列4）で一致判定（1-indexed で行番号は i+1）
            if len(row) >= 4 and row[0] == company_name and row[3] == meeting_date:
                sheet.update_cell(i + 1, 5, result_label)  # 5列目=結果
                logger.info(f"Sheets result更新: {company_name} 行{i+1} → {result_label}")
                updated = True
                break  # 最初に一致した行だけ更新

        if not updated:
            logger.warning(f"Sheets: 該当行が見つかりません → {company_name} / {meeting_date}")

    except Exception as e:
        logger.warning(f"Sheets result更新失敗（無視して続行）: {e}")


def update_hubspot_result(record: dict, new_result: str) -> None:
    """
    HubSpotの該当カンパニーの shodan_result プロパティのみを更新する。
    企業名で検索し、見つかったカンパニーを PATCH する。
    """
    token = HUBSPOT_TOKEN
    if not token:
        return

    company_name = record.get("company_name", "")
    if not company_name:
        return

    hs_headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }
    base_url = "https://api.hubapi.com"

    try:
        # 企業名でカンパニーを検索
        search_resp = requests.post(
            f"{base_url}/crm/v3/objects/companies/search",
            json={
                "filterGroups": [{"filters": [{
                    "propertyName": "name",
                    "operator":     "EQ",
                    "value":        company_name,
                }]}],
                "limit": 1,
            },
            headers=hs_headers,
            timeout=10,
        )
        if not search_resp.ok:
            logger.warning(f"HubSpot企業検索失敗（result更新）: {search_resp.status_code}")
            return

        results = search_resp.json().get("results", [])
        if not results:
            logger.info(f"HubSpot: 企業が見つかりません（result更新）→ {company_name}")
            return

        company_id = results[0]["id"]

        # shodan_result のみを PATCH 更新
        update_resp = requests.patch(
            f"{base_url}/crm/v3/objects/companies/{company_id}",
            json={"properties": {"shodan_result": new_result}},
            headers=hs_headers,
            timeout=10,
        )
        if update_resp.ok:
            logger.info(f"HubSpot shodan_result更新: {company_name} → {new_result}")
        else:
            logger.warning(
                f"HubSpot shodan_result更新失敗: {update_resp.status_code} - {update_resp.text[:200]}"
            )

    except Exception as e:
        logger.warning(f"HubSpot shodan_result更新例外（無視して続行）: {e}")


# ════════════════════════════════════════
# APIエンドポイント
# ════════════════════════════════════════

# ── 商談準備資料の生成 ──
@app.post("/generate", response_model=GenerateResponse)
def generate(req: GenerateRequest):
    """企業情報を受け取り、①〜④の商談準備資料を返す。"""
    logger.info(f"生成リクエスト: {req.company_name} ({req.company_url})")

    site_text     = scrape_company_site(req.company_url)
    logger.info(f"スクレイピング完了: {len(site_text)}文字取得")

    news_articles = search_company_news(req.company_name, req.president_name)
    logger.info(f"Web検索完了: {len(news_articles)}件取得")

    prompt   = build_prompt(req.company_name, req.company_url, site_text, news_articles, req.apo_info)
    raw_text = generate_with_claude(prompt)
    logger.info("Claude API生成完了")

    sections = split_sections(raw_text)
    return GenerateResponse(
        research=sections["research"],
        talk=sections["talk"],
        material=sections["material"],
        flyer=sections["flyer"],
    )


# ── 音声文字起こし + フィードバック生成 ──
@app.post("/analyze-voice")
async def analyze_voice(
    audio:            UploadFile = File(...),
    result:           str        = Form(""),
    memo:             str        = Form(""),
    company_name:     str        = Form(""),
    industry:         str        = Form(""),
    employee_count:   str        = Form(""),
    meeting_date:     str        = Form(""),
    apo_route:        str        = Form(""),
    contact_title:    str        = Form(""),
    meeting_minutes:  str        = Form(""),   # 手入力（分）。空なら音声から推測
    flow_stage:       str        = Form(""),   # 手入力（1〜4）。空なら音声から推測
    cases_used:       str        = Form(""),   # 手入力。空なら音声から推測
):
    """
    音声ファイルをWhisperで文字起こしし、
    Claudeで商談フィードバック（JSON）を生成して返す。
    """
    import re as _re
    logger.info(f"音声分析リクエスト: {audio.filename} / 結果={result}")

    # ① 音声ファイルを読み込む
    audio_bytes = await audio.read()

    # ② Whisperで文字起こし
    try:
        transcript = transcribe_with_whisper(audio_bytes, audio.filename)
        logger.info(f"Whisper文字起こし完了: {len(transcript)}文字")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"文字起こし失敗: {str(e)}")

    # ③ Claudeで分析（JSON返却）
    manual_fields = {
        "industry":       industry,
        "employee_count": employee_count,
        "apo_route":      apo_route,
        "contact_title":  contact_title,
        "meeting_minutes": meeting_minutes,
        "flow_stage":     flow_stage,
        "cases_used":     cases_used,
    }
    try:
        prompt = build_feedback_prompt(transcript, memo, result, manual_fields)
        raw    = generate_with_claude(prompt)
        logger.info("フィードバック生成完了")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"フィードバック生成失敗: {str(e)}")

    # ④ JSONパース
    json_match = _re.search(r'\{.*\}', raw, _re.DOTALL)
    if not json_match:
        return JSONResponse({"transcript": transcript, "analysis": {"feedback": raw}})
    try:
        analysis = json.loads(json_match.group())
    except json.JSONDecodeError:
        return JSONResponse({"transcript": transcript, "analysis": {"feedback": raw}})

    return JSONResponse({
        "transcript":          transcript,
        "analysis":            analysis,
        "feedback":            analysis.get("feedback", ""),   # 後方互換用
        # カテゴリ（フロントからの保存リクエストで使用）
        "hit_categories":      analysis.get("hit_categories", []),
        "loss_signals":        analysis.get("loss_signals", []),
        "objection_categories": analysis.get("objection_categories", []),
        # 手入力フィールドをそのままエコーバック（フロントで保存時に使用）
        "company_name":        company_name,
        "industry":            industry,
        "employee_count":      employee_count,
        "meeting_date":        meeting_date,
        "result":              result,
        "apo_route":           apo_route,
        "contact_title":       contact_title,
    })


# ── 商談メモ（PDF/テキスト）分析 ──
@app.post("/analyze-memo")
async def analyze_memo(
    memo_file:        UploadFile = File(...),
    company_name:     str        = Form(""),
    result:           str        = Form("hold"),
    industry:         str        = Form(""),
    employee_count:   str        = Form(""),
    meeting_date:     str        = Form(""),
    apo_route:        str        = Form(""),
    contact_title:    str        = Form(""),
    meeting_minutes:  str        = Form(""),
    flow_stage:       str        = Form(""),
    cases_used:       str        = Form(""),
):
    """
    PDF/テキストの商談メモをアップロードし、
    Claude で hit/miss/objection を分析して返す（音声分析と同じJSON構造）。
    """
    logger.info(f"メモ分析リクエスト: {memo_file.filename} / {company_name} / {result}")

    # ① ファイル読み込み・テキスト抽出
    file_bytes = await memo_file.read()
    try:
        text = extract_text_from_file(file_bytes, memo_file.filename)
        logger.info(f"テキスト抽出完了: {len(text)}文字")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"ファイル読み込み失敗: {str(e)}")

    # ② Claude で分析（音声分析と同じJSON構造）
    manual_fields = {
        "industry":       industry,
        "employee_count": employee_count,
        "apo_route":      apo_route,
        "contact_title":  contact_title,
        "meeting_minutes": meeting_minutes,
        "flow_stage":     flow_stage,
        "cases_used":     cases_used,
    }
    try:
        prompt = build_memo_analysis_prompt(text, company_name, result, manual_fields)
        raw    = generate_with_claude(prompt)
        logger.info("メモ分析完了")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"分析失敗: {str(e)}")

    # ③ JSON パース
    import re as _re
    json_match = _re.search(r'\{.*\}', raw, _re.DOTALL)
    if not json_match:
        raise HTTPException(status_code=500, detail="JSONの抽出に失敗しました")
    try:
        analysis = json.loads(json_match.group())
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"JSON解析失敗: {str(e)}")

    logger.info(f"分析結果カテゴリ - hit_categories: {analysis.get('hit_categories', [])}, loss_signals: {analysis.get('loss_signals', [])}, objection_categories: {analysis.get('objection_categories', [])}")

    return JSONResponse({
        "text":                text,
        "analysis":            analysis,
        # カテゴリ（フロントからの保存リクエストで使用）
        "hit_categories":      analysis.get("hit_categories", []),
        "loss_signals":        analysis.get("loss_signals", []),
        "objection_categories": analysis.get("objection_categories", []),
        # 手入力フィールドをエコーバック（フロントで保存時に使用）
        "company_name":        company_name,
        "industry":            industry,
        "employee_count":      employee_count,
        "meeting_date":        meeting_date,
        "result":              result,
        "apo_route":           apo_route,
        "contact_title":       contact_title,
    })


# ── 商談記録の保存 ──
@app.post("/knowledge")
def save_knowledge(req: SaveKnowledgeRequest):
    """商談記録をknowledge_db.jsonに追加保存する。"""
    records = load_knowledge_db()

    # 新しいレコードを先頭に追加（新しい順に並ぶ）
    new_record = {
        "id":               str(uuid.uuid4()),
        "company_name":     req.company_name,
        "industry":         req.industry,
        "employee_count":   req.employee_count,
        "meeting_date":     req.meeting_date or datetime.date.today().isoformat(),
        "result":           req.result,
        "apo_route":        req.apo_route,
        "contact_title":    req.contact_title,
        "memo":             req.memo,
        "transcript":       req.transcript,
        "feedback":         req.feedback,
        "meeting_minutes":  req.meeting_minutes,
        "flow_stage":       req.flow_stage,
        "cases_used":       req.cases_used,
        "loss_category":    req.loss_category,
        "score":            req.score,
        "temperature":      req.temperature,
        "next_action":      req.next_action,
        "total_utterances": req.total_utterances,
        "hits":             req.hits,
        "misses":           req.misses,
        "objections":       req.objections,
        "hit_categories":      req.hit_categories,
        "loss_signals":        req.loss_signals,
        "objection_categories": req.objection_categories,
        "created_at":       datetime.datetime.now().isoformat(),
    }
    records.insert(0, new_record)
    save_knowledge_db(records)

    # Googleスプレッドシートにも自動追記（設定済みの場合のみ）
    append_to_sheets(new_record)

    # HubSpotにも商談プロパティを書き込む（失敗しても続行）
    push_to_hubspot(new_record)

    logger.info(f"カテゴリ確認 - hit_categories: {new_record.get('hit_categories', [])}, loss_signals: {new_record.get('loss_signals', [])}, objection_categories: {new_record.get('objection_categories', [])}")

    # Notionにも商談記録を書き込む（失敗しても続行）
    push_to_notion(new_record)

    logger.info(f"商談記録を保存: {req.company_name} / {req.result}")
    return JSONResponse({"status": "ok", "id": new_record["id"]})


# ── 商談記録の一覧取得 ──
@app.get("/knowledge")
def list_knowledge(result: str = ""):
    """
    保存済みの商談記録を返す。
    result パラメータ（success/hold/ng）でフィルタリング可能。
    """
    records = load_knowledge_db()

    # フィルタリング（指定があれば）
    if result:
        records = [r for r in records if r.get("result") == result]

    return JSONResponse(records)


# ── 商談記録の詳細取得 ──
@app.get("/knowledge/{record_id}")
def get_knowledge(record_id: str):
    """指定IDの商談記録を返す。"""
    records = load_knowledge_db()
    for r in records:
        if r.get("id") == record_id:
            return JSONResponse(r)
    raise HTTPException(status_code=404, detail="記録が見つかりません")


# ── 商談結果の更新（hold → win / loss）──
@app.patch("/knowledge/{record_id}/result")
def update_knowledge_result(record_id: str, req: UpdateResultRequest):
    """
    保留（hold）の商談記録を「契約（win）」または「失注（loss）」に更新する。

    更新対象：
    1. knowledge_db.json の result フィールド
    2. Googleスプレッドシートの該当行「結果」列（行追加なし）
    3. HubSpotカンパニーの shodan_result プロパティ

    Sheets・HubSpot のどちらかが失敗しても処理を継続する。
    """
    if req.result not in ("win", "loss"):
        raise HTTPException(status_code=400, detail="result は win または loss を指定してください")

    records = load_knowledge_db()
    target  = None
    for r in records:
        if r.get("id") == record_id:
            target = r
            break

    if target is None:
        raise HTTPException(status_code=404, detail="記録が見つかりません")

    old_result     = target.get("result", "")
    target["result"] = req.result

    # ① knowledge_db.json を更新
    save_knowledge_db(records)
    logger.info(f"result更新: {target.get('company_name')} {old_result} → {req.result}")

    # ② Googleスプレッドシートの該当行を更新（失敗しても続行）
    update_sheet_result(target, req.result)

    # ③ HubSpot の shodan_result を更新（失敗しても続行）
    update_hubspot_result(target, req.result)

    return JSONResponse({"status": "ok", "id": record_id, "result": req.result})


# ════════════════════════════════════════
# ローカル起動用
# ════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8006)
