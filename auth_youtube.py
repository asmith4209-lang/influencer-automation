"""
One-time YouTube OAuth authorization script.

Run this on your PC (not on ZimaOS) to generate youtube_token.pkl.
Then copy the token file to ZimaOS: /DATA/credentials/youtube_token.pkl

Usage:
    python auth_youtube.py

When the browser opens, sign in as asmith4209@gmail.com and select Emily's channel.
"""

import os
import pickle
from pathlib import Path

from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

load_dotenv("watcher/.env")

CLIENT_SECRETS = os.getenv("YOUTUBE_CLIENT_SECRETS", "./credentials/youtube_client_secrets.json")
TOKEN_FILE     = os.getenv("YOUTUBE_TOKEN_FILE",    "./credentials/youtube_token.pkl")
SCOPES         = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]


def main():
    print("=" * 55)
    print("  YouTube One-Time Authorization")
    print("=" * 55)
    print()
    print("A browser window will open.")
    print("Sign in as: asmith4209@gmail.com")
    print("Select: Emily's channel (NOT your personal channel)")
    print()
    input("Press Enter to open the browser...")

    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS, SCOPES)
    creds = flow.run_local_server(port=0)

    token_path = Path(TOKEN_FILE)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    with open(token_path, "wb") as f:
        pickle.dump(creds, f)

    print()
    print(f"Token saved to: {token_path}")
    print()
    print("Next step — copy to ZimaOS:")
    print(f"  From: {token_path.resolve()}")
    print(f"  To:   /DATA/credentials/youtube_token.pkl")
    print()
    print("Done! The watcher will use this token automatically from now on.")


if __name__ == "__main__":
    main()
