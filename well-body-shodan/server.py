"""
Well Body 商談準備ツール — バックエンドサーバー
FastAPI で動作。フロントエンド（index.html）からのリクエストを受け取り、
スクレイピング → Web検索 → Claude API の順に処理して結果を返す。
"""

import os
import logging
import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import anthropic

# ── ログ設定 ──
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ── 環境変数 ──
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
GOOGLE_CSE_API_KEY = os.environ.get("GOOGLE_CSE_API_KEY", "")
GOOGLE_CSE_CX      = os.environ.get("GOOGLE_CSE_CX", "")

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


# ── リクエスト・レスポンスの型定義 ──

class GenerateRequest(BaseModel):
    company_name: str       # 企業名
    company_url:  str       # 企業URL
    president_name: str = ""  # 社長名（任意）
    apo_info: str = ""      # アポ取得時の情報（任意）

class GenerateResponse(BaseModel):
    research:  str   # ① 企業リサーチサマリー
    talk:      str   # ② キラートーク
    material:  str   # ③ 商談資料カスタマイズ指示
    flyer:     str   # ④ チラシ骨子


# ────────────────────────────────────────
# ① 企業URLのスクレイピング
# ────────────────────────────────────────

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


# ────────────────────────────────────────
# ② Google CSEでメディア記事を検索
# ────────────────────────────────────────

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
        "num": 5,  # 最大5件取得
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


# ────────────────────────────────────────
# ③ Claude APIで商談準備資料を生成
# ────────────────────────────────────────

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

    # メディア記事をテキスト化
    news_text = ""
    if news_articles:
        news_text = "\n".join(
            f"・{a['title']}\n  {a['snippet']}\n  URL: {a['link']}"
            for a in news_articles
        )
    else:
        news_text = "（取得なし）"

    # サイト本文
    site_section = site_text if site_text else "（取得できませんでした）"

    # アポ情報
    apo_section = apo_info if apo_info else "（入力なし）"

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

以下の4つを、それぞれ見出し（①②③④）付きで出力してください。

① 企業リサーチサマリー
- 事業内容・従業員規模の推測
- 働き方の推測（デスクワーク率・現場作業・女性比率など）
- 健康経営・福利厚生の取り組み状況（サイトや記事から読み取れる範囲）
- 注目すべき記事・掲載情報（あれば）
- アポ情報と照合したポイント（先方が言っていた課題と企業情報の繋がり）

② キラートーク（業種別カスタマイズ）
商談フローの各ステップに合わせて以下を作成：
1. 業種トーク（この企業の働き方に合わせた導入フレーズ）
2. YES取りの文言（先方が「たしかに」と言いやすい問いかけ）
3. 刺さりそうな導入事例（業種・規模・課題が近い事例を2〜3件想定で提案）
4. 想定される反論と切り返し（2〜3パターン）

商談フロー参考：
1. 不信の払拭：自己紹介＆取材
2. 不信・不要の払拭：ストーリートーク
3. 不適の払拭：ニーズ＆運用紹介
4. クロージング：体験会のアクションを切る

③ 商談資料カスタマイズ指示
- PPTスライドで優先して見せるべきページと理由
- 先方業種に合わせた事例スライドの順番
- 商談中に開くべきURLやデモの優先順位

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
    """
    Claude APIを呼び出してテキストを生成する。
    """
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY が未設定です")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-opus-4-6",   # 最新・最高精度モデルを使用
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


# ────────────────────────────────────────
# ④ 生成結果をセクションごとに分割
# ────────────────────────────────────────

def split_sections(text: str) -> dict[str, str]:
    """
    Claudeの出力を①②③④のセクションに分割する。
    """
    import re

    # ①②③④ の見出しで分割する
    pattern = r'[①②③④]'
    parts = re.split(pattern, text)

    # parts[0] は見出し前の余分なテキスト（無視）
    labels = ["research", "talk", "material", "flyer"]
    result = {label: "" for label in labels}

    for i, label in enumerate(labels):
        if i + 1 < len(parts):
            result[label] = parts[i + 1].strip()

    return result


# ────────────────────────────────────────
# APIエンドポイント
# ────────────────────────────────────────

@app.post("/generate", response_model=GenerateResponse)
def generate(req: GenerateRequest):
    """
    商談準備資料を生成するメインエンドポイント。
    フロントエンドから企業情報を受け取り、①〜④を返す。
    """
    logger.info(f"生成リクエスト: {req.company_name} ({req.company_url})")

    # ① 企業サイトをスクレイピング
    site_text = scrape_company_site(req.company_url)
    logger.info(f"スクレイピング完了: {len(site_text)}文字取得")

    # ② Google CSEでメディア記事を検索
    news_articles = search_company_news(req.company_name, req.president_name)
    logger.info(f"Web検索完了: {len(news_articles)}件取得")

    # ③ プロンプト組み立て → Claude APIで生成
    prompt = build_prompt(
        company_name=req.company_name,
        company_url=req.company_url,
        site_text=site_text,
        news_articles=news_articles,
        apo_info=req.apo_info,
    )
    raw_text = generate_with_claude(prompt)
    logger.info("Claude API生成完了")

    # ④ セクション分割
    sections = split_sections(raw_text)

    return GenerateResponse(
        research=sections["research"],
        talk=sections["talk"],
        material=sections["material"],
        flyer=sections["flyer"],
    )


# ────────────────────────────────────────
# ローカル起動用
# ────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
