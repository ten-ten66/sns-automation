"""YouTube OAuth2 初回認証スクリプト.

ブラウザで認証URLを開き、認証コードを入力してトークンを保存します。
"""
import json
import os
from google_auth_oauthlib.flow import Flow

SCOPES = [
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/youtube.upload",
]

CLIENT_CONFIG = {
    "web": {
        "client_id": os.environ["YOUTUBE_CLIENT_ID"],
        "client_secret": os.environ["YOUTUBE_CLIENT_SECRET"],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [os.environ["YOUTUBE_REDIRECT_URI"]],
    }
}

def main():
    flow = Flow.from_client_config(CLIENT_CONFIG, scopes=SCOPES)
    flow.redirect_uri = os.environ["YOUTUBE_REDIRECT_URI"]
    
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    
    print("\n" + "=" * 60)
    print("以下のURLをブラウザで開いてGoogleアカウントで認証してください:")
    print("=" * 60)
    print(f"\n{auth_url}\n")
    print("=" * 60)
    print("認証後、リダイレクトされたURLを丸ごとコピーして貼り付けてください:")
    print("(例: <YOUTUBE_REDIRECT_URI>?code=xxxx&scope=xxxx)")
    print("=" * 60)
    
    redirect_url = input("\nリダイレクトURL: ").strip()
    
    flow.fetch_token(authorization_response=redirect_url)
    credentials = flow.credentials
    
    creds_data = {
        "token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "scopes": list(credentials.scopes),
    }
    
    output_path = "data/youtube_oauth_credentials.json"
    with open(output_path, "w") as f:
        json.dump(creds_data, f, indent=2)
    
    print(f"\n✅ 認証成功！トークンを {output_path} に保存しました。")
    print("サーバーを再起動すれば YouTube の投稿・コメント機能が有効になります。")

if __name__ == "__main__":
    main()
