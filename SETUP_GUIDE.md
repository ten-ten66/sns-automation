# SNS自動化・収益化システム 完全導入手順書

> **最終更新:** 2026年3月
> **システム規模:** 38ファイル / 6,854行 / Python 3.11+

---

## 目次

1. [加入が必要なサービス](#1-加入が必要なサービス)
2. [ダウンロード・インストールするもの](#2-ダウンロードインストールするもの)
3. [事前に準備するもの](#3-事前に準備するもの)
4. [導入手順（ステップバイステップ）](#4-導入手順ステップバイステップ)
5. [費用見積もり](#5-費用見積もり月額)
6. [収益目標](#6-収益目標目安)
7. [システム構成図](#7-システム構成図)
8. [トラブルシューティング](#8-トラブルシューティング)

---

## 1. 加入が必要なサービス

### 必須（AI系）

| サービス | 用途 | 費用 | 登録URL |
|---|---|---|---|
| **Anthropic (Claude API)** | 投稿文生成・分析・バイラル予測・CTR最適化 | 従量課金（$3/100万トークン〜） | https://console.anthropic.com/ |
| **Kie.ai** | 画像生成 (Nano Banana Pro) + 動画生成 (VEO 3.1) | 従量課金（~$0.05/画像〜, ~$0.40/動画〜） | https://kie.ai/ |

### 必須（SNSプラットフォーム）

| サービス | 用途 | 費用 | 備考 |
|---|---|---|---|
| **X (Twitter) Developer** | 投稿・トレンド取得・バイラルリサーチ | Free〜Basic($100/月) | https://developer.x.com/ |
| **Instagram アカウント** | 投稿・ストーリー・リール | 無料 | ビジネスアカウントに切替必須 |
| **TikTok Developer** | 動画投稿・メトリクス取得 | 無料 | https://developers.tiktok.com/ |
| **YouTube (Google)** | 動画・Shorts投稿 | 無料 | YouTube Studio + API有効化 |

### 必須（収益化 — アフィリエイト）

| サービス | 用途 | 費用 | 備考 |
|---|---|---|---|
| **A8.net** | コスメ系アフィリエイト | 無料 | https://www.a8.net/ |
| **afb** | 美容系アフィリエイト | 無料 | https://www.afi-b.com/ |
| **Amazonアソシエイト** | 商品アフィリエイト | 無料 | https://affiliate.amazon.co.jp/ |
| **楽天アフィリエイト** | 商品アフィリエイト | 無料 | https://affiliate.rakuten.co.jp/ |
| **Qoo10アフィリエイト** | コスメ特化 | 無料 | https://www.qoo10.jp/ |

### 推奨（収益化 — メンバーシップ）

| サービス | 用途 | 費用 | 備考 |
|---|---|---|---|
| **pixiv FANBOX** | メイキング・ルーティン詳細の月額支援 | 無料（手数料10%） | https://www.fanbox.cc/ |
| **YouTube メンバーシップ** | 月額限定動画 | 無料（手数料30%） | チャンネル登録者500人〜 |

### 推奨（追加収益）

| サービス | 用途 | 費用 | 備考 |
|---|---|---|---|
| **BOOTH** | 美容ノウハウ PDF・テンプレート販売 | 無料（手数料5.6%+22円） | https://booth.pm/ |
| **note** | 有料記事（美容ノウハウ） | 無料（手数料10-20%） | https://note.com/ |
| **Patreon** | 海外向けサブスク | 無料（手数料5-12%） | https://www.patreon.com/ |

### 推奨（PR案件獲得）

| サービス | 用途 | 費用 | 備考 |
|---|---|---|---|
| **toridori marketing** | PR案件マッチング | 無料 | https://toridori.co.jp/ |
| **SPIRIT** | インフルエンサー案件 | 無料 | https://and-spirit.com/ |
| **REECH** | PR案件 | 無料 | フォロワー数に応じて |

---

## 2. ダウンロード・インストールするもの

### 必須ソフトウェア

| ソフト | バージョン | 用途 | 入手先 |
|---|---|---|---|
| **Python** | 3.11以上 | システム実行環境 | https://www.python.org/ |
| **Git** | 最新 | バージョン管理 | https://git-scm.com/ |
| **Docker**（推奨） | 最新 | コンテナ実行 | https://www.docker.com/ |

### 推奨ソフトウェア

| ソフト | 用途 | 入手先 |
|---|---|---|
| VS Code | コード編集・デバッグ | https://code.visualstudio.com/ |
| Docker Desktop | GUIでDocker管理 | https://www.docker.com/products/docker-desktop/ |

### 自動インストールされるPythonパッケージ（43個）

| カテゴリ | パッケージ | 用途 |
|---|---|---|
| **AI** | anthropic, httpx | Claude API, Kie.ai画像/動画生成 |
| **Web** | fastapi, uvicorn, slowapi | ダッシュボード, API, レート制限 |
| **DB** | sqlalchemy[asyncio], aiosqlite | データベース（SQLite/PostgreSQL対応） |
| **SNS** | tweepy, instagrapi, httpx | X, Instagram, TikTok API |
| **Google** | google-api-python-client, google-auth | YouTube API |
| **自動化** | apscheduler | スケジューラ |
| **分析** | feedparser, beautifulsoup4 | トレンドスクレイピング |
| **設定** | pydantic, pydantic-settings, python-dotenv | 設定管理 |
| **I/O** | aiofiles | 非同期ファイル処理 |
| **テスト** | pytest, pytest-asyncio | テストスイート |

---

## 3. 事前に準備するもの

### APIキー・認証情報（全10個）

| # | 項目 | 取得方法 | 所要時間 |
|---|---|---|---|
| 1 | **Anthropic APIキー** | Console → API Keys → Create Key | 5分 |
| 2 | **Kie.ai APIキー** | https://kie.ai/ → アカウント作成 → API Keys | 5分 |
| 3 | **X API Key + Secret** | Developer Portal → App作成 → Consumer Keys | 30分 |
| 4 | **X Access Token + Secret** | Developer Portal → App → Authentication Tokens | 上記含む |
| 5 | **X Bearer Token** | Developer Portal → App → Bearer Token | 上記含む |
| 6 | **Instagram ユーザー名/パスワード** | ビジネスアカウントのログイン情報 | - |
| 7 | **TikTok Access Token** | Developer Portal → App → Content Posting API → OAuth | 30分 |
| 8 | **YouTube API Key** | Cloud Console → YouTube Data API v3有効化 → APIキー作成 | 10分 |
| 9 | **YouTube OAuth Client ID/Secret** | Cloud Console → OAuth同意画面 → デスクトップアプリ認証情報 | 20分 |
| 10 | **SNS_API_KEY（自作）** | 自分で安全なランダム文字列を生成 | 1分 |

### SNSアカウント準備

| プラットフォーム | 設定内容 |
|---|---|
| **X** | プロフ写真・ヘッダー設定、固定ツイート（おすすめコスメまとめ・リンク集） |
| **Instagram** | プロアカウント（クリエイター）切替、リンク集（lit.link等）設置、ハイライトカテゴリ作成 |
| **TikTok** | クリエイターアカウント設定、プロフィールにリンク設置 |
| **YouTube** | チャンネルアート設定、概要欄にSEOキーワード＋リンク集 |
| **FANBOX（任意）** | クリエイターページ作成、メイキング・詳細ルーティンのプラン設定 |

### コンテンツ素材（全自動生成）

| 項目 | 自動生成方法 | 手動不要 |
|---|---|---|
| プロフィール写真 | Kie.ai Nano Banana ProでAI生成（キャラクター一貫性あり） | ✅ |
| 美容・日常写真 | 投稿時にKie.ai Nano Banana Proが自動生成（顔は写さない構図） | ✅ |
| コスメ写真 | レビュー投稿時にKie.ai Nano Banana Proが自動生成 | ✅ |
| 動画素材 | Kie.ai VEO 3.1が自動生成（GRWM/チュートリアル/try-on） | ✅ |

> **完全自動運用:** 写真・動画は全てAIが自動生成します。
> キャラクターの外見（髪型・体型・雰囲気）は `config/media_prompts.py` の
> `CHARACTER_BASE` で一貫性を保っています。カスタマイズも可能です。

---

## 4. 導入手順（ステップバイステップ）

### Phase 1: 環境構築（30分）

```bash
# Step 1: Python確認（3.11以上必須）
python3 --version

# Step 2: プロジェクトに移動
cd /opt/sns-automation

# Step 3: 仮想環境作成（推奨）
python3 -m venv venv
source venv/bin/activate  # Mac/Linux
# venv\Scripts\activate   # Windows

# Step 4: パッケージインストール
pip install -r requirements.txt

# Step 5: 環境変数ファイル作成
cp .env.example .env

# Step 6: データベース用ディレクトリ作成
mkdir -p data/media
```

### Phase 2: APIキー設定（1〜2時間）

**.envファイルを編集して各APIキーを設定:**

```bash
# Step 7: .envファイルを編集
# お好みのエディタで開く
nano .env   # or: code .env
```

```env
# ========================================
# 必須: AI API
# ========================================

# Anthropic Claude (https://console.anthropic.com/)
ANTHROPIC_API_KEY=
CLAUDE_MODEL=claude-sonnet-4-6

# Kie.ai (https://kie.ai/)
# 画像生成 (Nano Banana Pro) + 動画生成 (VEO 3.1) 統合API
KIE_API_KEY=xxxxxxxx

# ========================================
# 必須: SNS プラットフォーム
# ========================================

# X (Twitter) - https://developer.x.com/
X_API_KEY=xxxxxxxx
X_API_SECRET=xxxxxxxx
X_ACCESS_TOKEN=xxxxxxxx-xxxxxxxx
X_ACCESS_TOKEN_SECRET=xxxxxxxx
X_BEARER_TOKEN=xxxxxxxx

# Instagram
INSTAGRAM_USERNAME=your_instagram_id
INSTAGRAM_PASSWORD=
INSTAGRAM_PROXY=  # オプション: プロキシURL（レート制限回避用）

# TikTok - https://developers.tiktok.com/
TIKTOK_ACCESS_TOKEN=xxxxxxxx

# YouTube - Cloud Console
YOUTUBE_API_KEY=xxxxxxxx
YOUTUBE_CLIENT_ID=xxxxxxxx.apps.googleusercontent.com
YOUTUBE_CLIENT_SECRET=xxxxxxxx

# ========================================
# アプリケーション設定
# ========================================

# API認証キー（ランダム文字列を自分で設定 ※必須）
# 例: openssl rand -hex 32 で生成
SNS_API_KEY=ここに安全なランダム文字列を設定

# データベース
DATABASE_URL=sqlite+aiosqlite:///data/sns_automation.db

# タイムゾーン
DEFAULT_TIMEZONE=Asia/Tokyo

# 1日の最大投稿数
MAX_POSTS_PER_DAY=10

# ========================================
# メンバーシップURL（任意）
# ========================================
FANBOX_URL=
PATREON_URL=

# ========================================
# アラート通知（Slack/Discord Webhook）
# ========================================
ALERT_WEBHOOK_URL=  # Slack or Discord Webhook URL
```

```bash
# Step 8: SNS_API_KEYを安全に生成
python3 -c "import secrets; print(secrets.token_hex(32))"
# 出力されたランダム文字列を .env の SNS_API_KEY に貼付
```

### Phase 3: SNSアカウント最適化（1〜2時間）

```
Step 9: X (Twitter) アカウント設定
  - プロフィール写真・ヘッダー設定
  - 自己紹介文にキーワード埋め込み
    例: 「コスメ大好き🌸｜毎日のメイク記録💄｜正直レビュー｜AI生成画像を使用しています」
  - 固定ツイートにおすすめコスメまとめ・リンク集を設定

Step 10: Instagram 設定
  - プロアカウント（クリエイター）に切替
  - プロフィールにリンク集（lit.link等）設置
  - ハイライトカテゴリ作成（コスメ/ルーティン/レビュー/etc）

Step 11: TikTok 設定
  - クリエイターアカウント設定
  - プロフィールにリンク設置

Step 12: YouTube 設定
  - チャンネルアート・アイコン設定
  - チャンネル説明にSEOキーワード
  - 概要欄テンプレート作成（リンク集・PR表記のテンプレート含む）
```

### Phase 4: 収益化準備（2〜3時間）

```
Step 13: アフィリエイト登録
  - A8.net → 会員登録 → コスメ系プログラムに提携申請
  - afb → 会員登録 → 美容系プログラムに提携申請
  - Amazonアソシエイト → 申請（審査あり。投稿実績が必要）
  - 楽天アフィリエイト → 登録

Step 14: メンバーシップ（任意）
  - FANBOX または YouTube メンバーシップでプラン作成
    例: 500円プラン（使用コスメの詳細リスト）
        1,000円プラン（メイキング動画＋先行公開）
  - 銀行口座・振込先を設定

Step 15: PR案件プラットフォーム（フォロワー増加後）
  - toridori marketing 登録
  - SPIRIT 登録
  ※フォロワー1,000人超えたら案件が来始める
```

### Phase 5: システム起動・動作確認（10分）

```bash
# =========================================
# 方法A: 直接起動（開発・テスト用）
# =========================================

# Step 16: 動作確認テスト
cd /opt/sns-automation

# ペルソナ設定確認
python -m src.cli persona

# コンテンツ生成テスト（Claude APIが必要）
python -m src.cli generate x "新作リップレビュー" --tone engaging

# ルーティン投稿生成テスト
python -m src.cli routine instagram

# トレンドリサーチテスト
python -m src.cli trends --niche "美容コスメ"

# バイラルスコア予測テスト
python -m src.cli predict x "新作リップ、ブルベにも似合う色だった…！"

# Step 17: ダッシュボード起動
python -m src.cli serve
# → ブラウザで http://localhost:8000 を開く
# → 初回アクセス時にAPIキーの入力を求められる


# =========================================
# 方法B: Docker起動（本番運用推奨）
# =========================================

# Step 16b: Dockerビルド＆起動
docker-compose up -d --build

# Step 17b: ログ確認
docker-compose logs -f

# → ブラウザで http://localhost:8000 を開く
```

### Phase 6: 自動運用開始確認

```
Step 18: 自動スケジュール確認

ダッシュボード起動と同時に以下が自動実行される:

  [3時間毎] トレンドリサーチ → コンテンツ自動生成 → スケジュール登録
             - 美容コスメ・ルーティン・レビュー投稿
             - 収益化投稿（PR表記つき、15%比率で自動挿入）
             - SEO最適化・ハッシュタグ自動付与
             - 全プラットフォームでKie.ai画像(Nano Banana Pro)/動画(VEO 3.1)を自動生成（手動素材不要）

  [5分毎]   スケジュール済み投稿の自動公開
             - 最適時間帯に合わせて投稿
             - プラットフォーム別にメディア形式を自動判定
             - 失敗時は自動リトライ（指数バックオフ）
             - PUBLISHING状態のスタック投稿を15分毎に自動復旧

  [1時間毎]  メトリクス収集（並列処理・重複防止）
             - いいね・RT・コメント・保存・クリック数
             - エンゲージメント率＋CTR自動計算
             - フィードバックループ: 結果を次回の生成に自動反映

  [毎日23:59] フォロワー数スナップショット記録

  [追加機能]
             - Instagram Stories自動投稿（ストーリー素材の自動生成・投稿）
             - コメント監視（各プラットフォームのコメントを自動監視・分析）
             - A/Bテスト自動実行（投稿バリエーションの効果測定・最適化）
             - アラート通知（Slack/Discord Webhookで異常検知・重要イベント通知）
             - データベースマイグレーション（Alembicによるスキーマ管理）
```

---

## 5. 費用見積もり（月額）

| 項目 | 初月 | 安定期（3ヶ月後〜） | 備考 |
|---|---|---|---|
| Claude API | ¥3,000〜5,000 | ¥5,000〜10,000 | 投稿生成・分析 |
| Kie.ai (画像+動画) | ¥2,000〜5,000 | ¥3,000〜8,000 | Nano Banana Pro + VEO 3.1 |
| X Developer (Basic) | ¥0〜15,000 | ¥0〜15,000 | Freeプランでも可 |
| サーバー（VPS等） | ¥1,000〜3,000 | ¥1,000〜3,000 | Docker推奨 |
| **合計** | **¥6,000〜28,000** | **¥9,000〜36,000** | |

**予算管理:** システムに日次上限($10)＋月次上限($250)のAPI予算アラートが組み込み済み。超過時は自動停止。

---

## 6. 収益目標（目安）

| フォロワー数 | 期間目安 | 想定月収 | 主な収益源 |
|---|---|---|---|
| 〜1,000 | 1〜2ヶ月 | ¥0〜5,000 | アフィリエイト少額 |
| 1,000〜5,000 | 2〜4ヶ月 | ¥10,000〜50,000 | アフィリエイト |
| 5,000〜10,000 | 4〜8ヶ月 | ¥50,000〜150,000 | アフィリエイト + PR案件開始 |
| 10,000〜50,000 | 8〜18ヶ月 | ¥150,000〜500,000 | PR + メンバーシップ + 広告 |
| 50,000〜 | 18ヶ月〜 | ¥500,000〜 | 全収益源フル稼働 |

### 収益化の内訳イメージ（フォロワー1万人時）

| 収益源 | 月額目安 | 備考 |
|---|---|---|
| アフィリエイト | ¥20,000〜50,000 | コスメレビュー経由 |
| メンバーシップ | ¥10,000〜30,000 | 有料会員 20〜60人 × 500円 |
| PR案件 | ¥30,000〜100,000 | 月1〜3件 × 1〜3万円 |
| YouTube広告 | ¥5,000〜20,000 | 再生回数に依存 |

---

## 7. システム構成図

```
┌─────────────────────────────────────────────────────────────┐
│                     SNS自動化システム                          │
│                                                               │
│  ┌──────────┐  ┌──────────────┐  ┌─────────────┐  ┌──────────┐  │
│  │  Claude   │  │  Kie.ai Nano │  │  Kie.ai     │  │ Trend    │  │
│  │  API      │  │  Banana Pro  │  │  VEO 3.1    │  │ Research │  │
│  └────┬─────┘  └──────┬───────┘  └──────┬──────┘  └────┬─────┘  │
│       │              │              │              │           │
│  ┌────┴──────────────┴──────────────┴──────────────┴─────┐   │
│  │              コンテンツ生成エンジン                       │   │
│  │  ┌─────────────┐ ┌──────────┐ ┌──────────────────┐    │   │
│  │  │コンテンツミックス│ │SEO最適化 │ │CTR/CTA最適化     │    │   │
│  │  │ルーター(比率制御)│ │+ハッシュタグ│ │+バイラルスコア   │    │   │
│  │  └─────────────┘ └──────────┘ └──────────────────┘    │   │
│  └───────────────────────┬───────────────────────────────┘   │
│                           │                                    │
│  ┌────────────────────────┴──────────────────────────────┐   │
│  │                    スケジューラ                          │   │
│  │  最適時間投稿 │ リトライ │ スタック復旧 │ 予算制御       │   │
│  └────────────────────────┬──────────────────────────────┘   │
│                           │                                    │
│  ┌────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐          │
│  │   X    │ │Instagram │ │  TikTok  │ │ YouTube  │          │
│  └────┬───┘ └────┬─────┘ └────┬─────┘ └────┬─────┘          │
│       │          │              │              │               │
│  ┌────┴──────────┴──────────────┴──────────────┴─────┐       │
│  │                 分析・フィードバック                   │       │
│  │  メトリクス収集 → CTR分析 → フィードバックループ       │       │
│  │  → 次回生成に反映（自動改善サイクル）                  │       │
│  └───────────────────────────────────────────────────┘       │
│                                                               │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐                   │
│  │ Dashboard │  │  CLI     │  │  REST    │                   │
│  │ Web UI   │  │ コマンド  │  │  API     │                   │
│  └──────────┘  └──────────┘  └──────────┘                   │
└─────────────────────────────────────────────────────────────┘
```

### コンテンツミックス比率（自動制御）

```
X (Twitter):
  美容コスメ 35% │ レビュー 20% │ 日常 15%
  エンゲージメント 15% │ 収益化(PR) 15%

Instagram:
  美容 35% │ ルーティン 25% │ レビュー 25% │ 収益化(PR) 15%

YouTube:
  GRWM 35% │ チュートリアル 35% │ try-on 30%

※ 比率は ratio_optimizer がエンゲージメント実績に応じて自動調整（config/persona.py の CONTENT_SCHEDULE_RATIO が初期値）
```

---

## 8. トラブルシューティング

### よくあるエラーと対処法

| エラー | 原因 | 対処 |
|---|---|---|
| `500 Internal Server Error` | Anthropic APIの一時障害 | 自動リトライが働く。数分待つ |
| `429 Too Many Requests` | API予算超過 | 日次/月次予算を.envで調整、翌日まで待つ |
| `503 Server not configured` | SNS_API_KEYが未設定 | .envにSNS_API_KEYを設定 |
| `401 Invalid API key` | ダッシュボードのAPIキーが間違い | ブラウザで「Change API Key」ボタンを押す |
| `database is locked` | SQLite同時書込み | 高負荷時はPostgreSQLへ移行を検討 |
| Instagram login_required | セッション期限切れ | 自動再ログインが動作。連続失敗時はパスワード確認 |
| Post stuck in PUBLISHING | 投稿中のクラッシュ | 15分毎に自動復旧ジョブが実行される |

### ログ確認方法

```bash
# 直接起動時
# ターミナルにリアルタイムでログ出力

# Docker起動時
docker-compose logs -f          # 全ログ
docker-compose logs --tail 100  # 最新100行
```

### ヘルスチェック

```bash
# ブラウザまたはcurlで確認
curl http://localhost:8000/health

# 正常時: {"status": "healthy", "components": {...}}
# 異常時: {"status": "degraded", ...} (HTTP 503)
```

### テスト実行

```bash
cd /opt/sns-automation
pytest tests/ -v
```

---

## チェックリスト

- [ ] Python 3.11+ インストール済み
- [ ] .env ファイル作成済み
- [ ] Anthropic APIキー設定済み
- [ ] Kie.ai APIキー設定済み
- [ ] SNS_API_KEY 設定済み（安全なランダム文字列）
- [ ] X Developer アカウント＋APIキー取得済み
- [ ] Instagram ビジネスアカウント設定済み
- [ ] TikTok Developer アカウント設定済み
- [ ] YouTube API 有効化済み
- [ ] アフィリエイト（A8.net/afb）登録済み
- [ ] `pip install -r requirements.txt` 実行済み
- [ ] `python -m src.cli persona` でペルソナ確認済み
- [ ] `python -m src.cli serve` でダッシュボード起動確認済み
- [ ] ブラウザで http://localhost:8000 アクセス確認済み
